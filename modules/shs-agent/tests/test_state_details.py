import asyncio
import gc
import json
import unittest
import weakref
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from shs_agent import (AgentActivity, AgentLimits, ProblemSolver, SessionState,
                       TaskStatus, TracerSnapshot)
from shs_agent.agent.tracer.serialization import capture
from shs_agent.llm import ChatOptions, LLMResponse
from shs_agent.llm.observation import fields, observe

from tests.fakes import AsyncCase, FakeProvider, calls, complete, event_of, registry_for


def artifacts(snapshot, kind):
    return [a for a in snapshot.artifacts.values() if a.kind == kind]


def events(snapshot, kind):
    return [e for e in snapshot.events if e.type == kind]


class StateDetailTests(AsyncCase):
    async def test_live_state_cursor_and_artifacts_replay_without_gaps(self):
        gate = self.gate(complete())
        session = await self.start(FakeProvider(gate))
        await gate.entered.wait()
        state = await session.state()
        self.assertEqual(state.task.status, session.status)
        self.assertEqual(state.agent.activity, AgentActivity.LLM_CALL)
        self.assertEqual(state.agent.steps, 1)
        self.assertEqual(state.agent.operation_kind, "llm")
        request = await session.artifact(state.agent.operation_artifact_id)
        self.assertEqual(request.operation_id, state.agent.operation_id)
        self.assertEqual(request.data["model"], "fake")
        self.assertEqual(request.data["messages"][-1]["content"], "test task")
        request.data["messages"].clear()
        self.assertTrue((await session.artifact(request.artifact_id)).data["messages"])
        with self.assertRaises(FrozenInstanceError):
            state.agent.steps = 55
        edited = state.to_dict()
        edited["agent"]["steps"] = 55
        self.assertEqual((await session.state()).agent.steps, 1)
        observed = []

        async def consume():
            suffix = []
            async for event in session.events(after_sequence=state.last_event_sequence):
                for artifact_id in event.artifact_ids:
                    self.assertEqual((await session.artifact(artifact_id)).task_id, session.task_id)
                suffix.append(event)
                observed.append(await session.state())
            return suffix

        reader = asyncio.create_task(consume())
        gate.release.set()
        result = await self.result(session)
        self.assertEqual(tuple(await reader), result.tracer.events[state.last_event_sequence:])
        for sample in [state, *observed, await session.state()]:
            self.assertEqual(result.tracer.state(at_sequence=sample.last_event_sequence), sample)
        for event in result.tracer.events:
            if event.type in {"created", "state_changed", "status_changed", "finished"}:
                self.assertEqual(SessionState.from_dict(event.payload["state"]),
                                 result.tracer.state(at_sequence=event.sequence))
        final = await session.state()
        self.assertEqual(final.agent.activity, AgentActivity.FINISHED)
        self.assertIsNone(final.agent.operation_id)
        self.assertIsNotNone(final.task.started_at)
        self.assertIsNotNone(final.task.finished_at)
        self.assertEqual(final.task.stop_reason, result.stop_reason)
        with self.assertRaises(KeyError):
            await session.artifact("unknown")

    async def test_state_query_does_not_copy_full_tracer(self):
        gate = self.gate(complete())
        session = await self.start(FakeProvider(gate))
        await gate.entered.wait()
        with patch("shs_agent.agent.tracer.Tracer.snapshot", side_effect=AssertionError("full copy")):
            state = await session.state()
        self.assertFalse(hasattr(state.agent, "messages"))
        self.assertFalse(hasattr(state.task, "memos"))
        gate.release.set()
        await self.result(session)

    async def test_cancel_requested_and_running_tool_coexist_then_skip_unstarted_call(self):
        gate = self.gate({"output": "effect finished"})
        session = await self.start(FakeProvider(calls(("slow", {}), ("later", {}))),
                                   registry=registry_for(slow=gate, later=lambda: self.fail("must not run")))
        await gate.entered.wait()
        active = await session.state()
        await session.cancel()
        received = await session.state()
        self.assertTrue(received.task.cancel_requested)
        self.assertEqual(received.agent.activity, AgentActivity.TOOL_EXECUTION)
        self.assertEqual(received.agent.operation_id, active.agent.operation_id)
        await event_of(session, "status_changed", lambda e: e.payload["status"] == "cancelling")
        cancelling = await session.state()
        self.assertEqual(cancelling.task.status, TaskStatus.CANCELLING)
        self.assertEqual(cancelling.agent.activity, AgentActivity.TOOL_EXECUTION)
        gate.release.set()
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.CANCELLED)
        self.assertEqual(result.tracer.state(at_sequence=received.last_event_sequence), received)
        outcomes = artifacts(result.tracer, "tool_result")
        self.assertEqual([a.data["status"] for a in outcomes], ["succeeded", "skipped"])
        self.assertIsNone(outcomes[1].data["raw_result_artifact_id"])
        self.assertIsNone(outcomes[1].data["dispatch_artifact_id"])
        self.assertEqual(len(artifacts(result.tracer, "tool_request")), 2)

    async def test_host_wait_answer_instruction_and_memo_have_self_contained_sources(self):
        gate = self.gate(complete("discarded"))
        provider = FakeProvider(calls(("host_ask", {"question": "color?", "context": "pick one"})), gate,
                                calls(("memo_write", {"content": "blue remembered", "kind": "fact"})), complete())
        session = await self.start(provider)
        question = await event_of(session, "question")
        state = await session.state()
        self.assertEqual(state.task.status, TaskStatus.WAITING_FOR_HOST)
        self.assertEqual(state.agent.activity, AgentActivity.WAITING_FOR_HOST)
        self.assertEqual(state.agent.waiting_for, "host")
        self.assertEqual(state.agent.question_id, question.payload["question_id"])
        self.assertEqual(state.agent.operation_id, question.payload["tool_ref"]["tool_execution_id"])
        await session.answer(state.agent.question_id, "blue")
        await gate.entered.wait()
        await session.send_instruction("remember blue")
        gate.release.set()
        result = await self.result(session)
        snapshot = TracerSnapshot.from_json(result.tracer.to_json())
        self.assertEqual(snapshot.state(at_sequence=state.last_event_sequence), state)
        discarded = events(snapshot, "response_discarded")[0]
        self.assertEqual(discarded.payload["reason"], "new_host_instruction")
        self.assertIn("discarded", json.dumps([snapshot.artifact(key).data for key in discarded.artifact_ids]))
        context = snapshot.artifact(snapshot.events[-1].payload["context_artifact_id"])
        self.assertEqual(context.data["memos"][0]["content"], "blue remembered")
        self.assertIn("remember blue", json.dumps(context.data["messages"]))
        self.assertIn("blue", json.dumps([e.payload for e in events(snapshot, "command_received")]))
        self.assertEqual(artifacts(snapshot, "task_input")[0].data["instruction"], "test task")

    async def test_thinking_content_calls_and_past_requests_survive_external_mutations(self):
        response = calls(("echo", {"nested": {"value": "original"}}), content="visible content")
        response.thinking = "disclosed thinking"
        response.usage = {"custom": {"provider_value": "original usage"}}
        gate = self.gate(complete())
        options = ChatOptions(temperature=0.4, extra={"options": {"top_k": 12}})
        provider = FakeProvider(response, gate)

        def mutate(nested):
            nested["value"] = "executor edit"
            return {"nested": nested}

        session = await self.start(provider, registry=registry_for(echo=mutate), options=options)
        await gate.entered.wait()
        options.extra["options"]["top_k"] = 99
        response.content = "external edit"
        response.thinking = "external edit"
        response.tool_calls[0].arguments["nested"]["value"] = "external edit"
        response.usage["custom"]["provider_value"] = "external edit"
        provider.requests[0]["messages"].clear()
        gate.release.set()
        result = await self.result(session)
        snapshot = result.tracer
        observed = artifacts(snapshot, "llm_common_response")[0].data["fields"]
        self.assertEqual(observed["content"], "visible content")
        self.assertEqual(observed["thinking"], "disclosed thinking")
        self.assertEqual(observed["tool_calls"][0]["arguments"], {"nested": {"value": "original"}})
        self.assertEqual(observed["usage"], {"custom": {"provider_value": "original usage"}})
        request = artifacts(snapshot, "llm_common_request")[0].data
        self.assertTrue(request["messages"])
        self.assertEqual(request["options"]["extra"]["options"]["top_k"], 12)
        self.assertTrue(request["tools"])
        self.assertEqual(artifacts(snapshot, "tool_dispatch")[0].data["passed_arguments"],
                         {"nested": {"value": "original"}})
        self.assertEqual(artifacts(snapshot, "tool_raw_result")[0].data["output"],
                         {"nested": {"value": "executor edit"}})

    async def test_compaction_activity_and_all_intermediate_summaries_are_exported(self):
        gate = self.gate(LLMResponse("first applied summary"))

        class Provider:
            decisions = 0
            summaries = 0

            async def chat(inner, **request):
                if not request["tools"]:
                    inner.summaries += 1
                    return await gate() if inner.summaries == 1 else LLMResponse("second applied summary")
                inner.decisions += 1
                return complete() if inner.decisions == 4 else LLMResponse("old context " * 50)

        session = await self.start(Provider(), limits=AgentLimits(context_compact_threshold_chars=256,
                                                                 context_keep_recent_messages=1))
        await gate.entered.wait()
        state = await session.state()
        self.assertEqual(state.agent.activity, AgentActivity.CONTEXT_COMPACTION)
        request = await session.artifact(state.agent.operation_artifact_id)
        self.assertEqual(request.data["tools"], [])
        self.assertIn("old context", json.dumps(request.data["messages"]))
        gate.release.set()
        result = await self.result(session)
        snapshot = TracerSnapshot.from_json(result.tracer.to_json())
        self.assertEqual(snapshot.state(at_sequence=state.last_event_sequence), state)
        self.assertEqual([a.data["summary"]["content"] for a in artifacts(snapshot, "context_summary")],
                         ["first applied summary", "second applied summary"])
        self.assertEqual(result.context_summary, "second applied summary")
        self.assertIn("first applied summary", json.dumps(artifacts(snapshot, "llm_common_request")[-2].data))
        self.assertEqual(len(events(snapshot, "context_built")), 6)

    async def test_receipt_validation_application_and_missing_responses_are_distinct(self):
        invalid = SimpleNamespace(content="received invalid", thinking="kept", tool_calls=[
            SimpleNamespace(id="x", name="missing", arguments="not a dict")], finish_reason="stop", usage=None)
        for response, received, normalized, discarded in (
            (RuntimeError("provider failed"), False, False, None),
            (invalid, True, False, None),
            (LLMResponse("", thinking="only thinking"), True, True, "empty_response"),
            (LLMResponse("partial", finish_reason="length"), True, True, "incomplete_response"),
        ):
            with self.subTest(response=response):
                session = await self.start(FakeProvider(response))
                result = await self.result(session)
                ended = events(result.tracer, "llm_call_finished")[0].payload
                self.assertEqual(ended["response_received"], received)
                self.assertEqual(ended["validation_succeeded"], normalized)
                self.assertEqual(bool(artifacts(result.tracer, "llm_common_response")), received)
                self.assertFalse(events(result.tracer, "response_recorded"))
                self.assertFalse(events(result.tracer, "tool_started"))
                if discarded:
                    self.assertEqual(events(result.tracer, "response_discarded")[0].payload["reason"], discarded)
                if response is invalid:
                    self.assertEqual(artifacts(result.tracer, "llm_common_response")[0].data["fields"]["thinking"], "kept")
                self.assertTrue(result.tracer.complete)
                self.assertEqual(result.tracer.state().task.status, result.status)

    async def test_cancelled_llm_receipt_is_preserved_without_execution(self):
        response = complete("unused")
        response.thinking = "received after cancellation"
        gate = self.gate(response)
        session = await self.start(FakeProvider(gate))
        await gate.entered.wait()
        await session.cancel()
        gate.release.set()
        result = await self.result(session)
        snapshot = result.tracer
        self.assertEqual(result.status, TaskStatus.CANCELLED)
        self.assertEqual(artifacts(snapshot, "llm_common_response")[0].data["fields"]["thinking"], response.thinking)
        self.assertFalse(artifacts(snapshot, "tool_dispatch"))
        self.assertEqual(events(snapshot, "response_discarded")[0].payload["reason"], "host_cancelled")

    async def test_large_raw_tool_tail_serialization_failures_and_exception_distinctions(self):
        big = "prefix" * 8000 + "preserved-tail"
        cycle = []
        cycle.append(cycle)

        def fail():
            raise ValueError("real exception")

        session = await self.start(FakeProvider(calls(("big", {}), ("cycle", {}), ("fail", {})), complete()),
                                   registry=registry_for(big=lambda: big, cycle=lambda: cycle, fail=fail),
                                   limits=AgentLimits(max_tool_output_chars=256))
        snapshot = (await self.result(session)).tracer
        restored = TracerSnapshot.from_json(snapshot.to_json())
        outcomes = artifacts(restored, "tool_result")
        self.assertEqual(restored.artifact(outcomes[0].data["raw_result_artifact_id"]).data["output"], big)
        self.assertNotIn("preserved-tail", json.dumps(outcomes[0].data["llm_result"]))
        self.assertEqual(outcomes[1].data["error"]["type"], "serialization_error")
        cyclic = restored.artifact(outcomes[1].data["raw_result_artifact_id"])
        self.assertFalse(cyclic.details_complete)
        self.assertEqual(cyclic.issues[0]["reason"], "cyclic_reference")
        self.assertEqual(outcomes[2].data["error"], {"type": "ValueError", "message": "real exception"})
        self.assertIsNone(outcomes[2].data["raw_result_artifact_id"])
        self.assertTrue(restored.complete)
        self.assertFalse(restored.details_complete)

    async def test_export_is_self_contained_and_independent_without_subscribers_or_session(self):
        session = await ProblemSolver(FakeProvider(complete())).start("standalone input", model="fake")
        result = await session.result()
        original = result.tracer.to_json()
        for message in result.messages:
            message.clear()
        snapshot = TracerSnapshot.from_json(original)
        for event in snapshot.events:
            for reference in event.artifact_ids:
                self.assertEqual(snapshot.artifact(reference).task_id, snapshot.task_id)
        for artifact in snapshot.artifacts.values():
            for reference in artifact.references:
                self.assertIn(reference, snapshot.artifacts)
        self.assertTrue(snapshot.artifact(snapshot.events[-1].payload["context_artifact_id"]).data["messages"])
        first = artifacts(result.tracer, "llm_common_request")[0]
        first.data["messages"].clear()
        self.assertEqual((await session.result()).tracer.to_json(), original)
        reference = weakref.ref(session)
        del session
        gc.collect()
        self.assertIsNone(reference())
        self.assertEqual(snapshot.to_json(), original)

    async def test_shared_sync_provider_hooks_are_isolated_across_concurrent_tasks(self):
        class Shared:
            def chat(self, **request):
                instruction = request["messages"][-1]["content"]
                observe("adapter_request", {"body": instruction})
                observe("adapter_response", {"body": instruction})
                return complete(instruction)

        solver = ProblemSolver(Shared())
        a = await solver.start("first task", model="fake")
        b = await solver.start("second task", model="fake")
        self.sessions.extend((a, b))
        first, second = await asyncio.gather(self.result(a), self.result(b))
        for result, other in ((first, second), (second, first)):
            self.assertTrue(result.tracer.details_complete)
            self.assertEqual(artifacts(result.tracer, "llm_adapter_request")[0].data["body"], result.output)
            self.assertEqual(artifacts(result.tracer, "llm_adapter_response")[0].data["body"], result.output)
            self.assertNotIn(other.output, result.tracer.to_json())
            self.assertTrue(set(result.tracer.artifacts).isdisjoint(other.tracer.artifacts))

    async def test_limit_result_has_terminal_state_and_unexecuted_tool_detail(self):
        session = await self.start(FakeProvider(complete()), limits=AgentLimits(max_tool_calls=0))
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.LIMIT_REACHED)
        self.assertEqual(result.tracer.state().task.status, result.status)
        self.assertEqual(result.tracer.state().agent.tool_calls, 0)
        self.assertEqual(artifacts(result.tracer, "tool_result")[0].data["status"], "skipped")
        self.assertEqual(result.tracer.events[-1].type, "finished")


