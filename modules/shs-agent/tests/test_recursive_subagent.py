"""Recursive execution contracts, ordered with events/gates rather than elapsed time."""
import asyncio
import copy
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from shs_agent import (AgentActivity, AgentLimits, ProblemSolver, SubagentConfig,
                       SubagentError, TaskRequest, TaskSession, TaskStatus, TaskTreeSnapshot)
from shs_agent.agent.communication import CommunicationError
from shs_agent.agent.session.channel import TaskChannel
from shs_agent.llm import LLMResponse
from shs_agent.tool import ToolExecutor, ToolRegistry
from tests.fakes import AsyncCase, FakeProvider, calls, complete, event_of, events_of, registry_for
from tests.test_subagent import profile, spawn, child_ids, outputs, wait_children, read_children
from tests.test_communication import incoming, wait_own


def limits(total=10, depth=3, **kwargs):
    return AgentLimits(max_total_subagents=total, max_subagent_depth=depth,
                       context_compact_threshold_chars=None, **kwargs)


def configuration(trace):
    return next(a.data for a in trace.artifacts.values() if a.kind == "subagent_configuration")


class RecursiveTests(AsyncCase):
    async def start_tree(self, provider, configs, **kwargs):
        kwargs.setdefault("limits", limits())
        return await self.start(provider, subagents=configs, **kwargs)

    async def held_tree(self, *, total=10, depth=3):
        root_gate = self.gate(complete("root"))
        a_gate, b_gate = self.gate(complete("A")), self.gate(complete("B"))
        leaf_gate = self.gate(complete("leaf"))
        session = await self.start_tree(FakeProvider(root_gate), {
            "A": profile(FakeProvider(a_gate), allowed_subagent_config_ids=("leaf",)),
            "B": profile(FakeProvider(b_gate), allowed_subagent_config_ids=("leaf",)),
            "leaf": profile(FakeProvider(leaf_gate), allowed_subagent_config_ids=("leaf",)),
        }, limits=limits(total, depth))
        await root_gate.entered.wait()
        manager = session.subagents
        children = []
        for key, gate in (("A", a_gate), ("B", b_gate)):
            value = await manager.spawn(session.task_id, instruction=key, context=[], config_id=key)
            children.append(manager.child(value["task_id"]))
            await gate.entered.wait()
        return session, children, [root_gate, a_gate, b_gate, leaf_gate]

    async def test_full_tree_independent_engines_live_observation_and_detached_results(self):
        branches = {"root": ("A", "B"), "A": ("A1", "A2"), "B": ("B1",), "A1": ("A11",)}
        names = ("A", "B", "A1", "A2", "B1", "A11")
        gates = {name: self.gate(name + " evidence") for name in ("A2", "B1", "A11")}
        providers = {}

        def state_tools(name):
            return calls(("plan_create", {"items": [{"item_id": "local", "description": name}],
                                           "reason": name + " plan"}),
                         ("memo_write", {"content": name + " memo", "kind": "fact"}))

        def delegate(name):
            def decision(request):
                plan = json.loads(next(m["content"].split("\n", 1)[1] for m in request["messages"]
                                      if m.get("content", "").startswith("Current task plan")))
                return calls(*(spawn(key, plan_ref={"plan_id": plan["plan_id"], "revision": 1,
                                                    "item_id": "local"}) for key in branches[name]))
            return decision

        def combine(name):
            def decision(request):
                results = outputs(request, "subagent_result")
                self.assertTrue(all(r["ok"] and r["output"]["available"] for r in results))
                return complete(name + "(" + ",".join(r["output"]["output"] for r in results) + ")")
            return decision

        for name in ("root", *names):
            if name in branches:
                providers[name] = FakeProvider(state_tools(name), delegate(name),
                    calls(("subagent_status", {})), wait_children, read_children, combine(name))
            else:
                providers[name] = FakeProvider(state_tools(name), calls(("work", {})), complete(name))
        configs = {name: profile(providers[name], registry_for(work=gates[name]) if name in gates else None,
                                allowed_subagent_config_ids=branches.get(name, ())) for name in names}
        session = await self.start_tree(providers["root"], configs, limits=limits(6, 3))
        await asyncio.wait_for(asyncio.gather(*(g.entered.wait() for g in gates.values())), 3)
        manager = session.subagents
        self.assertEqual(manager.total_created, 6)
        items = await manager.descendants()
        self.assertEqual(len(items), 6)
        self.assertEqual(len(await manager.children()), 2)
        live = await manager.snapshot()
        self.assertFalse(live.complete)
        self.assertEqual(len(live.traces), 7)
        consumers = {session.task_id: asyncio.create_task(events_of(session))}
        states = {}
        for item in items:
            child = manager.child(item["task_id"])
            state = await child.state()
            states[child.task_id] = state
            self.assertEqual(state.to_dict(), item["state"])
            self.assertEqual(state.task.root_task_id, session.task_id)
            self.assertIsNotNone(state.task.plan_artifact_id)
            self.assertIsNotNone(await child.artifact(state.task.plan_artifact_id))
            if state.agent.operation_artifact_id:
                self.assertIsNotNone(await child.artifact(state.agent.operation_artifact_id))
            consumers[child.task_id] = asyncio.create_task(events_of(child))
        # A can finish while its uncle B is still running; only direct children block completion.
        gates["A2"].release.set()
        gates["A11"].release.set()
        a_id = next(i["task_id"] for i in items if i["state"]["task"]["instruction"] == "delegated A")
        self.assertEqual((await self.result(manager.child(a_id))).status, TaskStatus.COMPLETED)
        self.assertFalse(session.done)
        gates["B1"].release.set()
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.COMPLETED, result.warnings)
        self.assertEqual(result.output, "root(A(A1(A11),A2),B(B1))")
        tree = TaskTreeSnapshot.from_json(result.tree.to_json())
        self.assertEqual(tree.to_json(), (await manager.collect()).to_json())
        self.assertTrue(tree.complete and tree.details_complete)
        plan_ids = set()
        for key, trace in tree.traces.items():
            live_events = await consumers[key]
            self.assertEqual(json.dumps([e.to_dict() for e in trace.events], sort_keys=True),
                             json.dumps([e.to_dict() for e in live_events], sort_keys=True))
            self.assertEqual(trace.events[0].sequence, 1)
            if key in states:
                self.assertEqual(trace.state(at_sequence=states[key].last_event_sequence), states[key])
            task_result = result if key == session.task_id else await manager.child(key).result()
            self.assertEqual(task_result.tool_errors, 0)
            self.assertEqual(len(task_result.memos), 1)
            self.assertEqual(task_result.memos[0].task_id, key)
            self.assertEqual(task_result.plan.revision, 1)
            self.assertEqual(task_result.plan.items[0].status.value, "pending")
            plan_ids.add(task_result.plan.plan_id)
            self.assert_balanced(task_result.messages)
            if key != session.task_id:
                self.assertIsNone(task_result.tree)  # No repeated ancestor/tree copies in child results.
                self.assertNotIn("test task", json.dumps(task_result.messages))
            for artifact in trace.artifacts.values():
                if artifact.kind.startswith(("subagent_", "agent_")):
                    for ref in artifact.data.get("task_refs", []):
                        self.assertEqual(tree.artifact(ref).task_id, ref["task_id"])
                if artifact.kind == "subagent_created":
                    child_state = tree.traces[artifact.data["task_id"]].state().task
                    self.assertEqual(artifact.data["depth"], child_state.depth)
                    self.assertEqual(artifact.data["source"]["task_id"], key)
                    self.assertIn(task_result.plan.plan_id, artifact.data["plan_ref"].values())
        self.assertEqual(len(plan_ids), 7)

    async def test_concurrent_descendant_spawns_share_cumulative_budget(self):
        session, (a, b), gates = await self.held_tree(total=3)
        manager = session.subagents
        ready = [asyncio.Event(), asyncio.Event()]
        release = asyncio.Event()
        async def competing(index, parent):
            ready[index].set()
            await release.wait()
            return await manager.spawn(parent.task_id, instruction="leaf", context=[], config_id="leaf")
        jobs = [asyncio.create_task(competing(i, p)) for i, p in enumerate((a, b))]
        await asyncio.gather(*(event.wait() for event in ready))
        release.set()
        outcomes = await asyncio.gather(*jobs, return_exceptions=True)
        self.assertEqual(sum(isinstance(value, SubagentError) for value in outcomes), 1)
        self.assertEqual(manager.total_created, 3)
        leaf_id = next(value["task_id"] for value in outcomes if isinstance(value, dict))
        await gates[3].entered.wait()
        gates[3].release.set()
        await self.result(manager.child(leaf_id))
        with self.assertRaisesRegex(SubagentError, "max_total_subagents"):
            await manager.spawn(a.task_id, instruction="later", context=[], config_id="leaf")
        self.assertEqual(manager.total_created, 3)
        for gate in gates[1:3]:
            gate.release.set()
        await manager.collect()
        gates[0].release.set()
        self.assertTrue((await self.result(session)).tree.complete)

    async def test_total_limit_is_tool_error_and_intermediate_can_replan_and_finish(self):
        a = FakeProvider(calls(spawn("leaf")), wait_children, calls(spawn("leaf")),
            calls(("plan_create", {"items": [{"item_id": "self", "description": "do it myself"}],
                                    "reason": "budget exhausted"})), complete("handled locally"))
        root = await self.start_tree(FakeProvider(calls(spawn("A")), wait_children, read_children, complete()), {
            "A": profile(a, allowed_subagent_config_ids=("leaf",)), "leaf": profile(FakeProvider(complete()))},
            limits=limits(2, 3))
        result = await self.result(root)
        child = root.subagents.child((await root.subagents.children())[0]["task_id"])
        child_result = await child.result()
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual(child_result.status, TaskStatus.COMPLETED)
        self.assertEqual(child_result.tool_errors, 1)
        self.assertIn("max_total_subagents", outputs(a.requests[-1], "subagent_spawn")[-1]["error"]["message"])
        self.assertIsNotNone(child_result.plan)
        self.assertEqual(root.subagents.total_created, 2)

    async def test_depth_limit_hides_tools_but_keeps_work_and_parent_communication(self):
        seen = {}
        class Provider:
            async def chat(_, **request):
                task = request["messages"][1]["content"]
                schemas = {tool["name"]: tool for tool in request["tools"]}
                seen[task] = schemas
                level = 0 if task == "test task" else int(task)
                if level < 3:
                    if not child_ids(request):
                        return calls(("subagent_spawn", {"instruction": str(level + 1), "context": [], "config_id": "worker"}))
                    questions = incoming(request, "question")
                    if questions and not outputs(request, "agent_reply"):
                        return calls(("agent_reply", {"request_id": questions[0]["request_id"], "message": "approved answer"}))
                    waits = outputs(request, "subagent_wait")
                    if not waits or waits[-1]["output"]["reason"] != "terminated":
                        return wait_children(request)
                    return complete(str(level))
                if not outputs(request, "subagent_spawn"):
                    return calls(spawn("worker"), ("parent_notify", {"message": "leaf is working"}),
                                 ("parent_ask", {"message": "leaf question"}))
                if not outputs(request, "agent_wait_reply"):
                    return wait_own(request)
                return complete("leaf used parent answer")
        root = await self.start_tree(Provider(), {"worker": profile(allowed_subagent_config_ids=("worker",))},
                                     limits=limits(10, 3))
        result = await self.result(root)
        self.assertEqual(result.status, TaskStatus.COMPLETED, result.warnings)
        self.assertEqual(root.subagents.total_created, 3)
        self.assertNotIn("subagent_spawn", seen["3"])
        self.assertNotIn("subagent_status", seen["3"])
        self.assertNotIn("host_ask", seen["3"])
        self.assertTrue({"parent_ask", "parent_notify", "agent_reply", "agent_wait_reply"} <= seen["3"].keys())
        self.assertTrue({"parent_ask", "subagent_spawn", "subagent_ask"} <= seen["2"].keys())
        leaf = next(t for t in result.tree.traces.values() if t.state().task.depth == 3)
        self.assertEqual(result.tree.result(leaf.task_id)["tool_errors"], 1)
        self.assertEqual(result.tree.result(leaf.task_id)["output"], "leaf used parent answer")

    async def test_manager_rejects_depth_overrun_even_when_called_directly(self):
        session, (a, b), gates = await self.held_tree(depth=1)
        with self.assertRaisesRegex(SubagentError, "max_subagent_depth"):
            await session.subagents.spawn(a.task_id, instruction="too deep", context=[], config_id="leaf")
        self.assertEqual(session.subagents.total_created, 2)

    async def test_config_delegation_and_external_tools_are_not_implicitly_inherited(self):
        leaf = FakeProvider(calls(("parent_only", {}), spawn("secret")), complete("leaf"))
        a = FakeProvider(calls(spawn("secret"), spawn("leaf", max_total_subagents=100), spawn("leaf")),
                         wait_children, complete("A"))
        root = await self.start_tree(FakeProvider(calls(spawn("A")), wait_children, complete()), {
            "A": profile(a, registry_for(parent_only=lambda: "private"), allowed_subagent_config_ids=("leaf",)),
            "leaf": profile(leaf), "secret": profile(FakeProvider(complete("secret")))})
        result = await self.result(root)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        schema = next(t for t in a.requests[0]["tools"] if t["name"] == "subagent_spawn")
        self.assertEqual(schema["parameters"]["properties"]["config_id"]["enum"], ["leaf"])
        self.assertNotIn("parent_only", [t["name"] for t in leaf.requests[0]["tools"]])
        self.assertNotIn("subagent_spawn", [t["name"] for t in leaf.requests[0]["tools"]])
        self.assertEqual(root.subagents.total_created, 2)
        for key in result.tree.traces.keys() - {root.task_id}:
            self.assertEqual(result.tree.result(key)["tool_errors"], 2)

    async def test_grandchild_inherits_immediate_parent_execution_settings(self):
        seen = []
        class Shared:
            async def chat(_, **request):
                seen.append(request)
                task = request["messages"][1]["content"]
                if task == "delegated A":
                    if not child_ids(request):
                        return calls(spawn("leaf"))
                    if not outputs(request, "subagent_wait"):
                        return wait_children(request)
                return complete(task)
        shared = Shared()
        root = await self.start_tree(FakeProvider(calls(spawn("A")), wait_children, complete()), {
            "A": profile(shared, model="middle-model", options={"temperature": 0},
                         limits=AgentLimits(max_steps=7, max_tool_calls=8), allowed_subagent_config_ids=("leaf",)),
            "leaf": profile()}, options={"root": True})
        result = await self.result(root)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        leaf = next(t for t in result.tree.traces.values() if t.state().task.depth == 2)
        config = configuration(leaf)
        self.assertEqual(config["model"], "middle-model")
        self.assertEqual(config["options"], {"temperature": 0})
        self.assertEqual(config["limits"]["max_steps"], 7)
        self.assertEqual(config["limits"]["max_tool_calls"], 8)
        self.assertIsNone(config["limits"]["max_total_subagents"])
        self.assertEqual(config["root_limits"], {"max_total_subagents": 10, "max_subagent_depth": 3})
        self.assertTrue(all(r["model"] == "middle-model" and r["options"] == {"temperature": 0} for r in seen))

    async def test_non_delegated_management_and_message_targets_are_rejected(self):
        session, (a, b), gates = await self.held_tree()
        manager = session.subagents
        value = await manager.spawn(a.task_id, instruction="leaf", context=[], config_id="leaf")
        leaf = manager.child(value["task_id"])
        await gates[3].entered.wait()
        for caller, target in ((a.task_id, b.task_id), (session.task_id, leaf.task_id), (leaf.task_id, a.task_id)):
            for operation in (manager.status, manager.result, manager.cancel):
                with self.subTest(operation=operation.__name__, caller=caller), self.assertRaises(SubagentError):
                    await operation(caller, target)
            with self.assertRaises(SubagentError):
                await manager.wait(caller, [target])
        for caller, target in ((a.task_id, b.task_id), (session.task_id, leaf.task_id), (leaf.task_id, b.task_id)):
            with self.assertRaises(CommunicationError):
                manager.send_message(caller, target, kind="question", message="invalid route")
        with self.assertRaises(SubagentError):
            await manager.spawn(a.task_id, instruction="unapproved", context=[], config_id="B")
        with self.assertRaises(CommunicationError):
            manager.send_message(leaf.task_id, a.task_id, kind="instruction", message="cannot instruct parent")
        with self.assertRaises(CommunicationError):
            manager.reply_message(b.task_id, request_id=manager.send_message(leaf.task_id, a.task_id,
                kind="question", message="private")['request_id'], message="forged")

    async def test_intermediate_completion_guard_status_cancel_wait_and_result(self):
        gate = self.gate("external work finished")
        a = FakeProvider(calls(spawn("leaf")), complete("premature"), calls(("subagent_status", {})),
            lambda r: calls(("subagent_cancel", {"task_id": child_ids(r)[0]})), wait_children,
            read_children, complete("cleaned up"))
        root = await self.start_tree(FakeProvider(calls(spawn("A")), wait_children, complete()), {
            "A": profile(a, allowed_subagent_config_ids=("leaf",)),
            "leaf": profile(FakeProvider(calls(("work", {})), complete()), registry_for(work=gate))})
        await gate.entered.wait()
        a_session = root.subagents.child((await root.subagents.children())[0]["task_id"])
        await event_of(a_session, "subagent_cancel_requested")
        leaf = root.subagents.child((await root.subagents.children(a_session.task_id))[0]["task_id"])
        await event_of(leaf, "status_changed", lambda e: e.payload.get("status") == "cancelling")
        self.assertFalse(leaf.done)
        self.assertFalse(a_session.done)
        gate.release.set()
        result = await self.result(root)
        a_result = await a_session.result()
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual(a_result.tool_errors, 1)
        self.assertIn("running children", outputs(a.requests[-1], "task_complete")[0]["error"]["message"])
        self.assertEqual(outputs(a.requests[-1], "subagent_result")[-1]["output"]["status"], "cancelled")

    async def test_registration_failures_at_depth_two_keep_count_boundary(self):
        registry = ToolRegistry()
        a = FakeProvider(calls(spawn("bad"), spawn("budget"), spawn("leaf")), wait_children,
                         read_children, complete())
        original = TaskSession.start
        async def fail_grandchild(session):
            if (await session.state()).task.depth == 2:
                raise RuntimeError("schedule failed")
            await original(session)
        with patch.object(TaskSession, "start", fail_grandchild):
            root = await self.start_tree(FakeProvider(calls(spawn("A")), wait_children, complete()), {
                "A": profile(a, allowed_subagent_config_ids=("bad", "budget", "leaf")),
                "bad": SubagentConfig(registry=registry, executor=ToolExecutor(ToolRegistry())),
                "budget": profile(limits=AgentLimits(max_total_subagents=99, max_subagent_depth=9)),
                "leaf": profile(FakeProvider(complete()))})
            result = await self.result(root)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual(root.subagents.total_created, 2)
        self.assertEqual(len(result.tree.traces), 3)
        leaf = next(t for t in result.tree.traces.values() if t.state().task.depth == 2)
        self.assertEqual(result.tree.result(leaf.task_id)["stop_reason"], "subagent_schedule_failed")
        self.assertTrue(result.tree.complete)

    async def test_recursive_roots_keep_managers_budgets_and_cancellation_independent(self):
        first, (a, _), first_gates = await self.held_tree(total=3)
        second, (other, _), second_gates = await self.held_tree(total=3)
        for root, parent, gates in ((first, a, first_gates), (second, other, second_gates)):
            await root.subagents.spawn(parent.task_id, instruction="leaf", context=[], config_id="leaf")
            await gates[3].entered.wait()
        self.assertIsNot(first.subagents, second.subagents)
        with self.assertRaises(SubagentError):
            await second.subagents.spawn(a.task_id, instruction="cross root", context=[], config_id="leaf")
        with self.assertRaises(CommunicationError):
            first.subagents.send_message(a.task_id, other.task_id, kind="notify", message="cross root")
        await first.cancel()
        for gate in first_gates:
            gate.release.set()
        first_result = await self.result(first)
        self.assertEqual(first_result.status, TaskStatus.CANCELLED)
        self.assertTrue(first_result.tree.complete)
        self.assertEqual(first.subagents.total_created, 3)
        self.assertEqual(second.subagents.total_created, 3)
        for item in await second.subagents.descendants():
            self.assertFalse(item["done"])
            self.assertFalse(item["state"]["task"]["cancel_requested"])


