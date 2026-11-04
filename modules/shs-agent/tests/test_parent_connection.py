import asyncio

from shs_agent import AgentLimits, TaskStatus, TaskTreeSnapshot
from shs_agent.agent.communication import ParentConnection, RootCommunication, ChildCommunication
from tests.fakes import AsyncCase, FakeProvider, calls, complete, event_of, tool_results
from tests.test_subagent import outputs
from tests.test_communication import incoming


class ParentConnectionTests(AsyncCase):
    def test_one_class_for_both_relationship_directions(self):
        self.assertIs(ParentConnection, RootCommunication)
        self.assertIs(ParentConnection, ChildCommunication)

    async def test_root_can_send_two_questions_before_waiting_and_host_answers_out_of_order(self):
        def wait_both(request):
            ids = [x["output"]["request_id"] for x in outputs(request, "parent_ask")]
            return calls(*(("agent_wait_reply", {"request_id": key}) for key in ids))
        provider = FakeProvider(calls(("parent_ask", {"message": "one"}), ("parent_ask", {"message": "two"})),
                                wait_both, complete())
        session = await self.start(provider)
        one = await event_of(session, "question", lambda e: e.payload["question"] == "one")
        two = await event_of(session, "question", lambda e: e.payload["question"] == "two")
        await session.answer(two.payload["request_id"], "second answer")
        await session.answer(one.payload["request_id"], "first answer")
        result = await self.result(session)
        self.assertEqual((result.status, result.tool_errors), (TaskStatus.COMPLETED, 0))
        self.assertEqual([r.status for r in await session.requests()], ["answered", "answered"])
        answers = [x["output"]["reply"]["content"] for x in tool_results(result)[2:4]]
        self.assertEqual(answers, ["first answer", "second answer"])
        tree = TaskTreeSnapshot(session.task_id, {session.task_id: result.tracer})
        self.assertIsNone(tree.traces[session.task_id].state().task.parent_task_id)
        self.assert_balanced(result.messages)

    async def test_new_host_question_interrupts_wait_then_original_wait_resumes(self):
        def wait_own(request):
            return calls(("agent_wait_reply", {"request_id": outputs(request, "parent_ask")[0]["output"]["request_id"]}))
        def reply(request):
            return calls(("agent_reply", {"request_id": incoming(request, "question")[0]["request_id"],
                                           "message": "still working"}))
        provider = FakeProvider(calls(("parent_ask", {"message": "configuration?"})),
                                wait_own, reply, wait_own, complete())
        session = await self.start(provider)
        original = await event_of(session, "question")
        await event_of(session, "agent_wait_started")
        progress_id = await session.ask("progress?")
        answered = await event_of(session, "parent_reply")
        self.assertEqual(answered.payload["request_id"], progress_id)
        await session.answer(original.payload["request_id"], "configuration supplied")
        result = await self.result(session)
        self.assertEqual((result.status, result.tool_errors), (TaskStatus.COMPLETED, 0))
        results = tool_results(result)
        waits = [results[1], results[3]]
        self.assertEqual([r["output"]["reason"] for r in waits], ["incoming_request", "answered"])
        self.assert_balanced(result.messages)

    async def test_host_question_timeout_is_visible_and_cancel_closes_request(self):
        def wait(request):
            return calls(("agent_wait_reply", {"request_id": outputs(request, "parent_ask")[0]["output"]["request_id"]}))
        provider = FakeProvider(calls(("parent_ask", {"message": "permission?"})), wait)
        session = await self.start(provider, limits=AgentLimits(host_response_timeout=0.02))
        result = await self.result(session)
        self.assertEqual(result.stop_reason, "host_response_timeout")
        self.assertEqual((await session.requests())[0].status, "timed_out")
        provider = FakeProvider(calls(("parent_ask", {"message": "permission?"})), wait)
        session = await self.start(provider, limits=AgentLimits(host_response_timeout=None))
        await event_of(session, "agent_wait_started")
        await session.cancel()
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.CANCELLED)
        self.assertEqual((await session.requests())[0].status, "closed")
