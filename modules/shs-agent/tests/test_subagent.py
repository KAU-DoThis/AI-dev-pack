import copy
import asyncio
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from shs_agent import (AgentActivity, AgentLimits, ProblemSolver, SubagentConfig, SubagentError,
                       TaskRequest, TaskSession, TaskStatus)
from shs_agent.llm import LLMResponse, ToolCall
from shs_agent.tool import ToolExecutor, ToolRegistry
from shs_agent.agent.session.channel import TaskChannel
from shs_agent.agent.subagent import TaskTreeSnapshot
from tests.fakes import AsyncCase, FakeProvider, calls, complete, event_of, registry_for, tool_results


def profile(provider=None, registry=None, **kwargs):
    registry = registry if registry is not None else ToolRegistry()
    return SubagentConfig(provider=provider, registry=registry, executor=ToolExecutor(registry), **kwargs)


def outputs(request, name):
    return [json.loads(m["content"]) for m in request["messages"]
            if m["role"] == "tool" and m.get("name") == name]


def child_ids(request):
    return [value["output"]["task_id"] for value in outputs(request, "subagent_spawn") if value["ok"]]


def spawn(config="child", **kwargs):
    return ("subagent_spawn", {"instruction": "delegated " + config, "context": ["explicit background"],
                                "config_id": config, **kwargs})


def wait_children(request):
    return calls(("subagent_wait", {"task_ids": child_ids(request)}))


def read_children(request):
    return calls(*(("subagent_result", {"task_id": key}) for key in child_ids(request)))


