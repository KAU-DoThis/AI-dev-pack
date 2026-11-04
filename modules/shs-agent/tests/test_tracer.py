import asyncio
import gc
import json
import unittest
import weakref
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from shs_agent import (AgentEvent, AgentLimits, ProblemSolver, TaskRequest,
                       TaskResult, TaskStatus, Tracer, TracerSnapshot)
from shs_agent.agent.types import AgentEvent as LegacyEvent
from shs_agent.llm import LLMResponse

from tests.fakes import (AsyncCase, FakeProvider, calls, complete, event_of,
                         events_of, registry_for)


def selected(result, kind):
    return [event for event in result.tracer.events if event.type == kind]


class TracerModelTests(unittest.TestCase):
    def test_legacy_event_and_result_construction_remains_valid(self):
        self.assertIs(AgentEvent, LegacyEvent)
        event = AgentEvent("task", 1, "created", {})
        self.assertEqual(event.step, 0)
        self.assertEqual(event.event_id, "task:1")
        self.assertEqual(datetime.fromisoformat(event.timestamp).utcoffset(), timedelta(0))
        result = TaskResult("task", TaskStatus.COMPLETED, "done", "task_complete", 1, 1, 0)
        self.assertIsNone(result.tracer)  # Compatibility for manually constructed results.

    def test_sequence_not_wall_clock_orders_events_and_copies_are_independent(self):
        tracer = Tracer("task")
        payload = {"nested": {"values": [1]}}
        later = datetime(2026, 10, 17, tzinfo=timezone.utc)
        with patch("shs_agent.agent.tracer.models.datetime") as clock:
            clock.now.side_effect = [later, later - timedelta(seconds=3)]
            returned = tracer.record("first", payload)
            tracer.record("second", {}, step=1)
        payload["nested"]["values"].append(2)
        returned.payload["nested"]["values"].append(3)
        snapshot = tracer.snapshot()
        self.assertFalse(snapshot.complete)
        self.assertEqual([event.sequence for event in snapshot.events], [1, 2])
        self.assertGreater(snapshot.events[0].timestamp, snapshot.events[1].timestamp)
        exported = snapshot.to_dict()
        exported["events"][0]["payload"]["nested"]["values"].append(4)
        snapshot.events[0].payload["nested"]["values"].append(5)
        self.assertEqual(tracer.read(0).payload, {"nested": {"values": [1]}})
        tracer.close()
        self.assertTrue(tracer.snapshot().complete)
        self.assertEqual(json.loads(tracer.snapshot().to_json())["events"][0]["sequence"], 1)
        with self.assertRaises(RuntimeError):
            tracer.record("after_close", {})