class RecursiveCommunicationTests(AsyncCase):
    async def test_subagent_communication_tools_cannot_address_the_callers_parent(self):
        middle_ready = self.gate(None)
        root_id = None
        async def invalid(request):
            await middle_ready()
            return calls(("subagent_ask", {"task_id": root_id, "message": "wrong interface"}),
                         ("subagent_send", {"task_id": root_id, "message": "wrong interface"}),
                         ("parent_notify", {"message": "right interface"}))
        a = FakeProvider(invalid, complete("A"))
        class RootProvider:
            async def chat(_, **request):
                if not child_ids(request):
                    return calls(spawn("A"))
                waits = outputs(request, "subagent_wait")
                if not waits or waits[-1]["output"]["reason"] != "terminated":
                    return wait_children(request)
                return complete()
        root = await self.start(RootProvider(), limits=limits(), subagents={
            "A": profile(a, allowed_subagent_config_ids=("leaf",)), "leaf": profile()})
        root_id = root.task_id
        middle_ready.release.set()
        result = await self.result(root)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        child = root.subagents.child((await root.subagents.children())[0]["task_id"])
        self.assertEqual((await child.result()).tool_errors, 2)
        self.assertTrue(outputs(a.requests[-1], "parent_notify")[0]["output"]["accepted"])

    async def test_upward_relay_and_new_question_during_parent_reply_wait(self):
        leaf_start = self.gate(calls(("parent_ask", {"message": "original leaf question"})))
        leaf_continue = self.gate("leaf resumed original task")
        clarify_ready = self.gate(None)

        def leaf_finish(request):
            reply = outputs(request, "agent_wait_reply")[-1]["output"]
            self.assertEqual(reply["reason"], "answered")
            self.assertEqual(reply["reply"]["content"], "verified answer")
            return calls(("continue_work", {}))
        leaf = FakeProvider(leaf_start, wait_own, leaf_finish, complete("leaf original task complete"))

        def forward(request):
            question = incoming(request, "question")[0]
            self.assertEqual(outputs(request, "subagent_wait")[-1]["output"]["reason"], "incoming_request")
            return calls(("parent_ask", {"message": "need root evidence", "related_request_id": question["request_id"]}))

        def answer_clarification(request):
            outcome = outputs(request, "agent_wait_reply")[-1]["output"]
            self.assertEqual(outcome["reason"], "incoming_request")
            self.assertTrue(outcome["pending"])
            q = next(q for q in incoming(request, "question") if q["content"] == "which leaf?")
            return calls(("agent_reply", {"request_id": q["request_id"], "message": "my direct child"}))

        def relay_answer(request):
            outcome = outputs(request, "agent_wait_reply")[-1]["output"]
            self.assertEqual(outcome["reason"], "answered")
            return calls(("agent_reply", {"request_id": incoming(request, "question")[0]["request_id"],
                                           "message": outcome["reply"]["content"]}))
        a = FakeProvider(calls(spawn("leaf")), wait_children, forward, wait_own,
                         answer_clarification, wait_own, relay_answer, wait_children, read_children, complete("A integrated leaf"))

        async def clarify(request):
            await clarify_ready()
            q = incoming(request, "question")[0]
            return calls(("subagent_ask", {"task_id": child_ids(request)[0], "message": "which leaf?",
                                            "related_request_id": q["request_id"]}))
        def root_reply(request):
            self.assertEqual(outputs(request, "agent_wait_reply")[-1]["output"]["reply"]["content"], "my direct child")
            return calls(("agent_reply", {"request_id": incoming(request, "question")[0]["request_id"],
                                           "message": "verified answer"}))
        root_provider = FakeProvider(calls(spawn("A")), wait_children, clarify,
            lambda r: wait_own(r, "subagent_ask"), root_reply, wait_children, read_children, complete("root integrated A"))
        root = await self.start(root_provider, limits=limits(), subagents={
            "A": profile(a, allowed_subagent_config_ids=("leaf",)),
            "leaf": profile(leaf, registry_for(continue_work=leaf_continue))})
        await event_of(root, "subagent_wait_started")
        await leaf_start.entered.wait()
        a_session = root.subagents.child((await root.subagents.children())[0]["task_id"])
        leaf_session = root.subagents.child((await root.subagents.children(a_session.task_id))[0]["task_id"])
        await event_of(a_session, "subagent_wait_started")
        leaf_start.release.set()
        await event_of(a_session, "agent_wait_started")
        a_state = await a_session.state()
        self.assertEqual(a_state.agent.activity, AgentActivity.WAITING_FOR_PARENT_REPLY)
        self.assertEqual(a_state.agent.waiting_task_ids, (root.task_id,))
        self.assertEqual(len(a_state.agent.incoming_request_ids), 1)
        self.assertEqual(len(a_state.agent.outgoing_request_ids), 1)
        clarify_ready.release.set()
        await leaf_continue.entered.wait()
        self.assertFalse(leaf_session.done)
        leaf_continue.release.set()
        result = await self.result(root)
        self.assertEqual(result.status, TaskStatus.COMPLETED, result.warnings)
        tree = TaskTreeSnapshot.from_json(result.tree.to_json())
        self.assertTrue(tree.complete and tree.details_complete)
        self.assertEqual(tree.traces[a_session.task_id].state(at_sequence=a_state.last_event_sequence), a_state)
        questions = [a for t in tree.traces.values() for a in t.artifacts.values()
                     if a.kind == "agent_message_requested" and a.data["kind"] == "question"]
        self.assertEqual(len(questions), 3)
        self.assertEqual(len({q.data["request_id"] for q in questions}), 3)
        q1 = next(q for q in questions if q.task_id == leaf_session.task_id)
        q2 = next(q for q in questions if q.task_id == a_session.task_id)
        q3 = next(q for q in questions if q.task_id == root.task_id)
        self.assertEqual(q2.data["related_request_id"], q1.data["request_id"])
        self.assertEqual(q3.data["related_request_id"], q2.data["request_id"])
        for task_id, trace in tree.traces.items():
            task_result = result if task_id == root.task_id else await root.subagents.child(task_id).result()
            self.assertEqual(task_result.tool_errors, 0)
            self.assert_balanced(task_result.messages)
            self.assertFalse(trace.state().agent.outgoing_request_ids)
            self.assertFalse(trace.state().agent.incoming_request_ids)
            for message in task_result.messages:
                if message.get("content", "").startswith("Agent communication"):
                    value = json.loads(message["content"].split("\n", 1)[1])
                    self.assertEqual(message["role"], "assistant" if value["from_task_id"] == task_id else "user")

    async def test_downward_relay_has_child_wait_activity_at_intermediate_depth(self):
        leaf_work = self.gate("local evidence")
        root_start = self.gate("start question")
        def leaf_reply(request):
            return calls(("agent_reply", {"request_id": incoming(request, "question")[0]["request_id"],
                                           "message": "leaf evidence"}))
        leaf = FakeProvider(calls(("work", {})), leaf_reply, complete("leaf done"))
        def ask_leaf(request):
            return calls(("subagent_ask", {"task_id": child_ids(request)[0], "message": "get evidence",
                "related_request_id": incoming(request, "question")[0]["request_id"]}))
        def a_reply(request):
            return calls(("agent_reply", {"request_id": incoming(request, "question")[0]["request_id"],
                "message": outputs(request, "agent_wait_reply")[-1]["output"]["reply"]["content"]}))
        a = FakeProvider(calls(spawn("leaf")), wait_children, ask_leaf,
                         lambda r: wait_own(r, "subagent_ask"), a_reply, wait_children, complete("A done"))
        root_provider = FakeProvider(calls(spawn("A")), calls(("start_question", {})),
            lambda r: calls(("subagent_ask", {"task_id": child_ids(r)[0], "message": "collect evidence"})),
            lambda r: wait_own(r, "subagent_ask"), wait_children, complete())
        root = await self.start(root_provider, limits=limits(), registry=registry_for(start_question=root_start), subagents={
            "A": profile(a, allowed_subagent_config_ids=("leaf",)),
            "leaf": profile(leaf, registry_for(work=leaf_work))})
        await leaf_work.entered.wait()
        a_session = root.subagents.child((await root.subagents.children())[0]["task_id"])
        await event_of(a_session, "subagent_wait_started")
        root_start.release.set()
        await event_of(a_session, "agent_wait_started")
        state = await a_session.state()
        self.assertEqual(state.agent.activity, AgentActivity.WAITING_FOR_CHILD_REPLY)
        leaf_session = root.subagents.child(state.agent.waiting_task_ids[0])
        received = await event_of(leaf_session, "agent_message_received")
        self.assertEqual((await leaf_session.state()).agent.activity, AgentActivity.TOOL_EXECUTION)
        self.assertEqual(len(leaf.requests), 1)
        leaf_work.release.set()
        result = await self.result(root)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual(outputs(root_provider.requests[-1], "agent_wait_reply")[-1]["output"]["reply"]["content"], "leaf evidence")
        trace = result.tree.traces[leaf_session.task_id]
        applied = next(e for e in trace.events if e.type == "agent_message_applied")
        self.assertLess(received.sequence, applied.sequence)
        self.assertFalse(any(e.type == "response_discarded" for e in trace.events))

    async def test_parent_instruction_interrupts_only_target_intermediate_wait(self):
        gate = self.gate(complete("leaf"))
        pause_root = self.gate(complete("root"))
        a = FakeProvider(calls(spawn("leaf")), wait_children, wait_children, complete("A"))
        root = await self.start(FakeProvider(pause_root), limits=limits(), subagents={
            "A": profile(a, allowed_subagent_config_ids=("leaf",)), "leaf": profile(FakeProvider(gate))})
        await pause_root.entered.wait()
        manager = root.subagents
        value = await manager.spawn(root.task_id, instruction="A", context=[], config_id="A")
        a_session = manager.child(value["task_id"])
        await event_of(a_session, "subagent_wait_started")
        await gate.entered.wait()
        manager.send_message(root.task_id, a_session.task_id, kind="instruction", message="continue with revised scope")
        event = await event_of(a_session, "subagent_wait_finished")
        self.assertEqual(event.payload["reason"], "superseded_by_parent_instruction")
        self.assertFalse(a_session.done)
        leaf_id = (await manager.children(a_session.task_id))[0]["task_id"]
        self.assertFalse((await manager.child(leaf_id).state()).task.cancel_requested)
        gate.release.set()
        await manager.collect()
        pause_root.release.set()
        result = await self.result(root)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual((await a_session.result()).status, TaskStatus.COMPLETED)