class TreeArchiveTests(unittest.TestCase):
    def tree(self):
        root = TaskChannel(TaskRequest("root"))
        child = TaskChannel(TaskRequest("child"), parent_task_id=root.task_id,
                            root_task_id=root.task_id, depth=1)
        ref = {"task_id": child.task_id, "artifact_id": child.task.input_artifact_id}
        artifact_id = root.add_artifact("subagent_spawn", {"task_refs": [ref]})
        root.emit("subagent_created", {"task_id": child.task_id}, artifact_ids=(artifact_id,))
        return root, child, ref

    def test_json_roundtrip_resolves_qualified_refs_and_preserves_local_sequences(self):
        root, child, ref = self.tree()
        tree = TaskTreeSnapshot(root.task_id, {
            root.task_id: root.tracer_snapshot(), child.task_id: child.tracer_snapshot(),
        })
        restored = TaskTreeSnapshot.from_json(tree.to_json())
        self.assertEqual(tree.to_dict(), restored.to_dict())
        self.assertEqual(restored.artifact(ref).data["instruction"], "child")
        self.assertEqual(restored.traces[root.task_id].events[0].sequence, 1)
        self.assertEqual(restored.traces[child.task_id].events[0].sequence, 1)
        self.assertFalse(restored.complete)
        self.assertTrue(restored.details_complete)
        restored.artifact(ref).data["instruction"] = "mutation"
        self.assertEqual(restored.artifact(ref).data["instruction"], "child")

    def test_missing_child_other_root_and_broken_references_are_rejected(self):
        root, child, ref = self.tree()
        data = TaskTreeSnapshot(root.task_id, {
            root.task_id: root.tracer_snapshot(), child.task_id: child.tracer_snapshot(),
        }).to_dict()
        missing = copy.deepcopy(data)
        del missing["traces"][child.task_id]
        with self.assertRaises(ValueError):
            TaskTreeSnapshot.from_dict(missing)
        broken = copy.deepcopy(data)
        artifact = next(a for a in broken["traces"][root.task_id]["artifacts"].values()
                        if a["kind"] == "subagent_spawn")
        artifact["data"]["task_refs"][0]["artifact_id"] = "absent"
        with self.assertRaises(ValueError):
            TaskTreeSnapshot.from_dict(broken)
        other = TaskChannel(TaskRequest("other root"))
        with self.assertRaises(ValueError):
            TaskTreeSnapshot(root.task_id, {root.task_id: root.tracer_snapshot(),
                                            child.task_id: other.tracer_snapshot()})

    def test_cross_task_links_do_not_disable_existing_artifact_validation(self):
        root, child, ref = self.tree()
        with self.assertRaises(ValueError):
            root.add_artifact("invalid", {}, references=(ref["artifact_id"],))

    def test_relationship_validation(self):
        for kwargs in ({"depth": 2}, {"depth": True}, {"parent_task_id": "parent"},
                       {"depth": 1, "parent_task_id": "a", "root_task_id": "b"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                TaskChannel(TaskRequest("invalid"), **kwargs)

    def test_closed_parent_log_does_not_make_live_child_tree_complete(self):
        from shs_agent import TaskResult
        root, child, ref = self.tree()
        root.close(TaskResult(root.task_id, TaskStatus.COMPLETED, "root", "fixture", 0, 0, 0))
        tree = TaskTreeSnapshot(root.task_id, {root.task_id: root.tracer_snapshot(),
                                               child.task_id: child.tracer_snapshot()})
        self.assertTrue(tree.traces[root.task_id].complete)
        self.assertFalse(tree.complete)
        self.assertFalse(tree.result(child.task_id)["available"])


class TraceSessionTests(AsyncCase):
    async def test_root_relationship_live_trace_and_terminal_snapshot(self):
        gate = self.gate(complete())
        session = await self.start(FakeProvider(gate))
        await gate.entered.wait()
        state = await session.state()
        self.assertIsNone(state.task.parent_task_id)
        self.assertEqual(state.task.root_task_id, session.task_id)
        self.assertEqual(state.task.depth, 0)
        live = await session.trace()
        self.assertFalse(live.complete)
        live.artifacts[state.task.input_artifact_id].data["instruction"] = "mutation"
        self.assertEqual((await session.artifact(state.task.input_artifact_id)).data["instruction"], "test task")
        gate.release.set()
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual((await session.trace()).to_dict(), result.tracer.to_dict())


class SubagentRuntimeTests(AsyncCase):
    async def start_tree(self, provider, configs, **kwargs):
        kwargs.setdefault("limits", AgentLimits(max_total_subagents=4, max_subagent_depth=1,
                                                context_compact_threshold_chars=None))
        solver = kwargs.pop("solver", ProblemSolver())
        session = await solver.start("parent secret instruction", provider=provider, model="parent-model",
                                     subagents=configs, **kwargs)
        self.sessions.append(session)
        return session

    async def test_parallel_full_engine_parent_work_results_plan_and_detached_trace(self):
        gates = [self.gate("A evidence"), self.gate("B evidence")]
        children = {}
        for name, gate in zip(("A", "B"), gates):
            provider = FakeProvider(
                calls(("plan_create", {"items": [{"item_id": "local", "description": name}], "reason": "child plan"})),
                LLMResponse(None, [ToolCall("memo_write", {"content": name + " memo", "kind": "fact"}, "reused")]),
                calls(("child_work", {})), complete(name + " answer"))
            children[name] = profile(provider, registry_for(child_work=gate), model=name + "-model",
                                     options={"child": name}, limits=AgentLimits(context_compact_threshold_chars=None))
        own_work = asyncio.Event()

        async def parent_work():
            await asyncio.gather(*(gate.entered.wait() for gate in gates))
            self.assertFalse(any(gate.release.is_set() for gate in gates))
            own_work.set()
            return "parent worked while both children were in external tools"

        def parallel_spawn(request):
            plan = json.loads(next(m["content"].split("\n", 1)[1] for m in request["messages"]
                                   if m.get("content", "").startswith("Current task plan")))
            args = spawn("A", plan_ref={"plan_id": plan["plan_id"], "revision": 1, "item_id": "review"})[1]
            return LLMResponse(None, [ToolCall("subagent_spawn", args, "reused"),
                                      ToolCall(*spawn("B"), id="second")])

        def work_and_inspect(request):
            ids = child_ids(request)
            self.assertEqual(len(ids), 2)
            for value in outputs(request, "subagent_spawn"):
                self.assertFalse(value["output"]["done"])
            return calls(("parent_work", {}), ("subagent_status", {}),
                         ("subagent_result", {"task_id": ids[0]}))

        def revise(request):
            results = outputs(request, "subagent_result")
            self.assertFalse(results[0]["output"]["available"])
            self.assertEqual([r["output"]["output"] for r in results[1:]], ["A answer", "B answer"])
            return calls(("plan_update", {"updates": [{"item_id": "review", "status": "completed"}],
                                          "expected_revision": 1, "reason": "reviewed both results"}))

        parent = FakeProvider(
            calls(("plan_create", {"items": [{"item_id": "review", "description": "Review children"}],
                                    "reason": "parent plan"}),
                  ("memo_write", {"content": "parent only memo", "kind": "fact"})),
            parallel_spawn, work_and_inspect, wait_children, read_children, revise, complete("combined"))
        session = await self.start_tree(parent, children, registry=registry_for(parent_work=parent_work))
        await asyncio.wait_for(own_work.wait(), 3)
        await event_of(session, "subagent_wait_started")
        manager = session.subagents
        live = await manager.children()
        self.assertEqual(manager.total_created, 2)
        self.assertEqual((await session.state()).agent.activity, AgentActivity.WAITING_FOR_SUBAGENTS)
        self.assertEqual((await session.state()).task.plan_revision, 1)
        for value in live:
            child = manager.child(value["task_id"])
            state = await child.state()
            self.assertEqual(state.agent.activity, AgentActivity.TOOL_EXECUTION)
            self.assertEqual((state.task.parent_task_id, state.task.root_task_id, state.task.depth),
                             (session.task_id, session.task_id, 1))
            artifact = await child.artifact(state.agent.operation_artifact_id)
            self.assertEqual(artifact.data["name"], "child_work")
            self.assertIsNone(child.subagents)
            value["state"]["task"]["instruction"] = "mutated copy"
            self.assertNotEqual((await child.state()).task.instruction, "mutated copy")
        self.assertFalse((await manager.snapshot()).complete)
        for gate in gates:
            gate.release.set()
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual(result.plan.revision, 2)
        self.assertEqual([memo.content for memo in result.memos], ["parent only memo"])
        tree = TaskTreeSnapshot.from_json(result.tree.to_json())
        self.assertTrue(tree.complete)
        self.assertTrue(tree.details_complete)
        self.assertEqual(tree.to_json(), (await manager.collect()).to_json())
        self.assertEqual(tree.result(session.task_id)["output"], "combined")
        self.assertEqual(len(tree.traces), 3)
        creation = [e for e in result.tracer.events if e.type == "subagent_created"]
        first_record = result.tracer.artifact(creation[0].payload["artifact_id"])
        self.assertEqual(first_record.data["plan_ref"]["revision"], 1)
        self.assertIn(result.tracer.state(at_sequence=creation[0].sequence).task.plan_artifact_id,
                      first_record.references)
        for key in tree.traces.keys() - {session.task_id}:
            child_result = await manager.child(key).result()
            self.assertEqual(child_result.plan.revision, 1)
            self.assertEqual(child_result.memos[0].task_id, key)
            self.assertEqual(tree.result(key)["output"], child_result.output)
            text = json.dumps(child_result.messages)
            self.assertNotIn("parent secret instruction", text)
            self.assertNotIn("parent only memo", text)
            self.assert_balanced(child_result.messages)
            trace = tree.traces[key]
            self.assertTrue(any(a.kind == "llm_common_request" for a in trace.artifacts.values()))
            self.assertTrue(any(a.kind == "tool_raw_result" for a in trace.artifacts.values()))
            self.assertTrue(any(e.type == "subagent_bound" for e in trace.events))
        for config in children.values():
            request = config.provider.requests[0]
            self.assertEqual(request["options"], config.options)
            names = {tool["name"] for tool in request["tools"]}
            self.assertNotIn("host_ask", names)
            self.assertFalse(any(name.startswith("subagent_") for name in names))
        self.assert_balanced(result.messages)
        delivered = next(e for e in result.tracer.events if e.type == "subagent_result_delivered")
        link = result.tracer.artifact(delivered.payload["artifact_id"])
        self.assertEqual(link.data["source"]["task_id"], session.task_id)
        self.assertIn("llm_call_id", link.data["source"])
        self.assertTrue(all(tree.artifact(ref) for ref in link.data["task_refs"]))
        result.tree.traces[session.task_id].artifacts.clear()
        self.assertTrue((await session.result()).tree.traces[session.task_id].artifacts)

    async def test_wait_instruction_preserves_children_and_does_not_propagate(self):
        child_gate = self.gate(complete("child"))
        parent_after_instruction = self.gate(wait_children)
        # Provider callback needs the current request when released.
        async def after_instruction(request):
            parent_after_instruction.entered.set()
            await parent_after_instruction.release.wait()
            self.assertTrue(any(m.get("content") == "changed root only" for m in request["messages"]))
            return wait_children(request)
        child_provider = FakeProvider(child_gate)
        parent = FakeProvider(calls(spawn()), wait_children, after_instruction, read_children, complete())
        session = await self.start_tree(parent, {"child": profile(child_provider)})
        await event_of(session, "subagent_wait_started")
        await child_gate.entered.wait()
        command = await session.send_instruction("changed root only")
        await event_of(session, "command_applied", lambda e: e.payload["message_id"] == command)
        await asyncio.wait_for(parent_after_instruction.entered.wait(), 3)
        child = session.subagents.child((await session.subagents.children())[0]["task_id"])
        self.assertFalse(child.done)
        self.assertFalse((await child.state()).task.cancel_requested)
        self.assertNotIn("changed root only", json.dumps(child_provider.requests))
        child_gate.release.set()
        parent_after_instruction.release.set()
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        waits = [a for a in result.tracer.artifacts.values() if a.kind == "subagent_wait_finished"]
        self.assertEqual(waits[0].data["reason"], "superseded_by_host_instruction")

    async def test_parent_cancel_interrupts_wait_then_cooperatively_joins_external_child(self):
        gate = self.gate("finished external effect")
        child = FakeProvider(calls(("slow", {})), complete())
        session = await self.start_tree(FakeProvider(calls(spawn()), wait_children),
                                        {"child": profile(child, registry_for(slow=gate))})
        await event_of(session, "subagent_wait_started")
        await gate.entered.wait()
        child_session = session.subagents.child((await session.subagents.children())[0]["task_id"])
        receipt = await session.cancel()
        await event_of(session, "command_applied", lambda e: e.payload["message_id"] == receipt)
        await event_of(child_session, "command_applied", lambda e: e.payload["command_type"] == "cancel")
        self.assertFalse(session.done)
        self.assertFalse(child_session.done)
        self.assertEqual((await child_session.state()).agent.activity, AgentActivity.TOOL_EXECUTION)
        self.assertEqual((await session.state()).agent.waiting_for, "subagent_cleanup")
        gate.release.set()
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.CANCELLED)
        self.assertEqual((await child_session.result()).status, TaskStatus.CANCELLED)
        self.assertTrue(result.tree.complete)

    async def test_cancel_one_child_does_not_cancel_sibling(self):
        gate_a, gate_b = self.gate(complete("A")), self.gate(complete("B"))
        parent_gate = self.gate(complete())
        def cancel_a(request):
            return calls(("subagent_cancel", {"task_id": child_ids(request)[0]}))
        async def pause_parent(request):
            parent_gate.entered.set()
            await parent_gate.release.wait()
            return wait_children(request)
        parent = FakeProvider(calls(spawn("A"), spawn("B")), cancel_a, pause_parent, read_children, complete())
        session = await self.start_tree(parent, {"A": profile(FakeProvider(gate_a)),
                                                 "B": profile(FakeProvider(gate_b))})
        await parent_gate.entered.wait()
        children = await session.subagents.children()
        self.assertTrue(children[0]["state"]["task"]["cancel_requested"])
        self.assertFalse(children[1]["state"]["task"]["cancel_requested"])
        for gate in (gate_a, gate_b, parent_gate):
            gate.release.set()
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual([result.tree.result(c["task_id"])["status"] for c in children], ["cancelled", "completed"])

    async def test_complete_rejected_while_child_runs_then_parent_recovers(self):
        child_gate = self.gate(complete("child"))
        checked = asyncio.Event()
        async def recover(request):
            failure = outputs(request, "task_complete")[-1]
            self.assertFalse(failure["ok"])
            self.assertEqual(failure["error"]["type"], "SubagentError")
            checked.set()
            return wait_children(request)
        parent = FakeProvider(calls(spawn()), complete("too early"), recover, read_children, complete("reviewed"))
        session = await self.start_tree(parent, {"child": profile(FakeProvider(child_gate))})
        await asyncio.wait_for(checked.wait(), 3)
        self.assertFalse(session.done)
        child_gate.release.set()
        result = await self.result(session)
        self.assertEqual((result.status, result.output), (TaskStatus.COMPLETED, "reviewed"))

    async def test_child_failure_is_result_parent_can_replan(self):
        parent = FakeProvider(calls(("plan_create", {"items": [{"item_id": "child", "description": "delegate"}],
                                                     "reason": "initial"})),
            calls(spawn()), wait_children, read_children,
            calls(("plan_revise", {"items": [{"item_id": "self", "description": "solve directly"}],
                                    "expected_revision": 1, "reason": "child failed"})), complete("solved directly"))
        session = await self.start_tree(parent, {"child": profile(FakeProvider(RuntimeError("child unavailable")))})
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual(result.plan.revision, 2)
        delivered = next(r["output"] for r in tool_results(result) if r.get("output", {}).get("available"))
        self.assertEqual(delivered["status"], "failed")
        self.assertEqual(result.plan.items[0].status.value, "pending")

    async def test_limits_registration_failures_and_scheduling_failure_count(self):
        bad_registry = registry_for(host_ask=lambda: "forbidden")
        parent = FakeProvider(calls(spawn("unknown"), spawn("bad"), spawn("good")), wait_children,
                              read_children, calls(spawn("good")), complete())
        session = await self.start_tree(parent, {"bad": profile(FakeProvider(), bad_registry),
                                                 "good": profile(FakeProvider(complete()))},
                limits=AgentLimits(max_total_subagents=1, max_subagent_depth=1))
        result = await self.result(session)
        self.assertEqual(session.subagents.total_created, 1)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        failures = [e for e in result.tracer.events if e.type == "subagent_spawn_rejected"]
        self.assertEqual(len(failures), 3)
        self.assertIn("max_total_subagents", result.tracer.artifact(failures[-1].payload["artifact_id"]).data["reason"])
        original_start = TaskSession.start
        async def fail_child_start(child_session):
            if (await child_session.state()).task.depth == 1:
                raise RuntimeError("scheduler rejected")
            await original_start(child_session)
        with patch.object(TaskSession, "start", fail_child_start):
            second = await self.start_tree(FakeProvider(calls(spawn()), wait_children, read_children, complete()),
                                           {"child": profile(FakeProvider())})
            second_result = await self.result(second)
        self.assertEqual(second.subagents.total_created, 1)
        child_id = (await second.subagents.children())[0]["task_id"]
        self.assertEqual(second_result.tree.result(child_id)["stop_reason"], "subagent_schedule_failed")
        self.assertTrue(second_result.tree.complete)

    async def test_two_roots_same_solver_have_independent_budgets_and_reject_other_ids(self):
        solver = ProblemSolver()
        gates = [self.gate(complete()), self.gate(complete())]
        roots = []
        for gate in gates:
            roots.append(await self.start_tree(FakeProvider(calls(spawn()), wait_children, read_children, complete()),
                {"child": profile(FakeProvider(gate))}, solver=solver,
                limits=AgentLimits(max_total_subagents=1, max_subagent_depth=1)))
        await asyncio.gather(*(gate.entered.wait() for gate in gates))
        first_id = (await roots[0].subagents.children())[0]["task_id"]
        with self.assertRaises(SubagentError):
            roots[1].subagents.child(first_id)
        # Any registered task may inspect its own direct children (empty here).
        self.assertEqual(await roots[0].subagents.status(first_id), {"children": []})
        with self.assertRaises(SubagentError):
            await roots[0].subagents.status(first_id, roots[0].task_id)
        for root in roots:
            self.assertEqual(root.subagents.total_created, 1)
        for gate in gates:
            gate.release.set()
        for root in roots:
            self.assertEqual((await self.result(root)).status, TaskStatus.COMPLETED)

    async def test_cancelled_host_collect_waiter_preserves_child_handles(self):
        gate = self.gate(complete())
        session = await self.start_tree(FakeProvider(calls(spawn()), wait_children, read_children, complete()),
                                        {"child": profile(FakeProvider(gate))})
        await gate.entered.wait()
        collect = asyncio.create_task(session.subagents.collect())
        collect.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await collect
        child = session.subagents.child((await session.subagents.children())[0]["task_id"])
        self.assertFalse(child.done)
        self.assertFalse((await child.state()).task.cancel_requested)
        gate.release.set()
        self.assertEqual((await self.result(session)).status, TaskStatus.COMPLETED)

    async def test_host_inheritance_profiles_and_tool_scope(self):
        seen = []
        class SharedProvider:
            async def chat(_, **request):
                seen.append(copy.deepcopy(request))
                instruction = request["messages"][1]["content"]
                if instruction.startswith("delegated"):
                    return complete("inherited")
                if not child_ids(request):
                    return calls(spawn())
                if not outputs(request, "subagent_wait"):
                    return wait_children(request)
                if not outputs(request, "subagent_result"):
                    return read_children(request)
                return complete("parent")
        provider = SharedProvider()
        limits = AgentLimits(max_steps=12, max_tool_calls=25, max_total_subagents=1, max_subagent_depth=1)
        session = await self.start_tree(provider, {"child": profile()}, options={"temperature": 0.25}, limits=limits,
                                        registry=registry_for(parent_only=lambda: None))
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        child_request = next(r for r in seen if r["messages"][1]["content"].startswith("delegated"))
        self.assertEqual((child_request["model"], child_request["options"]), ("parent-model", {"temperature": 0.25}))
        self.assertNotIn("parent_only", {t["name"] for t in child_request["tools"]})
        child_trace = next(t for key, t in result.tree.traces.items() if key != session.task_id)
        applied = next(a.data for a in child_trace.artifacts.values() if a.kind == "subagent_configuration")
        self.assertEqual(applied["limits"]["max_steps"], 12)
        self.assertIsNone(applied["limits"]["max_total_subagents"])
        self.assertEqual(applied["root_limits"]["max_total_subagents"], 1)

    async def test_parent_failure_and_step_limit_cancel_and_recover_children(self):
        for abnormal in (RuntimeError("root fails"), None):
            with self.subTest(abnormal=abnormal):
                gate = self.gate(complete())
                async def fail_when_entered(request):
                    await gate.entered.wait()
                    raise RuntimeError("root fails")
                provider = FakeProvider(calls(spawn()), fail_when_entered)
                limits = AgentLimits(max_steps=1 if abnormal is None else 3,
                                      max_total_subagents=1, max_subagent_depth=1)
                session = await self.start_tree(provider, {"child": profile(FakeProvider(gate))}, limits=limits)
                await event_of(session, "subagent_cancel_requested")
                gate.release.set()
                result = await self.result(session)
                self.assertEqual(result.status, TaskStatus.LIMIT_REACHED if abnormal is None else TaskStatus.FAILED)
                self.assertTrue(result.tree.complete)
                child_id = (await session.subagents.children())[0]["task_id"]
                self.assertEqual(result.tree.result(child_id)["status"], "cancelled")

    async def test_invalid_ids_depth_zero_and_no_recursive_or_host_question_tools(self):
        parent = FakeProvider(calls(("subagent_status", {"task_id": "missing"}),
                                     ("subagent_result", {"task_id": "missing"}),
                                     ("subagent_wait", {"task_ids": ["missing"]}),
                                     ("subagent_cancel", {"task_id": "missing"}), spawn()), complete())
        session = await self.start_tree(parent, {"child": profile()},
                limits=AgentLimits(max_total_subagents=2, max_subagent_depth=0))
        result = await self.result(session)
        self.assertEqual(result.tool_errors, 5)
        self.assertEqual(session.subagents.total_created, 0)
        child_provider = FakeProvider(calls(("host_ask", {"question": "unsupported"}), spawn()), complete())
        second = await self.start_tree(FakeProvider(calls(spawn()), wait_children, read_children, complete()),
                                      {"child": profile(child_provider)})
        result = await self.result(second)
        child_id = (await second.subagents.children())[0]["task_id"]
        child_result = await second.subagents.child(child_id).result()
        self.assertEqual(child_result.tool_errors, 2)
        self.assertFalse(any(e.type == "question" for e in child_result.tracer.events))
        self.assertEqual(result.status, TaskStatus.COMPLETED)

    async def test_concurrent_spawn_count_boundary_and_cancelled_manager_wait(self):
        parent_gate = self.gate(complete())
        child_gate = self.gate(complete())
        session = await self.start_tree(FakeProvider(parent_gate), {"child": profile(FakeProvider(child_gate))},
                limits=AgentLimits(max_total_subagents=1, max_subagent_depth=1))
        await parent_gate.entered.wait()
        manager = session.subagents
        outcomes = await asyncio.gather(*(manager.spawn(session.task_id, instruction=str(index), context=[],
            config_id="child") for index in range(2)), return_exceptions=True)
        self.assertEqual(sum(isinstance(value, SubagentError) for value in outcomes), 1)
        self.assertEqual(manager.total_created, 1)
        child_id = (await manager.children())[0]["task_id"]
        await child_gate.entered.wait()
        waiting = asyncio.create_task(manager.wait(session.task_id, [child_id]))
        await event_of(session, "subagent_wait_started")
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        self.assertFalse(manager.child(child_id).done)
        self.assertFalse((await manager.child(child_id).state()).task.cancel_requested)
        child_gate.release.set()
        await manager.collect()
        parent_gate.release.set()
        self.assertTrue((await self.result(session)).tree.complete)

    async def test_mismatched_executor_and_child_root_budget_are_pre_registration_failures(self):
        registry = ToolRegistry()
        configs = {"mismatch": SubagentConfig(registry=registry, executor=ToolExecutor(ToolRegistry())),
                   "budget": profile(limits=AgentLimits(max_total_subagents=100, max_subagent_depth=1))}
        session = await self.start_tree(FakeProvider(calls(spawn("mismatch"), spawn("budget")), complete()), configs)
        result = await self.result(session)
        self.assertEqual(result.tool_errors, 2)
        self.assertEqual(session.subagents.total_created, 0)
        self.assertEqual(len(result.tree.traces), 1)

    async def test_plan_ref_and_spawn_identity_or_configuration_overrides_are_rejected(self):
        def invalid_requests(request):
            plan = json.loads(next(m["content"].split("\n", 1)[1] for m in request["messages"]
                                   if m.get("content", "").startswith("Current task plan")))
            ref = {"plan_id": plan["plan_id"], "revision": 1, "item_id": "one"}
            return calls(spawn(plan_ref={**ref, "revision": None}), spawn(plan_ref={**ref, "item_id": "missing"}),
                         spawn(parent_task_id="other"), spawn(provider="unapproved"),
                         spawn(max_total_subagents=100), spawn(context="not an array"),
                         ("subagent_wait", {"task_ids": []}))
        provider = FakeProvider(calls(("plan_create", {"items": [{"item_id": "one", "description": "valid"}],
                                                        "reason": "initial"})), invalid_requests, complete())
        session = await self.start_tree(provider, {"child": profile()})
        result = await self.result(session)
        self.assertEqual(result.tool_errors, 7)
        self.assertEqual(session.subagents.total_created, 0)
        self.assertEqual(result.plan.revision, 1)

    async def test_explicit_none_options_model_and_limits_override_parent_and_solve_keeps_tree(self):
        child_provider = FakeProvider(complete("child answer"))
        configs = {"child": profile(child_provider, model="approved-alternative", options=None,
                                     limits=AgentLimits(max_steps=2))}
        result = await ProblemSolver().solve("root", model="parent", options={"temperature": 1},
            provider=FakeProvider(calls(spawn()), wait_children, read_children, complete()),
            limits=AgentLimits(max_total_subagents=1, max_subagent_depth=1), subagents=configs)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertIsNone(child_provider.requests[0]["options"])
        self.assertEqual(child_provider.requests[0]["model"], "approved-alternative")
        restored = TaskTreeSnapshot.from_json(result.tree.to_json())
        child_trace = next(trace for key, trace in restored.traces.items() if key != result.task_id)
        settings = next(a.data for a in child_trace.artifacts.values() if a.kind == "subagent_configuration")
        self.assertEqual(settings["limits"]["max_steps"], 2)
        self.assertEqual(restored.result(child_trace.task_id)["output"], "child answer")

    async def test_changed_approved_registry_does_not_expand_child_capabilities(self):
        from shs_agent.tool import Tool, ToolSpec
        gate = self.gate(calls(spawn()))
        registry = ToolRegistry()
        session = await self.start_tree(FakeProvider(gate, complete()), {"child": profile(registry=registry)})
        await gate.entered.wait()
        registry.register(Tool(ToolSpec("new_tool", "later tool", {"type": "object"}), lambda: None))
        gate.release.set()
        result = await self.result(session)
        self.assertEqual(session.subagents.total_created, 0)
        self.assertEqual(result.tool_errors, 1)
        self.assertIn("registry changed", tool_results(result)[0]["error"]["message"])


class SubagentConfigurationTests(unittest.TestCase):
    def test_explicit_enable_requires_both_limits_and_rejects_invalid_limits(self):
        for kwargs in ({"max_total_subagents": -1}, {"max_subagent_depth": -1},
                       {"max_subagent_depth": True}, {"max_total_subagents": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                AgentLimits(**kwargs)
        for limits in (AgentLimits(), AgentLimits(max_total_subagents=1), AgentLimits(max_subagent_depth=1)):
            with self.assertRaises(ValueError):
                ProblemSolver(FakeProvider()).create_task(request=TaskRequest("root"), model="m",
                                                           limits=limits, subagents={"child": profile()})
        with self.assertRaises(ValueError):
            ProblemSolver(FakeProvider()).create_task(request=TaskRequest("root"), model="m",
                limits=AgentLimits(max_total_subagents=1, max_subagent_depth=1), subagents={})

    def test_subagent_names_reserved_even_when_feature_disabled(self):
        for name in ("subagent_spawn", "subagent_status", "subagent_result", "subagent_wait", "subagent_cancel"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                ProblemSolver(FakeProvider()).create_task(request=TaskRequest("root"), model="m",
                    registry=registry_for(**{name: lambda: None}))
