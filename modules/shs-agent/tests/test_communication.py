"""Deterministic communication tests. Gates, not elapsed sleeps, establish ordering."""
import asyncio
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from shs_agent import AgentActivity, AgentLimits, ProblemSolver, SessionState, TaskStatus
from shs_agent.agent.communication import AgentMessage, CommunicationError
from shs_agent.agent.context import ContextBuilder, TaskContext
from shs_agent.agent.context.compaction import Summary
from shs_agent.agent.subagent import TaskTreeSnapshot
from shs_agent.agent.tools.schemas import COMMUNICATION_SCHEMAS
from shs_agent import TaskRequest
from tests.fakes import AsyncCase, FakeProvider, calls, complete, event_of, events_of, registry_for
from tests.test_subagent import profile, spawn, child_ids, outputs, wait_children, read_children


def incoming(request, kind=None):
    result = []
    for message in request["messages"]:
        if message["role"] == "user" and message.get("content", "").startswith("Agent communication"):
            data = json.loads(message["content"].split("\n", 1)[1])
            if kind is None or data["kind"] == kind:
                result.append(data)
    return result


def wait_own(request, name="parent_ask", index=-1):
    return calls(("agent_wait_reply", {"request_id": outputs(request, name)[index]["output"]["request_id"]}))


class CommunicationTests(AsyncCase):
    async def start_tree(self, parent, children, **kwargs):
        kwargs.setdefault("limits", AgentLimits(max_total_subagents=4, max_subagent_depth=1,
                                                 context_compact_threshold_chars=None))
        session = await self.start(parent, subagents=children, **kwargs)
        return session

    async def held_tree(self, count=2):
        parent_gate = self.gate(complete())
        child_gates = [self.gate(complete()) for _ in range(count)]
        session = await self.start_tree(FakeProvider(parent_gate), {
            str(i): profile(FakeProvider(gate)) for i, gate in enumerate(child_gates)})
        await parent_gate.entered.wait()
        children = []
        for i, gate in enumerate(child_gates):
            value = await session.subagents.spawn(session.task_id, instruction=f"child {i}", context=[], config_id=str(i))
            children.append(session.subagents.child(value["task_id"]))
            await gate.entered.wait()
        return session, children, parent_gate, child_gates

    async def test_a_parent_b_roundtrip_while_b_tool_batch_runs_and_trace_reconstructs(self):
        a_start = self.gate(calls(("parent_ask", {"message": "login response format?"})))
        b_work = self.gate("old B work done")
        b_continue = self.gate("continued B work")
        b_marker = asyncio.Event()
        async def marker():
            b_marker.set()
            return "batch tail"
        def a_finish(request):
            answer = outputs(request, "agent_wait_reply")[-1]["output"]
            self.assertEqual(answer["reason"], "answered")
            self.assertEqual(answer["reply"]["content"], "access_token and user")
            return complete("A used access_token and user")
        a = FakeProvider(a_start, wait_own, a_finish)
        def b_reply(request):
            self.assertTrue(b_marker.is_set())
            self.assertEqual(len(incoming(request, "question")), 1)
            self.assert_balanced(request["messages"])
            return calls(("agent_reply", {"request_id": incoming(request, "question")[0]["request_id"],
                                           "message": "access_token and user"}))
        b = FakeProvider(calls(("work", {}), ("marker", {})), b_reply,
                         calls(("continue_work", {})), complete("B original task done"))
        def ask_b(request):
            released = outputs(request, "subagent_wait")[-1]["output"]
            question = incoming(request, "question")[0]
            self.assertEqual(released["reason"], "incoming_request")
            self.assertEqual(released["request_ids"], [question["request_id"]])
            return calls(("subagent_ask", {"task_id": child_ids(request)[1], "message": "login response?",
                                            "related_request_id": question["request_id"]}))
        def relay(request):
            reply = outputs(request, "agent_wait_reply")[-1]["output"]
            self.assertEqual(reply["reason"], "answered")
            return calls(("agent_reply", {"request_id": incoming(request, "question")[0]["request_id"],
                                           "message": reply["reply"]["content"]}))
        parent = FakeProvider(calls(spawn("A"), spawn("B")), wait_children, ask_b,
                              lambda r: wait_own(r, "subagent_ask"), relay, wait_children,
                              read_children, complete("reviewed A and B"))
        session = await self.start_tree(parent, {"A": profile(a), "B": profile(b, registry_for(
            work=b_work, marker=marker, continue_work=b_continue))})
        root_events = asyncio.create_task(events_of(session))
        await event_of(session, "subagent_wait_started")
        await b_work.entered.wait()
        await a_start.entered.wait()
        children = [session.subagents.child(v["task_id"]) for v in await session.subagents.children()]
        live_events = [asyncio.create_task(events_of(child)) for child in children]
        a_start.release.set()
        received = await event_of(children[1], "agent_message_received", lambda e: e.payload["kind"] == "question")
        state = await children[1].state()
        self.assertEqual(state.agent.activity, AgentActivity.TOOL_EXECUTION)
        self.assertEqual(len(state.agent.queued_message_ids), 1)
        self.assertEqual(len(b.requests), 1)
        self.assertFalse(b_marker.is_set())
        self.assertFalse(children[1].done)
        b_work.release.set()
        await b_continue.entered.wait()
        await event_of(children[0], "agent_message_received", lambda e: e.payload["kind"] == "reply")
        self.assertFalse(children[1].done)
        b_continue.release.set()
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.COMPLETED, result.warnings)
        self.assertEqual(result.tool_errors, 0)
        self.assertEqual([r["output"]["output"] for r in outputs(parent.requests[-1], "subagent_result")],
                         ["A used access_token and user", "B original task done"])
        tree = TaskTreeSnapshot.from_json(result.tree.to_json())
        self.assertEqual(tree.to_dict(), result.tree.to_dict())
        self.assertTrue(tree.complete and tree.details_complete)
        self.assertEqual(await root_events, list(result.tracer.events))
        for child, consumer in zip(children, live_events):
            trace = tree.traces[child.task_id]
            self.assertEqual(await consumer, list(trace.events))
            self.assert_balanced((await child.result()).messages)
            self.assertFalse((await child.result()).memos)
            self.assertIsNone((await child.result()).plan)
            self.assertNotIn("test task", json.dumps((await child.result()).messages))
        self.assert_balanced(result.messages)
        b_trace = tree.traces[children[1].task_id]
        applied = next(e for e in b_trace.events if e.type == "agent_message_applied")
        built = next(e for e in b_trace.events if e.type == "agent_message_context_built")
        self.assertLess(received.sequence, applied.sequence)
        self.assertLess(applied.sequence, built.sequence)
        self.assertFalse(any(e.type == "response_discarded" for e in b_trace.events))
        self.assertEqual(b_trace.state(at_sequence=state.last_event_sequence), state)
        self.assertEqual(SessionState.from_dict(state.to_dict()), state)
        questions = [artifact for trace in tree.traces.values() for artifact in trace.artifacts.values()
                     if artifact.kind == "agent_message_requested" and artifact.data["kind"] == "question"]
        self.assertEqual(len(questions), 2)
        forwarded = next(q for q in questions if q.data["related_request_id"] is not None)
        original = next(q for q in questions if q is not forwarded)
        self.assertNotEqual(forwarded.data["request_id"], original.data["request_id"])
        self.assertEqual(forwarded.data["related_request_id"], original.data["request_id"])
        accepted = next(a for a in result.tracer.artifacts.values() if a.kind == "agent_message_accepted"
                        and a.data.get("request_id") == forwarded.data["request_id"])
        self.assertIn(original.artifact_id, [ref["artifact_id"] for ref in accepted.data["task_refs"]])
        for trace in tree.traces.values():
            for artifact in trace.artifacts.values():
                if artifact.kind.startswith("agent_"):
                    for ref in artifact.data.get("task_refs", []):
                        self.assertTrue(tree.artifact(ref))

    async def test_wait_reply_yields_to_new_request_preserves_original_and_can_resume(self):
        session, children, _, _ = await self.held_tree(1)
        manager, child = session.subagents, children[0]
        original = manager.send_message(session.task_id, child.task_id, kind="question", message="parent query")
        waiting = asyncio.create_task(session._channel.wait_reply(original["request_id"]))
        await event_of(session, "agent_wait_started")
        new = manager.send_message(child.task_id, session.task_id, kind="question", message="child query")
        released = await asyncio.wait_for(waiting, 3)
        self.assertEqual(released["reason"], "incoming_request")
        self.assertEqual(released["request_id"], original["request_id"])
        self.assertEqual(released["request_ids"], [new["request_id"]])
        self.assertEqual(session._channel.request(original["request_id"]).status, "pending")
        session._channel.take_agent_messages()
        manager.reply_message(session.task_id, request_id=new["request_id"], message="parent answer")
        manager.reply_message(child.task_id, request_id=original["request_id"], message="child answer")
        result = await session._channel.wait_reply(original["request_id"])
        self.assertEqual(result["reason"], "answered")
        self.assertEqual(result["reply"]["content"], "child answer")

    async def test_multiple_requests_out_of_order_and_duplicate_reply(self):
        session, children, _, _ = await self.held_tree(1)
        manager, child = session.subagents, children[0]
        requests = [manager.send_message(session.task_id, child.task_id, kind="question", message=str(i))
                    for i in range(3)]
        for i in (2, 0, 1):
            manager.reply_message(child.task_id, request_id=requests[i]["request_id"], message=f"answer {i}")
        for i, request in enumerate(requests):
            result = await session._channel.wait_reply(request["request_id"])
            self.assertEqual(result["reply"]["content"], f"answer {i}")
        duplicate = manager.reply_message(child.task_id, request_id=requests[0]["request_id"], message="duplicate")
        self.assertEqual((duplicate["accepted"], duplicate["reason"], duplicate["request_status"]),
                         (False, "request_closed", "answered"))

    async def test_other_root_sibling_arbitrary_target_and_request_access_rejected(self):
        session, children, _, _ = await self.held_tree()
        other, others, _, _ = await self.held_tree(1)
        manager, a, b = session.subagents, *children
        for caller, target in ((a.task_id, b.task_id), (a.task_id, other.task_id),
                               (session.task_id, others[0].task_id), (session.task_id, "invented"),
                               (session.task_id, session.task_id)):
            with self.subTest(caller=caller, target=target), self.assertRaises(CommunicationError):
                manager.send_message(caller, target, kind="question", message="forbidden")
        question = manager.send_message(a.task_id, session.task_id, kind="question", message="private")
        for caller in (a.task_id, b.task_id):
            with self.assertRaises(CommunicationError):
                manager.reply_message(caller, request_id=question["request_id"], message="forged")
        with self.assertRaises(CommunicationError):
            await session._channel.wait_reply(question["request_id"])
        with self.assertRaises(CommunicationError):
            manager.send_message(b.task_id, session.task_id, kind="question", message="bad link",
                                 related_request_id=question["request_id"])
        with self.assertRaises(CommunicationError):
            manager.send_message(a.task_id, session.task_id, kind="instruction", message="bad instruction")
        endpoint = manager.communication(a._channel)
        for name in ("spawn", "cancel", "child", "_sessions"):
            self.assertFalse(hasattr(endpoint, name))
        # The common interface exists at every depth; relationship checks enforce scope.
        with self.assertRaises(CommunicationError):
            endpoint.ask_child(b.task_id, "forbidden sibling")

    async def test_target_termination_wakes_requester_and_closed_send_is_explicit(self):
        session, children, _, gates = await self.held_tree(1)
        child = children[0]
        question = session.subagents.send_message(session.task_id, child.task_id, kind="question", message="question")
        waiting = asyncio.create_task(session._channel.wait_reply(question["request_id"]))
        await event_of(session, "agent_wait_started")
        await child.cancel()
        gates[0].release.set()
        await self.result(child)
        result = await asyncio.wait_for(waiting, 3)
        self.assertEqual(result["reason"], "target_terminated")
        rejected = session.subagents.send_message(session.task_id, child.task_id, kind="notify", message="late")
        self.assertEqual((rejected["accepted"], rejected["reason"]), (False, "target_terminated"))

    async def test_requester_termination_rejects_late_answer(self):
        session, children, _, gates = await self.held_tree(1)
        child = children[0]
        question = session.subagents.send_message(child.task_id, session.task_id, kind="question", message="question")
        await child.cancel()
        gates[0].release.set()
        await self.result(child)
        result = session.subagents.reply_message(session.task_id, request_id=question["request_id"], message="late")
        self.assertEqual(result["reason"], "request_closed")
        self.assertEqual(result["request_status"], "requester_terminated")

    async def test_questions_and_notify_do_not_discard_llm_response(self):
        for kind in ("question", "notify"):
            with self.subTest(kind=kind):
                gate = self.gate(calls(("memo_write", {"content": "original decision", "kind": "fact"})))
                def answer(request):
                    data = incoming(request, kind)[0]
                    if kind == "question":
                        return calls(("agent_reply", {"request_id": data["request_id"], "message": "answer"}))
                    return calls(("parent_notify", {"message": "received information"}))
                child_provider = FakeProvider(gate, answer, complete())
                parent_gate = self.gate(wait_children)
                async def parent_next(request):
                    await parent_gate(request)
                    return wait_children(request)
                parent = FakeProvider(calls(spawn()), parent_next, wait_children, read_children, complete())
                session = await self.start_tree(parent, {"child": profile(child_provider)})
                await gate.entered.wait()
                child = session.subagents.child((await session.subagents.children())[0]["task_id"])
                value = session.subagents.send_message(session.task_id, child.task_id, kind=kind, message="incoming")
                self.assertEqual((await child.state()).agent.activity, AgentActivity.LLM_CALL)
                self.assertEqual(len(child_provider.requests), 1)
                self.assertFalse(any(e.type == "agent_message_applied" for e in (await child.trace()).events))
                gate.release.set()
                await self.result(child)
                parent_gate.release.set()
                await self.result(session)
                child_result = await child.result()
                self.assertEqual(child_result.status, TaskStatus.COMPLETED, child_result.warnings)
                self.assertEqual(child_result.memos[0].content, "original decision")
                self.assertFalse(any(e.type == "response_discarded" for e in child_result.tracer.events))
                self.assert_balanced(child_result.messages)
                self.assertEqual(incoming(child_provider.requests[1], kind)[0]["message_id"], value["message_id"])

    async def test_parent_instruction_uses_revision_rules_and_preserves_origin(self):
        old = self.gate(complete("obsolete"))
        def revised(request):
            data = incoming(request, "instruction")[0]
            self.assertEqual(data["content"], "new direction")
            self.assertEqual(data["direction"], "received")
            return complete("revised")
        provider = FakeProvider(old, revised)
        parent_gate = self.gate(complete())
        session = await self.start_tree(FakeProvider(parent_gate), {"child": profile(provider)})
        await parent_gate.entered.wait()
        child_id = (await session.subagents.spawn(session.task_id, **spawn()[1]))["task_id"]
        child = session.subagents.child(child_id)
        await old.entered.wait()
        sent = session.subagents.send_message(session.task_id, child_id, kind="instruction", message="new direction")
        self.assertEqual((await child.state()).agent.activity, AgentActivity.LLM_CALL)
        old.release.set()
        result = await self.result(child)
        self.assertEqual(result.output, "revised")
        discarded = next(e for e in result.tracer.events if e.type == "response_discarded")
        self.assertEqual(discarded.payload["reason"], "new_parent_instruction")
        message = incoming(provider.requests[-1], "instruction")[0]
        self.assertEqual(message["from_task_id"], session.task_id)
        self.assertEqual(message["message_id"], sent["message_id"])

    async def test_parent_instruction_during_tool_keeps_batch_balanced_and_skips_tail(self):
        slow = self.gate("effect done")
        def revised(request):
            results = outputs(request, "memo_write")
            self.assertFalse(results[0]["ok"])
            self.assertEqual(results[0]["error"]["message"], "superseded_by_parent_instruction")
            self.assert_balanced(request["messages"])
            return complete("changed")
        child_provider = FakeProvider(calls(("slow", {}), ("memo_write", {"content": "stale", "kind": "fact"})), revised)
        parent_gate = self.gate(complete())
        session = await self.start_tree(FakeProvider(parent_gate), {"child": profile(child_provider, registry_for(slow=slow))})
        await parent_gate.entered.wait()
        child_id = (await session.subagents.spawn(session.task_id, **spawn()[1]))["task_id"]
        child = session.subagents.child(child_id)
        await slow.entered.wait()
        session.subagents.send_message(session.task_id, child_id, kind="instruction", message="revise")
        self.assertFalse(child.done)
        slow.release.set()
        result = await self.result(child)
        self.assertEqual(result.output, "changed")
        self.assertFalse(result.memos)
        self.assert_balanced(result.messages)

    async def test_tool_wait_reply_handles_new_request_then_resumes_original(self):
        a = FakeProvider(calls(("parent_ask", {"message": "original"})), wait_own,
            lambda r: calls(("agent_reply", {"request_id": incoming(r, "question")[0]["request_id"], "message": "child reply"})),
            wait_own, complete("child done"))
        def ask_child(request):
            return calls(("subagent_ask", {"task_id": child_ids(request)[0], "message": "new question"}))
        def answer_original(request):
            self.assertEqual(outputs(request, "agent_wait_reply")[-1]["output"]["reason"], "answered")
            return calls(("agent_reply", {"request_id": incoming(request, "question")[0]["request_id"], "message": "original reply"}))
        parent = FakeProvider(calls(spawn()), wait_children, ask_child,
                              lambda r: wait_own(r, "subagent_ask"), answer_original, wait_children, complete())
        session = await self.start_tree(parent, {"child": profile(a)})
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.COMPLETED, result.warnings)
        child = session.subagents.child((await session.subagents.children())[0]["task_id"])
        child_result = await child.result()
        self.assertEqual(child_result.status, TaskStatus.COMPLETED, child_result.warnings)
        waits = outputs({"messages": child_result.messages}, "agent_wait_reply")
        self.assertEqual([w["output"]["reason"] for w in waits], ["incoming_request", "answered"])
        self.assertEqual(waits[0]["output"]["request_id"], waits[1]["output"]["request_id"])
        self.assert_balanced(child_result.messages)

    async def test_host_instruction_releases_reply_wait_without_cancelling_request(self):
        child = FakeProvider(calls(("parent_ask", {"message": "pending"})), wait_own, complete("revised"))
        parent_gate = self.gate(complete())
        session = await self.start_tree(FakeProvider(parent_gate), {"child": profile(child)})
        await parent_gate.entered.wait()
        child_id = (await session.subagents.spawn(session.task_id, **spawn()[1]))["task_id"]
        child_session = session.subagents.child(child_id)
        started = await event_of(child_session, "agent_wait_started")
        state = await child_session.state()
        self.assertEqual(state.agent.activity, AgentActivity.WAITING_FOR_PARENT_REPLY)
        self.assertEqual(state.agent.waiting_request_id, started.payload["request_id"])
        self.assertEqual(state.agent.waiting_task_ids, (session.task_id,))
        await child_session.send_instruction("continue without waiting")
        result = await self.result(child_session)
        wait = outputs({"messages": result.messages}, "agent_wait_reply")[0]["output"]
        self.assertEqual(wait["reason"], "superseded_by_host_instruction")
        self.assertTrue(wait["pending"])
        self.assertEqual(result.status, TaskStatus.COMPLETED)

    async def test_host_cancel_releases_reply_wait(self):
        child = FakeProvider(calls(("parent_ask", {"message": "pending"})), wait_own)
        parent_gate = self.gate(complete())
        session = await self.start_tree(FakeProvider(parent_gate), {"child": profile(child)})
        await parent_gate.entered.wait()
        child_id = (await session.subagents.spawn(session.task_id, **spawn()[1]))["task_id"]
        child_session = session.subagents.child(child_id)
        await event_of(child_session, "agent_wait_started")
        await child_session.cancel()
        result = await self.result(child_session)
        self.assertEqual(result.status, TaskStatus.CANCELLED)
        wait = outputs({"messages": result.messages}, "agent_wait_reply")[0]["output"]
        self.assertEqual(wait["reason"], "host_cancelled")
        self.assert_balanced(result.messages)

    async def test_host_question_stays_open_while_answering_child_and_late_host_answer_applies(self):
        child_start = self.gate(calls(("parent_ask", {"message": "child needs help"})))
        child = FakeProvider(child_start, wait_own, complete("child done"))
        parent_work = self.gate("parent continued while Host thought")
        def answer_child(request):
            released = outputs(request, "host_ask")[-1]["output"]
            self.assertTrue(released["pending"])
            return calls(("agent_reply", {"request_id": incoming(request, "question")[0]["request_id"],
                                           "message": "parent knows"}), ("work", {}))
        def finish(request):
            answers = [json.loads(m["content"].split("\n", 1)[1]) for m in request["messages"]
                       if m.get("content", "").startswith("Host answer:")]
            self.assertEqual(len(answers), 1)
            self.assertEqual(answers[0]["content"], "Host decision")
            return complete("all done")
        parent = FakeProvider(calls(spawn()), calls(("host_ask", {"question": "Host decision?"})),
                              answer_child, wait_children, finish)
        session = await self.start_tree(parent, {"child": profile(child)}, registry=registry_for(work=parent_work))
        host_question = await event_of(session, "question")
        await child_start.entered.wait()
        child_start.release.set()
        await parent_work.entered.wait()
        state = await session.state()
        question_id = host_question.payload["question_id"]
        self.assertEqual(state.agent.question_id, question_id)
        self.assertEqual(state.agent.activity, AgentActivity.TOOL_EXECUTION)
        self.assertEqual(state.task.status, TaskStatus.RUNNING)
        self.assertFalse(any(e.type == "question_closed" for e in (await session.trace()).events))
        await session.answer(question_id, "Host decision")
        await event_of(session, "question_closed")
        self.assertFalse(any(e.type == "host_answer_applied" for e in (await session.trace()).events))
        parent_work.release.set()
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.COMPLETED, result.warnings)
        self.assertEqual(result.tool_errors, 0)
        self.assert_balanced(result.messages)
        applied = next(e for e in result.tracer.events if e.type == "host_answer_applied")
        self.assertEqual(applied.payload["question_id"], question_id)
        self.assertEqual(len([e for e in result.tracer.events if e.type == "question"]), 1)

    async def test_pending_host_question_can_resume_same_wait_and_rejects_completion(self):
        child_start = self.gate(calls(("parent_notify", {"message": "information"})))
        child = FakeProvider(child_start, complete())
        parent = FakeProvider(calls(spawn()), calls(("host_ask", {"question": "choose"})),
                              complete("premature"), calls(("host_ask", {"question": "choose"})),
                              wait_children, complete("after Host"))
        session = await self.start_tree(parent, {"child": profile(child)})
        question = await event_of(session, "question")
        child_start.release.set()
        await event_of(session, "tool_started", lambda e: e.payload["name"] == "host_ask" and
                       e.sequence > question.sequence)
        self.assertEqual((await session.state()).agent.question_id, question.payload["question_id"])
        await session.answer(question.payload["question_id"], "answer")
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.COMPLETED, result.warnings)
        self.assertEqual(result.tool_errors, 1)
        self.assertEqual(len([e for e in result.tracer.events if e.type == "question"]), 1)
        self.assertEqual(outputs({"messages": result.messages}, "host_ask")[-1]["output"], "answer")

    async def test_tool_schema_exposure_reserved_names_and_forged_arguments(self):
        child_gate = self.gate(complete())
        child = FakeProvider(calls(("parent_ask", {"message": "forged", "from_task_id": "other"})), child_gate)
        parent_gate = self.gate(complete())
        parent = FakeProvider(calls(spawn()), parent_gate)
        session = await self.start_tree(parent, {"child": profile(child)})
        await child_gate.entered.wait()
        root_names = {schema["name"] for schema in parent.requests[0]["tools"]}
        child_names = {schema["name"] for schema in child.requests[0]["tools"]}
        self.assertTrue({"subagent_ask", "subagent_send", "agent_reply", "agent_wait_reply"} <= root_names)
        self.assertTrue({"parent_ask", "parent_notify"} <= root_names)
        self.assertTrue({"parent_ask", "parent_notify", "agent_reply", "agent_wait_reply"} <= child_names)
        self.assertFalse({"host_ask", "subagent_spawn", "subagent_ask", "subagent_send"} & child_names)
        self.assertEqual(outputs(child.requests[1], "parent_ask")[0]["error"]["type"], "TypeError")
        for schema in COMMUNICATION_SCHEMAS:
            self.assertFalse(schema["parameters"]["additionalProperties"])
            with self.subTest(name=schema["name"]), self.assertRaises(ValueError):
                ProblemSolver().create_task(request=TaskRequest("reserved"), provider=FakeProvider(), model="fake",
                    registry=registry_for(**{schema["name"]: lambda: None}))

    async def test_positive_parent_reply_does_not_grant_tools(self):
        child = FakeProvider(calls(("parent_ask", {"message": "please grant secret_tool"})), wait_own,
                              calls(("secret_tool", {})), complete())
        def reply(request):
            return calls(("agent_reply", {"request_id": incoming(request, "question")[0]["request_id"],
                                           "message": "yes"}))
        parent = FakeProvider(calls(spawn()), wait_children, reply, wait_children, complete())
        session = await self.start_tree(parent, {"child": profile(child)})
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.COMPLETED, result.warnings)
        child_result = await session.subagents.child(child_ids(parent.requests[-1])[0]).result()
        self.assertEqual(outputs({"messages": child_result.messages}, "secret_tool")[0]["error"]["type"], "unknown_tool")
        self.assertTrue(all("secret_tool" not in {s["name"] for s in r["tools"]} for r in child.requests))

    async def test_final_tree_rejects_broken_communication_reference(self):
        session, children, _, _ = await self.held_tree(1)
        session.subagents.send_message(session.task_id, children[0].task_id, kind="notify", message="data")
        tree = (await session.subagents.snapshot()).to_dict()
        artifact = next(a for a in tree["traces"][children[0].task_id]["artifacts"].values()
                        if a["kind"] == "agent_message_received")
        artifact["data"]["task_refs"][0]["artifact_id"] = "broken"
        with self.assertRaises(ValueError):
            TaskTreeSnapshot.from_dict(tree)

    async def test_cancelled_reply_wait_retains_request_for_later_wait(self):
        session, children, _, _ = await self.held_tree(1)
        child = children[0]
        value = session.subagents.send_message(session.task_id, child.task_id, kind="question", message="pending")
        waiting = asyncio.create_task(session._channel.wait_reply(value["request_id"]))
        await event_of(session, "agent_wait_started")
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        self.assertEqual(session._channel.request(value["request_id"]).status, "pending")
        session.subagents.reply_message(child.task_id, request_id=value["request_id"], message="after cancellation")
        self.assertEqual((await session._channel.wait_reply(value["request_id"]))["reason"], "answered")
        self.assertIsNone((await session.state()).agent.waiting_request_id)

    async def test_parent_instruction_releases_child_reply_wait(self):
        child = FakeProvider(calls(("parent_ask", {"message": "need info"})), wait_own, complete("changed"))
        parent_gate = self.gate(complete())
        session = await self.start_tree(FakeProvider(parent_gate), {"child": profile(child)})
        await parent_gate.entered.wait()
        child_id = (await session.subagents.spawn(session.task_id, **spawn()[1]))["task_id"]
        child_session = session.subagents.child(child_id)
        await event_of(child_session, "agent_wait_started")
        session.subagents.send_message(session.task_id, child_id, kind="instruction", message="new task direction")
        result = await self.result(child_session)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual(outputs({"messages": result.messages}, "agent_wait_reply")[0]["output"]["reason"],
                         "superseded_by_parent_instruction")
        self.assert_balanced(result.messages)

    async def test_reply_and_notify_sent_in_batch_record_after_all_results(self):
        child = FakeProvider(calls(("parent_notify", {"message": "one"}), ("parent_notify", {"message": "two"})), complete())
        parent = FakeProvider(calls(spawn()), wait_children, wait_children, complete())
        session = await self.start_tree(parent, {"child": profile(child)})
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.COMPLETED, result.warnings)
        child_result = await session.subagents.child(child_ids(parent.requests[-1])[0]).result()
        self.assert_balanced(child_result.messages)
        sent = [m for m in child_result.messages if m.get("content", "").startswith("Agent communication")]
        self.assertEqual(len(sent), 2)
        self.assertEqual([m["role"] for m in sent], ["assistant", "assistant"])
        self.assertEqual([m["content"] for m in incoming(parent.requests[-1], "notify")], ["one", "two"])

    async def test_host_answer_releases_subagent_wait_and_preserves_host_id(self):
        child_start = self.gate(calls(("parent_notify", {"message": "release host wait"})))
        child_work = self.gate(complete())
        child = FakeProvider(child_start, child_work)
        parent = FakeProvider(calls(spawn()), calls(("host_ask", {"question": "choose"})),
                              wait_children, wait_children, complete())
        session = await self.start_tree(parent, {"child": profile(child)})
        question = await event_of(session, "question")
        child_start.release.set()
        await event_of(session, "subagent_wait_started")
        await child_work.entered.wait()
        await session.answer(question.payload["question_id"], "chosen")
        released = await event_of(session, "subagent_wait_finished")
        detail = await session.artifact(released.artifact_ids[0])
        self.assertEqual(detail.data["reason"], "host_answer")
        await event_of(session, "host_answer_applied")
        child_work.release.set()
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.COMPLETED, result.warnings)
        self.assert_balanced(result.messages)

    async def test_detached_host_timeout_preserves_inflight_tool_until_safe_boundary(self):
        # Fire the existing Host deadline explicitly; no sleep/timing inference.
        loop = asyncio.get_running_loop()
        call_later = loop.call_later
        deadlines = []
        def observe_timer(delay, callback, *args, **kwargs):
            if delay == 123:
                deadlines.append(lambda: callback(*args))
            return call_later(delay, callback, *args, **kwargs)
        child_start = self.gate(calls(("parent_notify", {"message": "release host wait"})))
        parent_work = self.gate("external effect finished")
        child = FakeProvider(child_start, complete())
        parent = FakeProvider(calls(spawn()), calls(("host_ask", {"question": "choose"})),
                              calls(("work", {}), ("memo_write", {"content": "must skip", "kind": "fact"})))
        with patch.object(loop, "call_later", side_effect=observe_timer):
            session = await self.start_tree(parent, {"child": profile(child)},
                registry=registry_for(work=parent_work), limits=AgentLimits(host_response_timeout=123,
                    max_total_subagents=1, max_subagent_depth=1, context_compact_threshold_chars=None))
            await event_of(session, "question")
            child_start.release.set()
            await parent_work.entered.wait()
            self.assertEqual(len(deadlines), 1)
            deadlines[0]()
            await event_of(session, "question_closed")
            self.assertFalse(session.done)
            self.assertEqual((await session.state()).agent.activity, AgentActivity.TOOL_EXECUTION)
            parent_work.release.set()
            result = await self.result(session)
        self.assertEqual((result.status, result.stop_reason), (TaskStatus.INCOMPLETE, "host_response_timeout"))
        self.assertFalse(result.memos)
        self.assertEqual(outputs({"messages": result.messages}, "work")[0]["output"], "external effect finished")
        self.assertFalse(outputs({"messages": result.messages}, "memo_write")[0]["ok"])
        self.assert_balanced(result.messages)

    async def test_host_cancel_after_suspended_question_closes_without_fabricated_answer(self):
        child_start = self.gate(calls(("parent_notify", {"message": "release"})))
        parent_work = self.gate("done")
        session = await self.start_tree(FakeProvider(calls(spawn()), calls(("host_ask", {"question": "pick"})),
            calls(("work", {}))), {"child": profile(FakeProvider(child_start, complete()))},
            registry=registry_for(work=parent_work))
        await event_of(session, "question")
        child_start.release.set()
        await parent_work.entered.wait()
        await session.cancel()
        await event_of(session, "command_applied", lambda e: e.payload["command_type"] == "cancel")
        parent_work.release.set()
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.CANCELLED)
        self.assertFalse(any(e.type == "host_answer_applied" for e in result.tracer.events))
        closed = next(e for e in result.tracer.events if e.type == "question_closed")
        self.assertIn(closed.payload["reason"], {"host_cancelled", "task_terminated"})
        self.assert_balanced(result.messages)

    async def test_single_task_has_parent_tools_without_unused_communication_events(self):
        provider = FakeProvider(complete())
        result = await self.result(await self.start(provider))
        names = {s["name"] for s in provider.requests[0]["tools"]}
        self.assertEqual(names & {s["name"] for s in COMMUNICATION_SCHEMAS},
                         {"parent_ask", "parent_notify", "agent_reply", "agent_wait_reply"})
        self.assertFalse(any(e.type.startswith("agent_message_") for e in result.tracer.events))
        self.assertEqual(result.status, TaskStatus.COMPLETED)