class DetailEncodingTests(unittest.TestCase):
    def test_projection_loss_is_distinct_from_literal_diagnostic_keys(self):
        literal = {"$unavailable": "user text", "$encoding": "also user text"}
        copied, issues = capture(literal)
        self.assertEqual(copied, literal)
        self.assertFalse(issues)

        class BrokenProperty:
            content = "received text"

            @property
            def thinking(self):
                raise ValueError("cannot read")

        value, issues = capture(fields(BrokenProperty(), ("content", "thinking")))
        self.assertEqual(value["content"], "received text")
        self.assertEqual(issues[0]["reason"], "attribute_error:ValueError")
        self.assertEqual(issues[0]["path"], "$.thinking")

    def test_supported_encodings_and_unavailable_values_are_explicit(self):
        value, issues = capture({"bytes": b"\x00\xff", "mapping": {1: "integer", "1": "string"},
                                 "time": datetime(2026, 1, 1, tzinfo=timezone.utc), "opaque": object()})
        json.dumps(value, allow_nan=False)
        self.assertEqual(value["bytes"]["data"], "AP8=")
        self.assertEqual(value["mapping"]["entries"], [[1, "integer"], ["1", "string"]])
        self.assertEqual(value["time"]["data"], "2026-01-01T00:00:00+00:00")
        self.assertEqual(issues[0]["path"], "$.opaque")
        self.assertEqual(issues[0]["category"], "unavailable")
        self.assertNotIn("object at", json.dumps(value))

    def test_credentials_are_excluded_without_masking_body_fields(self):
        value, issues = capture({"options": {"extra": {"request": {"extra_headers": {
            "Authorization": "never record", "X-API-Key": "secret", "X-Trace": "kept", 1: "malformed header"},
            "response_format": {"json_schema": {"properties": {"authorization": {"type": "string"}}}},
            "metadata": {"api_key": "application body"}},
            "extra_body": {"api_key": "body data"}}},
            "messages": [{"content": "Authorization: user supplied prose", "api_key": "body field"}],
            "tools": [{"parameters": {"properties": {"api_key": {"type": "string"}}}}]}, credentials=True)
        self.assertNotIn("never record", json.dumps(value))
        self.assertNotIn("secret", json.dumps(value))
        self.assertIn("body data", json.dumps(value))
        self.assertIn("body field", json.dumps(value))
        self.assertIn("application body", json.dumps(value))
        self.assertIn("authorization", value["options"]["extra"]["request"]["response_format"]["json_schema"]["properties"])
        self.assertIn("Authorization: user supplied prose", json.dumps(value))
        self.assertEqual(len(issues), 2)
        self.assertTrue(all(issue["category"] == "excluded" for issue in issues))

    def test_import_rejects_dangling_refs_and_cross_task_records(self):
        from shs_agent import Tracer
        tracer = Tracer("a")
        first = tracer.add_artifact("first", {"body": "first"})
        second = tracer.add_artifact("second", {"body": "second"}, references=(first,))
        tracer.record("sample", {}, artifact_ids=(second,))
        exported = tracer.snapshot().to_dict()
        self.assertEqual(TracerSnapshot.from_dict(exported).to_json(), tracer.snapshot().to_json())
        del exported["artifacts"][first]
        with self.assertRaisesRegex(ValueError, "missing artifact"):
            TracerSnapshot.from_dict(exported)
        exported = tracer.snapshot().to_dict()
        exported["events"][0]["task_id"] = "other"
        with self.assertRaisesRegex(ValueError, "inconsistent task"):
            TracerSnapshot.from_dict(exported)