class RecursiveCleanupTests(AsyncCase):
    async def test_finished_grandchild_result_and_trace_survive_root_failure(self):
        pause = self.gate(None)
        gate = self.gate(complete("unfinished sibling"))
        root = await self.start(FakeProvider(calls(spawn("A"), spawn("B")), calls(("gate", {})),
                                             RuntimeError("root failed")), limits=limits(),
            registry=registry_for(gate=pause), subagents={
                "A": profile(FakeProvider(calls(spawn("leaf")), wait_children, complete("A")),
                             allowed_subagent_config_ids=("leaf",)),
                "B": profile(FakeProvider(gate)), "leaf": profile(FakeProvider(complete("preserved")))})
        await gate.entered.wait()
        a = root.subagents.child((await root.subagents.children())[0]["task_id"])
        await self.result(a)
        leaf = root.subagents.child((await root.subagents.children(a.task_id))[0]["task_id"])
        before = (await leaf.trace()).to_json()
        pause.release.set()
        await event_of(root, "subagent_cancel_requested")
        gate.release.set()
        result = await self.result(root)
        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertTrue(result.tree.complete)
        self.assertEqual(result.tree.traces[leaf.task_id].to_json(), before)
        self.assertEqual(result.tree.result(leaf.task_id)["output"], "preserved")
        self.assertEqual(result.tree.result(a.task_id)["status"], "completed")

    async def test_intermediate_tool_call_limit_is_local_and_collects_leaf(self):
        gate = self.gate(complete("leaf"))
        root = await self.start(FakeProvider(calls(spawn("A")), wait_children, complete()), limits=limits(),
            subagents={"A": profile(FakeProvider(calls(spawn("leaf")), calls(("plan_read", {}))),
                                    limits=AgentLimits(max_tool_calls=1), allowed_subagent_config_ids=("leaf",)),
                       "leaf": profile(FakeProvider(gate), limits=AgentLimits(max_steps=5, max_tool_calls=10))})
        await gate.entered.wait()
        a = root.subagents.child((await root.subagents.children())[0]["task_id"])
        await event_of(a, "subagent_cancel_requested")
        gate.release.set()
        result = await self.result(root)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual((await a.result()).stop_reason, "max_tool_calls")
        self.assertEqual((await a.result()).status, TaskStatus.LIMIT_REACHED)
        self.assertTrue(result.tree.complete)

    async def run_cleanup(self, failure, *, at_root):
        leaf_gate = self.gate("leaf external work completed")
        trigger = self.gate(None)
        async def end(request):
            await trigger()
            if isinstance(failure, BaseException):
                raise failure
            return failure
        middle_limits = AgentLimits(max_steps=1 if failure == "limit" else 20,
                                    context_compact_threshold_chars=None)
        # Fail only after a depth-3 external operation is running.
        a = FakeProvider(calls(spawn("M")), wait_children, complete("A")) if at_root else FakeProvider(
            calls(spawn("M"), ("gate", {})), *([] if failure == "limit" else [end]))
        root_provider = FakeProvider(calls(spawn("A"), ("gate", {})), *([] if failure == "limit" else [end])) if at_root else FakeProvider(
            calls(spawn("A")), wait_children, read_children, complete("root recovered"))
        root = await self.start(root_provider, limits=limits(max_steps=1 if at_root and failure == "limit" else 20),
            registry=registry_for(gate=trigger) if at_root else None, subagents={
                "A": profile(a, registry_for(gate=trigger) if not at_root else None,
                             limits=AgentLimits(context_compact_threshold_chars=None) if at_root else middle_limits,
                             allowed_subagent_config_ids=("M",)),
                "M": profile(FakeProvider(calls(spawn("leaf")), wait_children, complete("M")),
                             limits=AgentLimits(context_compact_threshold_chars=None), allowed_subagent_config_ids=("leaf",)),
                "leaf": profile(FakeProvider(calls(("work", {})), complete()), registry_for(work=leaf_gate))})
        await asyncio.wait_for(leaf_gate.entered.wait(), 3)
        manager = root.subagents
        a_session = manager.child((await manager.children())[0]["task_id"])
        target = root if at_root else a_session
        if failure == "cancel":
            await target.cancel()
        trigger.release.set()
        middle = manager.child((await manager.children(a_session.task_id))[0]["task_id"])
        await event_of(middle, "subagent_cancel_requested")
        leaf_session = manager.child((await manager.children(middle.task_id))[0]["task_id"])
        await event_of(leaf_session, "status_changed", lambda e: e.payload.get("status") == "cancelling")
        self.assertFalse(root.done)
        self.assertFalse(target.done)
        self.assertFalse(leaf_session.done)
        live = await manager.snapshot()
        self.assertFalse(live.complete)
        with self.assertRaises(SubagentError):
            await manager.spawn(middle.task_id, instruction="late", context=[], config_id="leaf")
        leaf_gate.release.set()
        result = await self.result(root)
        self.assertTrue(result.tree.complete)
        self.assertEqual(len(result.tree.traces), 4)
        expected = TaskStatus.CANCELLED if failure == "cancel" else TaskStatus.LIMIT_REACHED if failure == "limit" else (
            TaskStatus.FAILED if isinstance(failure, BaseException) else TaskStatus.INCOMPLETE)
        self.assertEqual((await target.result()).status, expected)
        if not at_root:
            self.assertEqual(result.status, TaskStatus.COMPLETED)
        for session in (middle, leaf_session):
            self.assertEqual((await session.result()).status, TaskStatus.CANCELLED)
            self.assertTrue(result.tree.traces[session.task_id].complete)
        for session in (a_session, middle):
            trace = result.tree.traces[session.task_id]
            terminated = next(e for e in trace.events if e.type == "subagent_terminated")
            self.assertLess(terminated.sequence, trace.events[-1].sequence)

    async def test_root_failure_cancels_and_collects_all_depths(self):
        await self.run_cleanup(RuntimeError("root failure"), at_root=True)

    async def test_root_cancellation_waits_for_real_external_tool_termination(self):
        await self.run_cleanup("cancel", at_root=True)

    async def test_root_step_limit_cleans_up_descendants(self):
        await self.run_cleanup("limit", at_root=True)

    async def test_root_incomplete_cleans_up_descendants(self):
        await self.run_cleanup(LLMResponse("partial", finish_reason="length"), at_root=True)

    async def test_intermediate_failure_cleans_up_its_descendants(self):
        await self.run_cleanup(RuntimeError("middle failure"), at_root=False)

    async def test_intermediate_cancel_cleans_up_its_descendants(self):
        await self.run_cleanup("cancel", at_root=False)

    async def test_intermediate_step_limit_does_not_limit_grandchild_steps(self):
        await self.run_cleanup("limit", at_root=False)

    async def test_intermediate_incomplete_cleans_up_its_descendants(self):
        await self.run_cleanup(LLMResponse("partial", finish_reason="length"), at_root=False)