class CommunicationContextTests(unittest.TestCase):
    def test_compaction_preserves_unseen_messages_and_pending_request_ids_without_memos(self):
        limits = AgentLimits()
        context = TaskContext("child", TaskRequest("task"), limits)
        kinds = ("question", "notify", "reply", "instruction")
        indices = []
        for i, kind in enumerate(kinds):
            value = AgentMessage(f"m{i}", f"q{i}" if kind in {"question", "reply"} else None,
                                 "parent", "child", kind, kind + " content")
            indices.append(context.conversation.record_agent_message(value))
        snapshot = replace(context.snapshot(), summary=Summary("summary omitted IDs", len(context.conversation)))
        builder = ContextBuilder(limits)
        built = builder.build(snapshot)
        self.assertEqual([m["kind"] for m in incoming({"messages": built})], list(kinds))
        self.assertEqual(snapshot, replace(context.snapshot(), summary=snapshot.summary))
        context.conversation.mark_agent_messages_seen(indices)
        snapshot = replace(context.snapshot(), summary=snapshot.summary)
        self.assertEqual([m["kind"] for m in incoming({"messages": builder.build(snapshot)})],
                         ["question", "instruction"])
        context.conversation.resolve_agent_request("q0")
        snapshot = replace(context.snapshot(), summary=snapshot.summary)
        self.assertEqual([m["kind"] for m in incoming({"messages": builder.build(snapshot)})], ["instruction"])
        self.assertFalse(context.memo.snapshot())

    def test_outgoing_question_survives_compaction_until_answered(self):
        limits = AgentLimits()
        context = TaskContext("child", TaskRequest("task"), limits)
        context.conversation.record_agent_message(AgentMessage("m", "q", "child", "parent", "question", "need info"),
                                                  outgoing=True)
        snapshot = replace(context.snapshot(), summary=Summary("summary", len(context.conversation)))
        built = ContextBuilder(limits).build(snapshot)
        self.assertEqual(built[-1]["role"], "assistant")
        self.assertIn('"request_id": "q"', built[-1]["content"])
        context.conversation.resolve_agent_request("q")
        snapshot = replace(context.snapshot(), summary=snapshot.summary)
        self.assertFalse(any(m.get("content", "").startswith("Agent communication")
                             for m in ContextBuilder(limits).build(snapshot)))