class TracerRuntimeTests(AsyncCase):
    def assert_trace(self, result):
        trace = result.tracer
        self.assertIsInstance(trace, TracerSnapshot)
        self.assertTrue(trace.complete)
        self.assertEqual(trace.task_id, result.task_id)
        self.assertEqual([event.sequence for event in trace.events], list(range(1, len(trace.events) + 1)))
        self.assertEqual(len({event.event_id for event in trace.events}), len(trace.events))
        self.assertTrue(all(event.task_id == result.task_id for event in trace.events))
        self.assertTrue(all(0 <= event.step <= result.steps for event in trace.events))
        for event in trace.events:
            self.assertEqual(datetime.fromisoformat(event.timestamp).utcoffset(), timedelta(0))
        self.assertEqual(trace.events[0].type, "created")
        self.assertEqual(trace.events[-1].type, "finished")
        self.assertEqual(trace.events[-1].payload["status"], result.status.value)
        self.assertEqual(trace.events[-1].payload["stop_reason"], result.stop_reason)
        self.assertEqual(trace.events[-1].payload["output"], result.output)
        json.loads(trace.to_json())
        starts = {event.payload["llm_call_id"]: event for event in selected(result, "llm_call_started")}
        ends = {event.payload["llm_call_id"]: event for event in selected(result, "llm_call_finished")}
        self.assertEqual(starts.keys(), ends.keys())
        self.assertEqual(len(starts), len(selected(result, "llm_call_started")))
        self.assertEqual(len(ends), len(selected(result, "llm_call_finished")))
        for call_id, start in starts.items():
            end = ends[call_id]
            self.assertLess(start.sequence, end.sequence)
            self.assertEqual(start.step, end.step)
            self.assertEqual(start.payload["model"], end.payload["model"])
            self.assertGreaterEqual(end.payload["duration_ms"], 0)
        tool_starts = {event.payload["tool_execution_id"]: event for event in selected(result, "tool_started")}
        tool_ends = {event.payload["tool_execution_id"]: event for event in selected(result, "tool_finished")}
        self.assertEqual(len(tool_starts), result.tool_calls)
        for execution_id, start in tool_starts.items():
            end = tool_ends[execution_id]
            self.assertLess(start.sequence, end.sequence)
            self.assertEqual(start.step, end.step)
            self.assertTrue(end.payload["executed"])
            self.assertGreaterEqual(end.payload["duration_ms"], 0)
            self.assertLess(ends[start.payload["llm_call_id"]].sequence, start.sequence)
        for event in tool_ends.values():
            if not event.payload["executed"]:
                self.assertNotIn(event.payload["tool_execution_id"], tool_starts)
                self.assertIsNone(event.payload["duration_ms"])

    async def test_live_replay_multiple_consumers_and_final_snapshot_are_identical(self):
        gate = self.gate(complete())
        before = datetime.now(timezone.utc)
        session = await self.start(FakeProvider(gate))
        slow = session.events()
        first = await anext(slow)
        live_a = asyncio.create_task(events_of(session))
        live_b = asyncio.create_task(events_of(session))
        await gate.entered.wait()
        cursor = (await event_of(session, "llm_call_started")).sequence
        resumed = asyncio.create_task(self.collect_after(session, cursor))
        gate.release.set()
        result = await self.result(session)  # A stalled subscriber does not block this.
        a, b, suffix = await asyncio.gather(live_a, live_b, resumed)
        self.assert_trace(result)
        self.assertEqual(tuple(a), result.tracer.events)
        self.assertEqual(a, b)
        self.assertEqual(a, [first] + [event async for event in slow])
        self.assertEqual(suffix, a[cursor:])
        self.assertEqual(await events_of(session), a)
        self.assertEqual(await self.collect_after(session, len(a)), [])
        after = datetime.now(timezone.utc)
        self.assertTrue(all(before <= datetime.fromisoformat(event.timestamp) <= after for event in a))
        a[-1].payload["output"] = "altered by subscriber"
        self.assertEqual(result.tracer.events[-1].payload["output"], "done")
        for invalid in (-1, True, len(b) + 1):
            with self.subTest(cursor=invalid), self.assertRaises(ValueError):
                await anext(session.events(after_sequence=invalid))

    async def collect_after(self, session, cursor):
        return [event async for event in session.events(after_sequence=cursor)]

    async def test_no_subscriber_export_and_session_lifetime_independence(self):
        session = await ProblemSolver(FakeProvider(complete("한글 결과"))).start("goal", model="fake")
        result = await session.result()
        original = result.tracer.to_json()
        self.assert_trace(result)
        result.tracer.events[-1].payload["warnings"].append("consumer-only")
        second = await session.result()
        self.assertEqual(second.tracer.to_json(), original)
        exported = second.tracer.to_dict()
        exported["events"][-1]["payload"]["output"] = "export-only"
        self.assertEqual(json.loads(second.tracer.to_json())["events"][-1]["payload"]["output"], "한글 결과")
        ref = weakref.ref(session)
        del session
        gc.collect()
        self.assertIsNone(ref())
        self.assertEqual(second.tracer.to_json(), original)

    async def test_llm_model_usage_duration_and_disclosed_thinking_in_artifacts_only(self):
        response = calls(("echo", {}), content="working")
        response.thinking = "private-reasoning-sentinel"
        response.usage = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18,
                          "completion_tokens_details": {"reasoning_tokens": 4},
                          "total_duration_ns": 5000, "unsafe": object(), "nan": float("nan"),
                          "debug_text": "private-reasoning-sentinel"}
        response.usage["cycle"] = response.usage
        gate = self.gate(response)
        tool_gate = self.gate("echo result")
        now = [100.0]
        with patch("shs_agent.agent.runtime.runner.perf_counter", side_effect=lambda: now[0]):
            session = await self.start(FakeProvider(gate, complete()), registry=registry_for(echo=tool_gate),
                                       model_selector=lambda step: f"model-{step}")
            await gate.entered.wait()
            now[0] += 2
            gate.release.set()
            await tool_gate.entered.wait()
            now[0] += 3
            tool_gate.release.set()
            result = await self.result(session)
        self.assert_trace(result)
        ends = selected(result, "llm_call_finished")
        self.assertEqual([event.payload["model"] for event in ends], ["model-1", "model-2"])
        self.assertEqual(ends[0].payload["duration_ms"], 2000)
        self.assertEqual(selected(result, "tool_finished")[0].payload["duration_ms"], 3000)
        self.assertEqual(ends[0].payload["usage"], {
            "prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18,
            "completion_tokens_details": {"reasoning_tokens": 4}, "total_duration_ns": 5000,
        })
        self.assertIsNone(ends[1].payload["usage"])
        self.assertNotIn("private-reasoning-sentinel", json.dumps(result.tracer.to_dict()["events"]))
        observed = result.tracer.artifact(ends[0].payload["artifacts"]["common_response"])
        self.assertEqual(observed.data["fields"]["thinking"], response.thinking)
        self.assertFalse(observed.details_complete)  # Cycle, opaque object and NaN are explicit capture issues.
        self.assertTrue(any(message.get("thinking") == response.thinking for message in result.messages))
        self.assertEqual(selected(result, "message")[0].step, 1)

    async def test_provider_errors_invalid_empty_and_incomplete_responses_have_final_records(self):
        for response, status, call_status in (
            (RuntimeError("offline"), TaskStatus.FAILED, "failed"),
            (object(), TaskStatus.FAILED, "failed"),
            (LLMResponse(""), TaskStatus.FAILED, "succeeded"),
            (LLMResponse("partial", finish_reason="length"), TaskStatus.INCOMPLETE, "succeeded"),
        ):
            with self.subTest(status=status, response=response):
                session = await self.start(FakeProvider(response))
                result = await self.result(session)
                self.assertEqual(result.status, status)
                self.assert_trace(result)
                self.assertEqual(selected(result, "llm_call_finished")[0].payload["status"], call_status)
                self.assertFalse(selected(result, "tool_started"))
                self.assertEqual(tuple(await events_of(session)), result.tracer.events)

    async def test_tool_failures_rejections_and_large_original_references(self):
        large = "large-original-" * 4000 + "original-tail"
        cyclic = []
        cyclic.append(cyclic)

        def fail():
            raise ValueError("observed failure")

        session = await self.start(FakeProvider(
            calls(("large", {}), ("fail", {}), ("missing", {}), ("cyclic", {})), complete(),
        ), registry=registry_for(large=lambda: large, fail=fail, cyclic=lambda: cyclic),
            limits=AgentLimits(max_tool_output_chars=256))
        result = await self.result(session)
        self.assert_trace(result)
        ends = selected(result, "tool_finished")
        self.assertEqual([event.payload["status"] for event in ends],
                         ["succeeded", "failed", "rejected", "failed", "succeeded"])
        self.assertTrue(ends[0].payload["result"]["truncated"])
        self.assertNotIn("original-tail", json.dumps(result.tracer.to_dict()["events"]))
        detail = result.tracer.artifact(ends[0].payload["result_ref"]["artifact_id"])
        raw = result.tracer.artifact(detail.data["raw_result_artifact_id"])
        self.assertEqual(raw.data["output"], large)
        original = result.messages[ends[0].payload["result_ref"]["message_index"]]
        self.assertEqual(json.loads(original["content"])["output"], large)
        for event in ends:
            args_ref = event.payload["arguments_ref"]
            invocation = result.messages[args_ref["message_index"]]
            self.assertIn(args_ref["tool_call_id"], [call["id"] for call in invocation["tool_calls"]])
            message = result.messages[event.payload["result_ref"]["message_index"]]
            self.assertEqual(message["tool_call_id"], event.payload["tool_call_id"])

    async def test_cancel_during_llm_records_actual_completion_and_discard(self):
        gate = self.gate(complete("unused"))
        session = await self.start(FakeProvider(gate))
        await gate.entered.wait()
        command = await session.cancel()
        gate.release.set()
        result = await self.result(session)
        self.assert_trace(result)
        self.assertEqual(result.status, TaskStatus.CANCELLED)
        self.assertEqual(selected(result, "llm_call_finished")[0].payload["status"], "succeeded")
        self.assertEqual(selected(result, "response_discarded")[0].payload["reason"], "host_cancelled")
        self.assertEqual(selected(result, "command_applied")[0].payload["message_id"], command)
        self.assertFalse(selected(result, "tool_started"))

    async def test_cancel_during_tool_keeps_completed_effect_and_skips_unstarted_tool(self):
        gate = self.gate("effect occurred")
        effects = []
        session = await self.start(FakeProvider(calls(("slow", {}), ("later", {}))),
                                   registry=registry_for(slow=gate, later=lambda: effects.append(True)))
        await gate.entered.wait()
        await session.cancel()
        gate.release.set()
        result = await self.result(session)
        self.assert_trace(result)
        self.assertEqual(result.status, TaskStatus.CANCELLED)
        self.assertFalse(effects)
        ends = selected(result, "tool_finished")
        self.assertEqual([event.payload["status"] for event in ends], ["succeeded", "skipped"])
        self.assertEqual(ends[0].payload["result"]["output"], "effect occurred")
        self.assertEqual(tuple(await events_of(session)), result.tracer.events)

    async def test_cancel_before_run_has_no_fabricated_llm_or_tool_calls(self):
        provider = FakeProvider(complete())
        session = ProblemSolver(provider).create_task(request=TaskRequest("goal"), model="fake")
        await session.cancel()
        await session.start()
        self.sessions.append(session)
        result = await self.result(session)
        self.assert_trace(result)
        self.assertEqual(result.status, TaskStatus.CANCELLED)
        self.assertFalse(provider.requests)
        self.assertFalse(selected(result, "llm_call_started"))
        self.assertFalse(selected(result, "tool_started"))

    async def test_runtime_cancellation_closes_inflight_llm_and_tool_records(self):
        for kind in ("llm", "tool"):
            with self.subTest(kind=kind):
                gate = self.gate(complete() if kind == "llm" else "unfinished")
                provider = FakeProvider(gate if kind == "llm" else calls(("slow", {})))
                session = await self.start(provider, registry=registry_for(slow=gate))
                await gate.entered.wait()
                session._task.cancel()  # Infrastructure cancellation, distinct from cooperative Host cancel.
                result = await self.result(session)
                self.assert_trace(result)
                self.assertEqual(result.status, TaskStatus.CANCELLED)
                event_kind = "llm_call_finished" if kind == "llm" else "tool_finished"
                self.assertEqual(selected(result, event_kind)[0].payload["status"], "cancelled")

    async def test_host_question_answer_instruction_and_memo_are_correlated(self):
        gate = self.gate(complete("obsolete"))
        provider = FakeProvider(calls(("host_ask", {"question": "which?"})), gate,
                                calls(("memo_write", {"content": "chosen blue", "kind": "fact"})),
                                complete("updated"))
        session = await self.start(provider)
        question = await event_of(session, "question")
        await session.answer(question.payload["question_id"], "blue", message_id="answer")
        await gate.entered.wait()
        await session.send_instruction("new constraint", message_id="instruction")
        await session.send_instruction("new constraint", message_id="instruction")
        gate.release.set()
        result = await self.result(session)
        self.assert_trace(result)
        received = selected(result, "command_received")
        self.assertEqual(len(received), 2)
        self.assertEqual(received[0].payload["command"], {"question_id": question.payload["question_id"], "text": "blue"})
        self.assertEqual(received[1].payload["command"], {"content": "new constraint"})
        closed = selected(result, "question_closed")[0]
        self.assertEqual(closed.payload["reason"], "answered")
        self.assertEqual(closed.payload["tool_ref"], question.payload["tool_ref"])
        self.assertEqual(question.payload["tool_ref"]["tool_execution_id"],
                         selected(result, "tool_started")[0].payload["tool_execution_id"])
        memo = selected(result, "task_memo_written")[0]
        self.assertEqual(memo.payload["memo_id"], result.memos[0].memo_id)
        self.assertEqual(memo.step, result.memos[0].created_step)
        self.assertEqual(selected(result, "response_discarded")[0].payload["reason"], "new_host_instruction")

    async def test_question_cancel_timeout_and_rejected_answer_are_recorded(self):
        for mode in ("cancel", "timeout"):
            with self.subTest(mode=mode):
                session = await self.start(FakeProvider(calls(("host_ask", {"question": "pick"}))),
                                           limits=AgentLimits(host_response_timeout=0.02 if mode == "timeout" else None))
                await event_of(session, "question")
                if mode == "cancel":
                    await session.answer("wrong", "unapplied answer", message_id="wrong")
                    await event_of(session, "command_rejected")
                    await session.cancel()
                result = await self.result(session)
                self.assert_trace(result)
                reason = "host_cancelled" if mode == "cancel" else "host_response_timeout"
                self.assertEqual(selected(result, "question_closed")[0].payload["reason"], reason)
                self.assertEqual(selected(result, "tool_finished")[0].payload["status"], "failed")
                if mode == "cancel":
                    self.assertEqual(selected(result, "command_received")[0].payload["command"]["text"], "unapplied answer")

    async def test_limits_and_invalid_batches_never_invent_tool_start_events(self):
        for provider, limits, expected in (
            (FakeProvider(LLMResponse("progress")), AgentLimits(max_steps=1), "max_steps"),
            (FakeProvider(complete()), AgentLimits(max_tool_calls=0), "max_tool_calls"),
        ):
            with self.subTest(limit=expected):
                session = await self.start(provider, limits=limits)
                result = await self.result(session)
                self.assert_trace(result)
                self.assertEqual(result.status, TaskStatus.LIMIT_REACHED)
                self.assertEqual(selected(result, "limit_reached")[0].payload["limit"], expected)
                self.assertFalse(selected(result, "tool_started"))
        session = await self.start(FakeProvider(calls(("task_complete", {"output": "no"}),
                                                     ("memo_write", {"content": "no", "kind": "fact"})), complete()))
        result = await self.result(session)
        self.assert_trace(result)
        self.assertEqual([event.payload["status"] for event in selected(result, "tool_finished")],
                         ["skipped", "skipped", "succeeded"])

    async def test_compaction_llm_is_recorded_without_adding_decision_steps(self):
        for summary, final_status in ((LLMResponse("summary"), TaskStatus.COMPLETED),
                                      (LLMResponse(""), TaskStatus.FAILED),
                                      (ValueError("summary failed"), TaskStatus.FAILED)):
            with self.subTest(summary=summary):
                provider = FakeProvider(LLMResponse("old"), LLMResponse("recent"), summary, complete())
                session = await self.start(provider, limits=AgentLimits(context_compact_threshold_chars=256,
                                                                         context_keep_recent_messages=1))
                result = await self.result(session)
                self.assert_trace(result)
                self.assertEqual(result.status, final_status)
                starts = selected(result, "llm_call_started")
                self.assertEqual(len(starts), result.steps + 1)
                compact = selected(result, "context_compaction_started")[0]
                end_kind = "context_compacted" if final_status == TaskStatus.COMPLETED else "context_compaction_failed"
                self.assertEqual(selected(result, end_kind)[0].payload["llm_call_id"], compact.payload["llm_call_id"])
                self.assertEqual(starts[2].payload["purpose"], "context_compaction")
                self.assertEqual(starts[2].step, 2)
                self.assertEqual(result.messages[compact.payload["from_message"]]["content"], "old")

    async def test_cancel_during_compaction_does_not_claim_summary_applied(self):
        gate = self.gate(LLMResponse("unused summary"))
        session = await self.start(FakeProvider(LLMResponse("old"), LLMResponse("recent"), gate),
                                   limits=AgentLimits(context_compact_threshold_chars=256, context_keep_recent_messages=1))
        await gate.entered.wait()
        await session.cancel()
        gate.release.set()
        result = await self.result(session)
        self.assert_trace(result)
        self.assertEqual(result.context_compactions, 0)
        self.assertEqual(len(selected(result, "context_compaction_cancelled")), 1)
        self.assertFalse(selected(result, "context_compacted"))

    async def test_concurrent_tasks_with_reused_tool_ids_have_separate_traces(self):
        first = complete("first")
        second = complete("second")
        first.tool_calls[0].id = second.tool_calls[0].id = "provider-reused-id"
        gate_a, gate_b = self.gate(first), self.gate(second)
        a = await self.start(FakeProvider(gate_a))
        b = await self.start(FakeProvider(gate_b))
        await asyncio.gather(gate_a.entered.wait(), gate_b.entered.wait())
        gate_b.release.set()
        result_b = await self.result(b)
        gate_a.release.set()
        result_a = await self.result(a)
        for result, session in ((result_a, a), (result_b, b)):
            self.assert_trace(result)
            self.assertEqual(tuple(await events_of(session)), result.tracer.events)
        self.assertNotEqual(result_a.task_id, result_b.task_id)
        self.assertTrue({event.event_id for event in result_a.tracer.events}.isdisjoint(
                        event.event_id for event in result_b.tracer.events))
        self.assertNotEqual(selected(result_a, "tool_started")[0].payload["tool_execution_id"],
                            selected(result_b, "tool_started")[0].payload["tool_execution_id"])
