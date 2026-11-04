import asyncio

from shs_agent import AgentLimits, TaskStatus

from tests.fakes import (AsyncCase, FakeProvider, calls, complete, event_of,
                         events_of, tool_results)


class SessionTests(AsyncCase):
    async def test_question_answer_matching_and_legacy_keywords(self):
        provider = FakeProvider(calls(("host_ask", {"question": "which?", "context": "a or b"})), complete("b"))
        session = await self.start(provider)
        event = await event_of(session, "question")
        self.assertEqual(session.status, TaskStatus.WAITING_FOR_HOST)
        wrong = await session.answer("wrong id", "a")
        await event_of(session, "command_rejected", lambda e: e.payload["message_id"] == wrong)
        await session.answer(request_id=event.payload["request_id"], content="b")
        result = await self.result(session)
        self.assertEqual(tool_results(result)[0], {"ok": True, "output": "b"})
        self.assertEqual(result.output, "b")
        self.assert_balanced(result.messages)
        closed = await event_of(session, "question_closed")
        self.assertEqual(closed.payload["reason"], "answered")

    async def test_answer_canonical_keywords_and_duplicate_command(self):
        session = await self.start(FakeProvider(calls(("host_ask", {"question": "pick"})), complete()))
        question = await event_of(session, "question")
        for _ in range(2):
            await session.answer(question_id=question.payload["question_id"], text="a", message_id="answer-1")
        await self.result(session)
        events = await events_of(session)
        received = [e for e in events if e.type == "command_received" and e.payload["message_id"] == "answer-1"]
        self.assertEqual(len(received), 1)

    async def test_instruction_interrupts_question_and_invalidates_rest_of_batch(self):
        provider = FakeProvider(calls(("host_ask", {"question": "old question"}),
                                      ("memo_write", {"content": "obsolete", "kind": "fact"})), complete("new"))
        session = await self.start(provider, limits=AgentLimits(host_response_timeout=None))
        question = await event_of(session, "question")
        await session.send_instruction("the answer is unnecessary; do the new task")
        result = await self.result(session)
        self.assertEqual((result.status, result.output), (TaskStatus.COMPLETED, "new"))
        self.assertFalse(result.memos)
        self.assertEqual(tool_results(result)[0]["error"]["type"], "question_interrupted")
        self.assertEqual(tool_results(result)[1]["error"]["type"], "not_executed")
        self.assert_balanced(result.messages)
        closed = await event_of(session, "question_closed")
        self.assertEqual(closed.payload["question_id"], question.payload["question_id"])
        self.assertEqual(closed.payload["reason"], "superseded_by_host_instruction")

    async def test_cancel_interrupts_question_without_host_timeout(self):
        session = await self.start(FakeProvider(calls(("host_ask", {"question": "pick"}))),
                                  limits=AgentLimits(host_response_timeout=None))
        await event_of(session, "question")
        await session.cancel()
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.CANCELLED)
        self.assert_balanced(result.messages)

    async def test_question_timeout_is_incomplete_and_settles_remaining_calls(self):
        session = await self.start(FakeProvider(calls(("host_ask", {"question": "pick"}),
                                                     ("memo_write", {"content": "no", "kind": "fact"}))),
                                  limits=AgentLimits(host_response_timeout=0.02))
        result = await self.result(session)
        self.assertEqual((result.status, result.stop_reason), (TaskStatus.INCOMPLETE, "host_response_timeout"))
        self.assertFalse(result.memos)
        self.assert_balanced(result.messages)

    async def test_multiple_live_subscribers_replay_and_snapshot_isolation(self):
        gate = self.gate(complete())
        session = await self.start(FakeProvider(gate))
        first = asyncio.create_task(events_of(session))
        second = asyncio.create_task(events_of(session))
        await gate.entered.wait()
        gate.release.set()
        result = await self.result(session)
        a, b = await asyncio.gather(first, second)
        self.assertEqual(a, b)
        self.assertEqual([e.sequence for e in a], list(range(1, len(a) + 1)))
        self.assertEqual(a[-1].type, "finished")
        self.assertTrue(all(e.task_id == session.task_id for e in a))
        a[-1].payload["output"] = "tampered"
        result.messages[0]["content"] = "tampered"
        replay = [e async for e in session.events(after_sequence=2)]
        self.assertEqual(replay, b[2:])
        self.assertNotEqual((await session.result()).messages[0]["content"], "tampered")
        self.assertEqual([e async for e in session.events(after_sequence=len(b))], [])
        for cursor in (-1, True, len(b) + 1):
            with self.subTest(cursor=cursor), self.assertRaises(ValueError):
                await anext(session.events(after_sequence=cursor))

    async def test_slow_subscriber_does_not_block_execution(self):
        session = await self.start(FakeProvider(complete()))
        stream = session.events()
        first = await anext(stream)
        self.assertEqual(first.type, "created")
        self.assertEqual((await self.result(session)).status, TaskStatus.COMPLETED)
        remaining = [e async for e in stream]
        self.assertEqual(remaining[-1].type, "finished")

    async def test_command_idempotency_conflicts_and_terminal_rejection(self):
        gate = self.gate(complete("old"))
        session = await self.start(FakeProvider(gate, complete()))
        await gate.entered.wait()
        await session.send_instruction("new", message_id="one")
        await session.send_instruction("new", message_id="one")
        with self.assertRaises(ValueError):
            await session.send_instruction("different", message_id="one")
        gate.release.set()
        result = await self.result(session)
        self.assertEqual(sum(m.get("content") == "new" for m in result.messages), 1)
        self.assertEqual(await session.send_instruction("new", message_id="one"), "one")
        with self.assertRaises(RuntimeError):
            await session.cancel()

    async def test_cancelled_result_waiter_does_not_cancel_runtime(self):
        gate = self.gate(complete())
        session = await self.start(FakeProvider(gate))
        await gate.entered.wait()
        waiter = asyncio.create_task(session.result())
        await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertFalse(session.done)
        gate.release.set()
        self.assertEqual((await self.result(session)).status, TaskStatus.COMPLETED)

    async def test_early_answer_is_rejected_without_affecting_task(self):
        session = await self.start(FakeProvider(complete()))
        command = await session.answer("not-open", "answer")
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        event = await event_of(session, "command_rejected")
        self.assertEqual(event.payload["message_id"], command)

    async def test_cancel_at_tool_started_before_question_event_cannot_leave_waiter(self):
        session = await self.start(FakeProvider(calls(("host_ask", {"question": "pick"}))),
                                  limits=AgentLimits(host_response_timeout=None))
        await event_of(session, "tool_started")
        await session.cancel()
        self.assertEqual((await self.result(session)).status, TaskStatus.CANCELLED)
