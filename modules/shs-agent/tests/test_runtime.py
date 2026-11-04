import asyncio
import json

from shs_agent import AgentLimits, ProblemSolver, TaskRequest, TaskStatus
from shs_agent.llm import ChatOptions, LLMResponse
from shs_agent.tool import ToolExecutor

from tests.fakes import (AsyncCase, FakeProvider, calls, complete, event_of,
                         events_of, registry_for, tool_results)


class RuntimeTests(AsyncCase):
    async def test_create_start_and_explicit_completion(self):
        provider = FakeProvider(LLMResponse("intermediate"), complete("final"))
        session = ProblemSolver(provider).create_task(request=TaskRequest("goal"), model="fake")
        self.assertEqual(session.status, TaskStatus.CREATED)
        self.assertFalse(provider.requests)
        with self.assertRaises(RuntimeError):
            await session.result()
        await session.start()
        await session.start()
        self.sessions.append(session)
        result = await self.result(session)
        self.assertEqual((result.status, result.output, result.steps), (TaskStatus.COMPLETED, "final", 2))
        self.assertEqual(result.stop_reason, "task_complete")
        self.assert_balanced(result.messages)
        await session.start()
        self.assertEqual(len(provider.requests), 2)

    async def test_solve_accepts_task_provider_and_model_per_call(self):
        solver = ProblemSolver(FakeProvider(complete("default")))
        override = FakeProvider(complete("override"))
        a = await solver.solve("one", model="a")
        b = await solver.solve(TaskRequest("two", context=("host data",)), provider=override, model="b")
        self.assertEqual((a.output, b.output), ("default", "override"))
        self.assertEqual(override.requests[0]["model"], "b")
        self.assertIn("host data", str(override.requests[0]["messages"]))

    async def test_multiple_tools_reenter_provider_and_reuse_executor(self):
        executed = []
        async def second(value):
            executed.append(value)
            return value * 2
        registry = registry_for(first=lambda value: value + 1, second=second)
        class RecordingExecutor(ToolExecutor):
            def execute(self, name, arguments):
                executed.append(name)
                return super().execute(name, arguments)
        provider = FakeProvider(calls(("first", {"value": 3}), ("second", {"value": 5})), complete())
        session = await self.start(provider, registry=registry, executor=RecordingExecutor(registry))
        result = await self.result(session)
        outputs = [json.loads(m["content"])["output"] for m in provider.requests[1]["messages"] if m["role"] == "tool"]
        self.assertEqual(outputs, [4, 10])
        self.assertEqual(executed, ["first", "second", 5])
        self.assertEqual((result.tool_calls, result.tool_errors), (3, 0))
        self.assertEqual([s["name"] for s in registry.schemas()], ["first", "second"])
        self.assert_balanced(result.messages)

    async def test_tool_error_unknown_tool_and_serialization_error_are_observations(self):
        cyclic = []
        cyclic.append(cyclic)
        def fail():
            raise ValueError("tool failed")
        provider = FakeProvider(calls(("fail", {}), ("unknown", {}), ("cyclic", {})), complete("recovered"))
        session = await self.start(provider, registry=registry_for(fail=fail, cyclic=lambda: cyclic))
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual([x["error"]["type"] for x in tool_results(result)[:-1]],
                         ["ValueError", "unknown_tool", "serialization_error"])
        self.assertEqual((result.tool_calls, result.tool_errors), (3, 3))
        self.assert_balanced(result.messages)

    async def test_mixed_completion_batch_executes_nothing_and_can_recover(self):
        executed = []
        provider = FakeProvider(calls(("task_complete", {"output": "premature"}), ("effect", {})), complete())
        session = await self.start(provider, registry=registry_for(effect=lambda: executed.append(True)))
        result = await self.result(session)
        self.assertFalse(executed)
        self.assertEqual((result.status, result.tool_calls, result.tool_errors), (TaskStatus.COMPLETED, 1, 2))
        self.assert_balanced(result.messages)

    async def test_new_instruction_discards_old_response(self):
        gate = self.gate(complete("obsolete"))
        provider = FakeProvider(gate, complete("updated"))
        session = await self.start(provider)
        await gate.entered.wait()
        command = await session.send_instruction("new constraint")
        gate.release.set()
        result = await self.result(session)
        self.assertEqual((result.output, result.steps), ("updated", 2))
        self.assertNotIn("obsolete", str(result.messages))
        self.assertIn("new constraint", str(provider.requests[1]["messages"]))
        events = await events_of(session)
        self.assertEqual(sum(e.type == "response_discarded" for e in events), 1)
        self.assertTrue(any(e.type == "command_applied" and e.payload["message_id"] == command for e in events))

    async def test_instruction_during_tool_records_active_result_and_skips_remaining(self):
        gate = self.gate("effect already happened")
        executed = []
        provider = FakeProvider(calls(("slow", {}), ("remaining", {})), complete("new plan"))
        session = await self.start(provider, registry=registry_for(slow=gate, remaining=lambda: executed.append(True)))
        await gate.entered.wait()
        await session.send_instruction("change course")
        gate.release.set()
        result = await self.result(session)
        self.assertFalse(executed)
        self.assertEqual(tool_results(result)[0]["output"], "effect already happened")
        self.assertEqual(tool_results(result)[1]["error"]["type"], "not_executed")
        self.assertEqual(result.output, "new plan")
        self.assert_balanced(result.messages)
        self.assert_balanced(provider.requests[1]["messages"])

    async def test_cancel_during_provider_waits_then_discards_completion(self):
        gate = self.gate(complete("stale"))
        session = await self.start(FakeProvider(gate))
        await gate.entered.wait()
        await session.cancel()
        await event_of(session, "status_changed", lambda e: e.payload["status"] == "cancelling")
        self.assertFalse(session.done)
        gate.release.set()
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.CANCELLED)
        self.assertEqual(result.output, "")
        self.assertNotIn("stale", str(result.messages))

    async def test_cancel_during_tool_settles_batch_and_skips_remaining(self):
        gate = self.gate("settled")
        executed = []
        session = await self.start(FakeProvider(calls(("slow", {}), ("remaining", {}))),
                                  registry=registry_for(slow=gate, remaining=lambda: executed.append(True)))
        await gate.entered.wait()
        await session.cancel()
        gate.release.set()
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.CANCELLED)
        self.assertFalse(executed)
        self.assert_balanced(result.messages)

    async def test_cancel_before_execution_calls_no_provider(self):
        provider = FakeProvider()
        session = await self.start(provider)
        await session.cancel()
        result = await self.result(session)
        self.assertEqual((result.status, result.steps), (TaskStatus.CANCELLED, 0))
        self.assertFalse(provider.requests)

    async def test_step_limit_counts_intermediate_messages(self):
        session = await self.start(FakeProvider(LLMResponse("still working")), limits=AgentLimits(max_steps=1))
        result = await self.result(session)
        self.assertEqual((result.status, result.stop_reason), (TaskStatus.LIMIT_REACHED, "max_steps"))

    async def test_tool_limit_settles_all_calls_in_batch(self):
        session = await self.start(FakeProvider(calls(("echo", {}), ("echo", {}), ("echo", {}))),
                                  registry=registry_for(echo=lambda: "ok"), limits=AgentLimits(max_tool_calls=1))
        result = await self.result(session)
        self.assertEqual((result.status, result.stop_reason, result.tool_calls),
                         (TaskStatus.LIMIT_REACHED, "max_tool_calls", 1))
        self.assertEqual(len(tool_results(result)), 3)
        self.assert_balanced(result.messages)

    async def test_completion_requires_tool_budget(self):
        session = await self.start(FakeProvider(complete()), limits=AgentLimits(max_tool_calls=0))
        result = await self.result(session)
        self.assertEqual((result.status, result.tool_calls), (TaskStatus.LIMIT_REACHED, 0))

    async def test_provider_error_empty_and_incomplete_responses(self):
        cases = [(RuntimeError("offline"), TaskStatus.FAILED, "runtime_error"),
                 (LLMResponse(""), TaskStatus.FAILED, "empty_response"),
                 (LLMResponse("partial", finish_reason="length"), TaskStatus.INCOMPLETE, "length"),
                 (LLMResponse(None, finish_reason="content_filter"), TaskStatus.INCOMPLETE, "content_filter")]
        for response, status, reason in cases:
            with self.subTest(reason=reason):
                session = await self.start(FakeProvider(response))
                result = await self.result(session)
                self.assertEqual((result.status, result.stop_reason), (status, reason))
                self.assertEqual((await events_of(session))[-1].type, "finished")

    async def test_changed_registry_rejects_previously_planned_tool(self):
        gate = self.gate(calls(("effect", {})))
        executed = []
        registry = registry_for(effect=lambda: executed.append("old"))
        provider = FakeProvider(gate, complete())
        session = await self.start(provider, registry=registry)
        await gate.entered.wait()
        registry.unregister("effect")
        registry.register(registry_for(effect=lambda: executed.append("new")).get("effect"))
        gate.release.set()
        result = await self.result(session)
        self.assertFalse(executed)
        self.assertEqual(tool_results(result)[0]["error"]["type"], "tool_changed")

    async def test_model_selector_and_task_options_snapshot(self):
        selected = []
        def select(step):
            selected.append(step)
            return f"model-{step}"
        options = ChatOptions(temperature=0.2, extra={"a": [1]})
        provider = FakeProvider(LLMResponse("progress"), complete())
        session = await self.start(provider, options=options, model_selector=select)
        options.extra["a"].append(2)
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual(selected, [1, 2])
        self.assertEqual([r["model"] for r in provider.requests], ["model-1", "model-2"])
        self.assertEqual(provider.requests[0]["options"].extra, {"a": [1]})

    async def test_concurrent_task_context_provider_tools_and_cancellation_are_independent(self):
        solver = ProblemSolver()
        gate = self.gate(complete("one"))
        one = await solver.start("task one", provider=FakeProvider(gate), model="one",
                                 registry=registry_for(only_one=lambda: 1))
        provider_two = FakeProvider(calls(("memo_write", {"content": "two note", "kind": "fact"})), complete("two"))
        two = await solver.start("task two", provider=provider_two, model="two",
                                 registry=registry_for(only_two=lambda: 2))
        self.sessions.extend([one, two])
        await gate.entered.wait()
        await one.send_instruction("private one")
        await one.cancel()
        second = await self.result(two)
        gate.release.set()
        first = await self.result(one)
        self.assertEqual((first.status, second.status), (TaskStatus.CANCELLED, TaskStatus.COMPLETED))
        self.assertNotEqual(first.task_id, second.task_id)
        self.assertEqual(second.memos[0].task_id, second.task_id)
        self.assertFalse(first.memos)
        self.assertNotIn("private one", str(second.messages))
        self.assertNotIn("only_one", str(provider_two.requests))

    async def test_bad_model_selector_fails_without_calling_provider(self):
        provider = FakeProvider()
        session = await self.start(provider, model_selector=lambda step: "")
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertIn("selected model", result.warnings[0])
        self.assertFalse(provider.requests)