class RecursiveConfigurationTests(unittest.TestCase):
    def test_host_depth_is_nonnegative_integer_without_a_hardcoded_cap(self):
        for depth in (0, 1, 2, 3, 20, 1000):
            self.assertEqual(limits(depth=depth).max_subagent_depth, depth)
        for depth in (-1, True, 1.5, "3"):
            with self.assertRaises(ValueError):
                limits(depth=depth)

    def test_delegation_ids_validated_and_frozen(self):
        for value in ("worker", [""], ["worker", "worker"], [4]):
            with self.assertRaises((ValueError, TypeError)):
                profile(allowed_subagent_config_ids=value)
        values = ["worker"]
        config = profile(allowed_subagent_config_ids=values)
        values.append("secret")
        self.assertEqual(config.allowed_subagent_config_ids, ("worker",))
        with self.assertRaises(ValueError):
            ProblemSolver(FakeProvider()).create_task(request=TaskRequest("root"), model="m", limits=limits(),
                subagents={"parent": profile(allowed_subagent_config_ids=("missing",))})

    def test_channel_accepts_recursive_relationship_and_rejects_self_or_missing_parent(self):
        channel = TaskChannel(TaskRequest("leaf"), task_id="leaf", parent_task_id="middle", root_task_id="root", depth=7)
        self.assertEqual(channel.task.depth, 7)
        for parent, root in ((None, "root"), ("leaf", "root"), ("root", "root"), ("middle", None)):
            with self.assertRaises(ValueError):
                TaskChannel(task_id="leaf", parent_task_id=parent, root_task_id=root, depth=2)


