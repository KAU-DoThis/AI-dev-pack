import json
from pathlib import Path
import tempfile
from types import SimpleNamespace

from shs_agent import AgentLimits, TaskStatus, Workspace
from shs_agent.agent.context.compaction import Compaction, estimate_context_chars
from shs_agent.agent.context.conversation import Conversation
from shs_agent import TaskRequest
from shs_agent.llm import LLMResponse
from shs_agent.llm.errors import ContextOverflow, context_overflow
from shs_agent.llm.provider.openai import OpenAILLM
from tests.fakes import AsyncCase, FakeProvider, calls, complete, registry_for, tool_results


class ContextOverflowTests(AsyncCase):
    async def test_recovery_retries_same_step_and_never_reexecutes_side_effect(self):
        effects = []
        def write():
            effects.append("written")
            return "evidence " * 600
        provider = FakeProvider(calls(("write", {})), ContextOverflow(limit_tokens=4096),
                                LLMResponse("The file was written successfully."), complete("verified"))
        result = await self.result(await self.start(provider, registry=registry_for(write=write),
                                                   limits=AgentLimits(max_steps=2)))
        self.assertEqual((result.status, result.steps, result.context_compactions), (TaskStatus.COMPLETED, 2, 1))
        self.assertEqual(effects, ["written"])
        self.assertIn("evidence " * 600, str(result.messages))
        self.assertFalse(provider.requests[2]["tools"])
        self.assertLess(len(str(provider.requests[3]["messages"])), len(str(provider.requests[1]["messages"])))
        events = result.tracer.events
        overflow = next(e for e in events if e.type == "context_overflow")
        retry = [e for e in events if e.type == "llm_call_started" and e.payload["purpose"] == "decision"][-1]
        self.assertEqual(retry.payload["retry_of"], overflow.payload["llm_call_id"])
        self.assertEqual(retry.payload["logical_step"], 2)
        self.assert_balanced(result.messages)

    async def test_no_history_to_compact_is_explicit_not_endless_retry(self):
        provider = FakeProvider(ContextOverflow())
        result = await self.result(await self.start(provider))
        self.assertEqual((result.status, result.stop_reason), (TaskStatus.INCOMPLETE, "context_unrecoverable"))
        self.assertEqual(len(provider.requests), 1)

    async def test_output_length_empty_network_are_not_context_overflow(self):
        for response, reason in ((LLMResponse("partial", finish_reason="length"), "length"),
                                 (LLMResponse(""), "empty_response"),
                                 (ConnectionError("context_length_exceeded"), "runtime_error")):
            with self.subTest(reason=reason):
                provider = FakeProvider(response)
                result = await self.result(await self.start(provider))
                self.assertEqual(result.stop_reason, reason)
                self.assertEqual(result.context_compactions, 0)
                self.assertEqual(len(provider.requests), 1)

    async def test_new_instruction_during_summary_is_in_rebuilt_request(self):
        gate = self.gate(LLMResponse("Old history summarized."))
        provider = FakeProvider(LLMResponse("progress " * 700), ContextOverflow(), gate, complete())
        session = await self.start(provider)
        await gate.entered.wait()
        await session.send_instruction("LATEST REQUIREMENT: preserve headers")
        gate.release.set()
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertIn("LATEST REQUIREMENT", json.dumps(provider.requests[-1]["messages"]))

    async def test_summary_overflow_reduces_chunk_until_provider_accepts(self):
        sizes = []
        class Provider:
            count = 0
            overflowed = False
            async def chat(self, **request):
                if not request["tools"]:
                    size = estimate_context_chars(request["messages"], [])
                    sizes.append(size)
                    if len(sizes) == 1:
                        raise ContextOverflow()
                    return LLMResponse("Compact state.")
                self.count += 1
                if self.count < 4:
                    return LLMResponse("history " * 120)
                if not self.overflowed:
                    self.overflowed = True
                    raise ContextOverflow()
                return complete()
        result = await self.result(await self.start(Provider()))
        self.assertEqual(result.status, TaskStatus.COMPLETED, result.warnings)
        self.assertGreaterEqual(len(sizes), 2)
        self.assertLess(sizes[1], sizes[0])

    async def test_oversized_summary_does_not_retry_unchanged_forever(self):
        provider = FakeProvider(LLMResponse("x" * 3000), ContextOverflow(), LLMResponse("y" * 12000))
        result = await self.result(await self.start(provider))
        self.assertEqual(result.stop_reason, "context_unrecoverable")
        self.assertEqual(len(provider.requests), 3)

    async def test_precise_adapter_error_normalization_and_usage_unknowns(self):
        class Error(Exception):
            body = {"error": {"code": "context_length_exceeded"}}
        adapter = object.__new__(OpenAILLM)
        def fail(**kwargs):
            raise Error("request too large")
        adapter.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fail)))
        with self.assertRaises(ContextOverflow):
            adapter.chat("model", [{"role": "user", "content": "input"}])
        self.assertIsNone(context_overflow(ValueError("context_length_exceeded")))
        provider = FakeProvider(LLMResponse("progress", usage={"prompt_tokens": 10, "completion_tokens": 2}), complete())
        result = await self.result(await self.start(provider))
        finished = [e.payload for e in result.tracer.events if e.type == "llm_call_finished"]
        self.assertIsNone(finished[0]["context_window_tokens"])
        self.assertIsNone(finished[0]["remaining_tokens_estimate"])
        self.assertFalse(finished[0]["estimate_is_exact"])
        self.assertEqual(finished[-1]["cumulative_usage"]["prompt_tokens"], 10)

    async def test_explicit_overflow_finish_reason_discards_tool_calls(self):
        effects = []
        partial = calls(("effect", {}))
        partial.finish_reason = "context_length_exceeded"
        provider = FakeProvider(LLMResponse("history " * 500), partial, LLMResponse("summary"), complete())
        result = await self.result(await self.start(provider, registry=registry_for(effect=lambda: effects.append(1))))
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual(effects, [])

    def test_bounded_summary_never_splits_tool_batch(self):
        limits = AgentLimits()
        history = Conversation(TaskRequest("task"), limits)
        response = calls(("first", {}), ("second", {}))
        history.record_response(response)
        history.record_tool(response.tool_calls[0], {"ok": True, "output": "a" * 200})
        compaction = Compaction(history.initial_count, limits)
        self.assertIsNone(compaction.recovery_plan(history.snapshot(), context_chars=10000, budget_chars=5000))
        history.record_tool(response.tool_calls[1], {"ok": True, "output": "b" * 200})
        plan = compaction.recovery_plan(history.snapshot(), context_chars=10000, budget_chars=5000)
        folded = json.loads(plan.messages[1]["content"])["messages_to_fold_in"]
        self.assertEqual([m["role"] for m in folded], ["assistant", "tool", "tool"])

    async def test_workspace_files_survive_compaction(self):
        with tempfile.TemporaryDirectory() as folder:
            content = "# persisted evidence\n" * 160
            provider = FakeProvider(calls(("file_write", {"path": "working.py", "content": content})),
                ContextOverflow(), LLMResponse("working.py was written; read it to verify."),
                calls(("file_read", {"path": "working.py"})), complete())
            result = await self.result(await self.start(provider, workspace=Workspace(folder)))
            self.assertEqual(result.status, TaskStatus.COMPLETED, result.warnings)
            self.assertEqual((Path(folder) / "working.py").read_text(), content)
            self.assertEqual(tool_results(result)[1]["output"]["content"], content)

    async def test_latest_workspace_grant_applied_before_overflow_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            async def overflow(_):
                await gate()
                raise ContextOverflow()
            gate = self.gate(None)
            provider = FakeProvider(calls(("workspace_request", {
                "paths": ["/workspace"], "reason": "need actual files " * 100})), overflow,
                LLMResponse("Workspace requested; await runtime decision."),
                calls(("file_write", {"path": "new.py", "content": "# new workspace"})), complete())
            session = await self.start(provider)
            await gate.entered.wait()
            request_id = (await session.resource_state())["requests"][0]["request_id"]
            await session.grant_workspace(request_id=request_id, workspace=Workspace(folder))
            gate.release.set()
            result = await self.result(session)
            self.assertEqual(result.status, TaskStatus.COMPLETED, result.warnings)
            self.assertEqual((Path(folder) / "new.py").read_text(), "# new workspace")
            self.assertIn("file_write", {s["name"] for s in provider.requests[3]["tools"]})

    async def test_provider_error_capacity_is_not_reused_for_a_different_model(self):
        provider = FakeProvider(LLMResponse("history " * 500), ContextOverflow(limit_tokens=4096),
                                LLMResponse("summary"), LLMResponse("continue"), complete())
        result = await self.result(await self.start(provider, model_selector=lambda step: "old" if step < 3 else "new"))
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        last = [e for e in result.tracer.events if e.type == "llm_call_finished"][-1]
        self.assertEqual(last.payload["model"], "new")
        self.assertIsNone(last.payload["context_window_tokens"])