class RecursiveArchiveTests(unittest.TestCase):
    def archive(self):
        root = TaskChannel(TaskRequest("root"), task_id="root")
        a = TaskChannel(TaskRequest("A"), task_id="A", parent_task_id="root", root_task_id="root", depth=1)
        leaf = TaskChannel(TaskRequest("leaf"), task_id="leaf", parent_task_id="A", root_task_id="root", depth=2)
        for parent, child in ((root, a), (a, leaf)):
            ref = {"task_id": child.task_id, "artifact_id": child.task.input_artifact_id}
            artifact = parent.add_artifact("subagent_created", {"task_refs": [ref]})
            parent.emit("subagent_created", {"task_id": child.task_id}, artifact_ids=(artifact,))
        return TaskTreeSnapshot("root", {c.task_id: c.tracer_snapshot() for c in (root, a, leaf)})

    def test_v2_roundtrip_and_v1_direct_child_read_compatibility(self):
        tree = self.archive()
        self.assertEqual(tree.to_dict()["schema_version"], 2)
        self.assertEqual(TaskTreeSnapshot.from_json(tree.to_json()).to_dict(), tree.to_dict())
        from tests.test_subagent import TreeArchiveTests
        root, child, _ = TreeArchiveTests().tree()
        legacy = TaskTreeSnapshot(root.task_id, {c.task_id: c.tracer_snapshot() for c in (root, child)}).to_dict()
        legacy.update(schema_version=1, scope="root_and_direct_children")
        self.assertEqual(len(TaskTreeSnapshot.from_dict(legacy).traces), 2)
        recursive_legacy = tree.to_dict()
        recursive_legacy.update(schema_version=1, scope="root_and_direct_children")
        with self.assertRaises(ValueError):
            TaskTreeSnapshot.from_dict(recursive_legacy)

    def test_missing_grandchild_and_invalid_parent_depth_root_or_cycle_rejected(self):
        original = self.archive().to_dict()
        missing = copy.deepcopy(original)
        del missing["traces"]["leaf"]
        with self.assertRaises(ValueError):
            TaskTreeSnapshot.from_dict(missing)
        for key, value in (("parent_task_id", "root"), ("parent_task_id", "leaf"),
                           ("root_task_id", "other"), ("depth", 3)):
            data = copy.deepcopy(original)
            data["traces"]["leaf"]["events"][0]["payload"]["state"]["task"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                TaskTreeSnapshot.from_dict(data)

    def test_cross_task_artifact_validation_applies_at_every_depth(self):
        data = self.archive().to_dict()
        artifact = next(a for a in data["traces"]["A"]["artifacts"].values() if a["kind"] == "subagent_created")
        artifact["data"]["task_refs"][0]["artifact_id"] = "missing"
        with self.assertRaisesRegex(ValueError, "cross-task"):
            TaskTreeSnapshot.from_dict(data)

    def test_complete_flag_cannot_hide_a_running_task_and_depth_cannot_be_boolean(self):
        data = self.archive().to_dict()
        data["traces"]["leaf"]["complete"] = True
        with self.assertRaisesRegex(ValueError, "terminal"):
            TaskTreeSnapshot.from_dict(data)
        data = self.archive().to_dict()
        data["traces"]["A"]["events"][0]["payload"]["state"]["task"]["depth"] = True
        with self.assertRaises(ValueError):
            TaskTreeSnapshot.from_dict(data)
