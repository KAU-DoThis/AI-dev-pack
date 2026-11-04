"""Host grants, real dispatch and deterministic safe-boundary integration."""
import asyncio
import copy
import json
from unittest.mock import patch

from shs_agent import AgentLimits, ProblemSolver, TaskRequest, TaskStatus, TaskTreeSnapshot
from shs_agent.tool import Tool, ToolRegistry, ToolSpec
from shs_agent.agent.tools.integration import ToolIntegrationError
from shs_agent.agent.tools.session_tools import SessionTools
from tests.fakes import AsyncCase, FakeProvider, calls, complete, event_of, registry_for, tool_results
from tests.test_subagent import profile, spawn, child_ids, outputs, wait_children, read_children
from tests.test_communication import incoming


REQUEST = ("tool_request", {"requested_tool": "database_query", "reason": "need transactions",
                            "intended_use": "read three months"})


def wait_tool(request):
    return calls(("tool_wait", {"request_id": outputs(request, "tool_request")[-1]["output"]["request_id"]}))


def state_data(request):
    return next(json.loads(m["content"].split("\n", 1)[1]) for m in request["messages"]
                if m.get("content", "").startswith("Current tool availability"))


class TreeProvider:
    """Forward real child messages, then collect results through normal tools."""
    def __init__(self, child):
        self.child, self.requests = child, []

    async def chat(self, **request):
        self.requests.append(copy.deepcopy(request))
        if not child_ids(request):
            return calls(spawn(self.child))
        data = state_data(request)
        for message in incoming(request, "question"):
            known = next((r for r in data["requests"] if any(h["request_id"] == message["request_id"] for h in r["hops"])), None)
            if known and known["status"] == "requested" and known["hops"][-1]["request_id"] == message["request_id"]:
                return calls(("tool_forward", {"request_id": message["request_id"]}))
        results = outputs(request, "subagent_result")
        if results and results[-1]["output"]["available"]:
            return complete(results[-1]["output"]["output"])
        waits = outputs(request, "subagent_wait")
        if waits and waits[-1]["output"]["reason"] == "terminated":
            return read_children(request)
        return wait_children(request)


class ToolIntegrationTests(AsyncCase):
    async def start_request(self, *after, catalog=None, allowed=("database_query",), registry=None):
        provider = FakeProvider(calls(REQUEST), *after)
        session = await self.start(provider, host_tools=catalog if catalog is not None else registry_for(database_query=lambda: 73),
                                   allowed_tool_ids=allowed, registry=registry,
                                   limits=AgentLimits(context_compact_threshold_chars=None))
        event = await event_of(session, "tool_host_approval_requested")
        return session, provider, event.payload["request_id"]

    async def test_request_approve_apply_offer_execute_and_detached_trace(self):
        session, provider, request_id = await self.start_request(wait_tool, calls(("database_query", {})), complete())
        before = await session.tool_state()
        self.assertNotIn("database_query", before["tools"])
        self.assertEqual(before["requests"][0]["status"], "awaiting_host")
        receipt = await session.grant_tool(request_id=request_id, tool_id="database_query")
        self.assertEqual(receipt["status"], "approved")
        self.assertNotIn("database_query", (await session.tool_state())["tools"])
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual(result.tool_errors, 0)
        self.assertEqual(outputs(provider.requests[-1], "database_query")[0]["output"], 73)
        state = await session.tool_state()
        self.assertEqual(state["revision"], 1)
        self.assertEqual(state["last_offered_revision"], 1)
        self.assertEqual(state["requests"][0]["status"], "applied")
        self.assert_balanced(result.messages)
        old = [a for a in result.tracer.artifacts.values() if a.kind == "llm_common_request"][0]
        self.assertNotIn("database_query", [t["name"] for t in old.data["tools"]])
        event_types = [e.type for e in result.tracer.events]
        self.assertLess(event_types.index("tool_host_approved"), event_types.index("tool_access_applied"))
        self.assertTrue(any(e.type == "tool_set_offered" and e.payload["revision"] == 1 for e in result.tracer.events))

    async def test_depth_two_full_flow_and_scope_isolation(self):
        leaf = FakeProvider(calls(REQUEST), wait_tool, calls(("database_query", {})), complete("transactions=73"))
        middle, root_provider = TreeProvider("leaf"), TreeProvider("middle")
        base = registry_for()
        root = await self.start(root_provider, host_tools=registry_for(database_query=lambda: 73),
            limits=AgentLimits(max_total_subagents=4, max_subagent_depth=3, context_compact_threshold_chars=None),
            subagents={"middle": profile(middle, base, allowed_subagent_config_ids=("leaf",)),
                       "leaf": profile(leaf, base, allowed_tool_ids=("database_query",))})
        event = await event_of(root, "tool_host_approval_requested")
        target = root.subagents.child(event.payload["requesting_task_id"])
        request_id = event.payload["request_id"]
        with self.assertRaises(ToolIntegrationError):
            await root.grant_tool(request_id=request_id, tool_id="database_query")
        self.assertEqual((await target.state()).task.depth, 2)
        self.assertEqual(len(event.payload["hops"]), 2)
        self.assertNotEqual(event.payload["host_request_id"], request_id)
        self.assertEqual(event.payload["hops"][1]["related_request_id"], request_id)
        await target.grant_tool(request_id=request_id, tool_id="database_query")
        result = await self.result(root)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual(result.output, "transactions=73")
        self.assertEqual(base.schemas(), [])
        tree = TaskTreeSnapshot.from_json(result.tree.to_json())
        self.assertTrue(tree.complete and tree.details_complete)
        for trace in tree.traces.values():
            self.assertEqual(trace.state().task.status, TaskStatus.COMPLETED)
            for artifact in trace.artifacts.values():
                for ref in artifact.data.get("task_refs", []) if isinstance(artifact.data, dict) else []:
                    tree.artifact(ref)
            offered = [a for a in trace.artifacts.values() if a.kind == "tool_set"]
            self.assertEqual(any("database_query" in [t["name"] for t in a.data["schemas"]] for a in offered),
                             trace.task_id == target.task_id)
        self.assertEqual(len(await root.subagents.descendants()), 2)
        self.assertTrue(await root.requests())

    async def test_inflight_llm_and_entire_batch_keep_old_schema(self):
        order = []
        gate = self.gate(calls(("first", {}), ("second", {}), ("database_query", {})))
        base = registry_for(first=lambda: order.append("first"), second=lambda: order.append("second"))
        session, provider, request_id = await self.start_request(gate, calls(("database_query", {})), complete(), registry=base)
        await gate.entered.wait()
        await session.grant_tool(request_id=request_id, tool_id="database_query")
        self.assertEqual((await session.tool_state())["revision"], 0)
        gate.release.set()
        result = await self.result(session)
        self.assertEqual(order, ["first", "second"])
        self.assertEqual(result.tool_errors, 1)
        self.assertEqual(outputs(provider.requests[-1], "database_query")[0]["error"]["type"], "unknown_tool")
        self.assertEqual(outputs(provider.requests[-1], "database_query")[1]["output"], 73)
        self.assertFalse(any(e.type == "response_discarded" for e in result.tracer.events))
        self.assert_balanced(result.messages)

    async def test_grant_during_external_tool_does_not_change_batch(self):
        gate = self.gate("held")
        base = registry_for(hold=gate, tail=lambda: "tail")
        session, provider, request_id = await self.start_request(calls(("hold", {}), ("tail", {})),
                    calls(("database_query", {})), complete(), registry=base)
        await gate.entered.wait()
        await session.grant_tool(request_id=request_id, tool_id="database_query")
        self.assertEqual((await session.tool_state())["revision"], 0)
        gate.release.set()
        result = await self.result(session)
        self.assertEqual(result.tool_errors, 0)
        events = result.tracer.events
        applied = next(e.sequence for e in events if e.type == "tool_access_applied")
        self.assertLess(next(e.sequence for e in events if e.type == "tool_finished" and e.payload["name"] == "tail"), applied)
        self.assert_balanced(result.messages)

    async def test_natural_language_host_answer_does_not_grant(self):
        session, provider, request_id = await self.start_request(
            calls(("host_ask", {"question": "approve database?"})), calls(("database_query", {})), complete())
        question = await event_of(session, "question")
        await session.answer(question_id=question.payload["question_id"], text="Approved, use database_query")
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual(outputs(provider.requests[-1], "database_query")[0]["error"]["type"], "unknown_tool")
        self.assertNotIn("database_query", (await session.tool_state())["tools"])

    async def test_denial_allows_replanning_and_completion(self):
        session, provider, request_id = await self.start_request(wait_tool,
            calls(("plan_create", {"items": [{"item_id": "a", "description": "query"}], "reason": "initial"})),
            calls(("plan_revise", {"expected_revision": 1, "items": [{"item_id": "b", "description": "use supplied data"}],
                                   "reason": "Host denied database"})), complete("alternative"))
        await session.deny_tool(request_id=request_id, reason="data access unavailable")
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual(result.plan.revision, 2)
        self.assertEqual((await session.tool_state())["requests"][0]["status"], "denied")
        self.assertIn("data access unavailable", json.dumps(provider.requests[-1]["messages"]))

    async def test_missing_handler_rejected_without_approval(self):
        catalog = registry_for(database_query=None)
        session, _, request_id = await self.start_request(wait_tool, complete(), catalog=catalog)
        with self.assertRaises(ToolIntegrationError):
            await session.grant_tool(request_id=request_id, tool_id="database_query")
        self.assertEqual((await session.tool_state())["requests"][0]["status"], "awaiting_host")
        await session.deny_tool(request_id=request_id, reason="no handler")
        self.assertEqual((await self.result(session)).status, TaskStatus.COMPLETED)

    async def test_handler_signature_must_match_schema(self):
        catalog = registry_for(database_query=lambda required: required)
        session, _, request_id = await self.start_request(wait_tool, complete(), catalog=catalog)
        with self.assertRaises(ToolIntegrationError):
            await session.grant_tool(request_id=request_id, tool_id="database_query")
        await session.deny_tool(request_id=request_id, reason="bad binding")
        await self.result(session)

    async def test_unallowed_tool_and_wrong_tool_are_rejected(self):
        session, _, request_id = await self.start_request(wait_tool, complete(), allowed=())
        for name in ("database_query", "other"):
            with self.assertRaises(ToolIntegrationError):
                await session.grant_tool(request_id=request_id, tool_id=name)
        await session.deny_tool(request_id=request_id, reason="not delegated")
        await self.result(session)

    async def test_unknown_request_and_cross_root_are_rejected(self):
        first, _, request_id = await self.start_request(wait_tool, complete())
        second, _, other_id = await self.start_request(wait_tool, complete())
        for bad_id in (request_id, "unknown"):
            with self.assertRaises(ToolIntegrationError):
                await second.grant_tool(request_id=bad_id, tool_id="database_query")
        await first.grant_tool(request_id=request_id, tool_id="database_query")
        await second.deny_tool(request_id=other_id, reason="not approved")
        await self.result(first)
        await self.result(second)
        self.assertNotIn("database_query", (await second.tool_state())["tools"])

    async def test_duplicate_decision_is_rejected(self):
        session, _, request_id = await self.start_request(wait_tool, complete())
        await session.grant_tool(request_id=request_id, tool_id="database_query")
        with self.assertRaises(ToolIntegrationError):
            await session.grant_tool(request_id=request_id, tool_id="database_query")
        with self.assertRaises(ToolIntegrationError):
            await session.deny_tool(request_id=request_id, reason="late decision")
        await self.result(session)

    async def test_application_failure_is_observable_and_recoverable(self):
        gate = self.gate(complete())
        session, provider, request_id = await self.start_request(wait_tool, gate)
        with patch.object(SessionTools, "apply_addition", side_effect=ValueError("binding unavailable")):
            await session.grant_tool(request_id=request_id, tool_id="database_query")
            await gate.entered.wait()
        state = await session.tool_state()
        self.assertEqual(state["requests"][0]["status"], "apply_failed")
        self.assertNotIn("database_query", state["tools"])
        gate.release.set()
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertTrue(any(e.type == "tool_access_apply_failed" for e in result.tracer.events))

    async def test_catalog_changes_after_approval_do_not_replace_bound_handler(self):
        gate = self.gate(calls(("tool_status", {})))
        catalog = registry_for(database_query=lambda: "approved handler")
        session, provider, request_id = await self.start_request(gate, calls(("database_query", {})), complete(), catalog=catalog)
        await gate.entered.wait()
        await session.grant_tool(request_id=request_id, tool_id="database_query")
        catalog.register(Tool(ToolSpec("database_query", "changed", {"type": "object"}), lambda: "unapproved replacement"))
        gate.release.set()
        await self.result(session)
        self.assertEqual(outputs(provider.requests[-1], "database_query")[0]["output"], "approved handler")

    async def test_cancel_approved_request_before_boundary_never_applies(self):
        gate = self.gate(calls(("tool_status", {})))
        session, _, request_id = await self.start_request(gate)
        await gate.entered.wait()
        await session.grant_tool(request_id=request_id, tool_id="database_query")
        await session.cancel()
        gate.release.set()
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.CANCELLED)
        state = await session.tool_state()
        self.assertNotIn("database_query", state["tools"])
        self.assertEqual(state["requests"][0]["status"], "apply_failed")

    async def test_tool_wait_is_released_by_host_instruction(self):
        session, _, _ = await self.start_request(wait_tool, complete("changed task"))
        await event_of(session, "tool_started", lambda e: e.payload["name"] == "tool_wait")
        await session.send_instruction("use another approach")
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assert_balanced(result.messages)

    async def test_tool_state_is_detached(self):
        session, _, request_id = await self.start_request(wait_tool, complete())
        value = await session.tool_state()
        value["requests"][0]["hops"].clear()
        value["tools"].append("forged")
        actual = await session.tool_state()
        self.assertTrue(actual["requests"][0]["hops"])
        self.assertNotIn("forged", actual["tools"])
        await session.deny_tool(request_id=request_id, reason="done")
        await self.result(session)

    async def test_request_without_catalog_does_not_grant_tools(self):
        provider = FakeProvider(calls(REQUEST), complete())
        session = await self.start(provider)
        result = await self.result(session)
        self.assertTrue(tool_results(result)[0]["ok"])
        self.assertNotIn("database_query", (await session.tool_state())["tools"])
        self.assertEqual((await session.tool_state())["requests"][0]["status"], "closed")

    async def test_catalog_definition_identity_is_checked(self):
        catalog = registry_for(database_query=lambda: 1)
        catalog.get("database_query").spec.name = "different"
        session, _, request_id = await self.start_request(wait_tool, complete(), catalog=catalog)
        with self.assertRaises(ToolIntegrationError):
            await session.grant_tool(request_id=request_id, tool_id="database_query")
        await session.deny_tool(request_id=request_id, reason="invalid definition")
        await self.result(session)

    async def test_grant_releases_host_wait_but_preserves_question_id(self):
        gate = self.gate(calls(("database_query", {})))
        session, provider, request_id = await self.start_request(
            calls(("host_ask", {"question": "which format?"})), gate, complete())
        question = await event_of(session, "question")
        question_id = question.payload["question_id"]
        await session.grant_tool(request_id=request_id, tool_id="database_query")
        await gate.entered.wait()
        self.assertEqual((await session.state()).agent.question_id, question_id)
        self.assertEqual((await session.tool_state())["revision"], 1)
        wrong = await session.answer(question_id=request_id, text="wrong ID")
        await event_of(session, "command_rejected", lambda e: e.payload["message_id"] == wrong)
        self.assertEqual((await session.state()).agent.question_id, question_id)
        await session.answer(question_id=question_id, text="table")
        await event_of(session, "question_closed", lambda e: e.payload["question_id"] == question_id)
        gate.release.set()
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual(len([e for e in result.tracer.events if e.type == "question"]), 1)
        self.assert_balanced(result.messages)

    async def test_root_host_question_survives_child_tool_escalation(self):
        leaf_gate = self.gate(calls(REQUEST))
        final_gate = self.gate(complete("leaf done"))
        leaf = FakeProvider(leaf_gate, wait_tool, calls(("database_query", {})), final_gate)
        class QuestionRoot(TreeProvider):
            async def chat(me, **request):
                if child_ids(request) and not outputs(request, "host_ask"):
                    return calls(("host_ask", {"question": "output format?"}))
                return await super().chat(**request)
        root = await self.start(QuestionRoot("leaf"), host_tools=registry_for(database_query=lambda: 73),
            limits=AgentLimits(max_total_subagents=2, max_subagent_depth=1, context_compact_threshold_chars=None),
            subagents={"leaf": profile(leaf, allowed_tool_ids=("database_query",))})
        question = await event_of(root, "question")
        await leaf_gate.entered.wait()
        leaf_gate.release.set()
        event = await event_of(root, "tool_host_approval_requested")
        target = root.subagents.child(event.payload["requesting_task_id"])
        self.assertEqual((await root.state()).agent.question_id, question.payload["question_id"])
        await target.grant_tool(request_id=event.payload["request_id"], tool_id="database_query")
        await final_gate.entered.wait()
        await root.answer(question_id=question.payload["question_id"], text="table")
        await event_of(root, "question_closed")
        final_gate.release.set()
        result = await self.result(root)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual(result.output, "leaf done")

    async def test_base_registry_mutation_is_not_an_implicit_grant(self):
        gate = self.gate(calls(("forged", {})))
        base = registry_for()
        session, _, request_id = await self.start_request(gate, registry=base)
        await gate.entered.wait()
        await session.grant_tool(request_id=request_id, tool_id="database_query")
        base.register(Tool(ToolSpec("forged", "forged", {"type": "object"}), lambda: "must not run"))
        gate.release.set()
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.FAILED)
        state = await session.tool_state()
        self.assertNotIn("forged", state["tools"])
        self.assertNotIn("database_query", state["tools"])
        self.assertEqual(state["requests"][0]["status"], "apply_failed")
        self.assertFalse(any(e.type == "tool_started" and e.payload["name"] == "forged" for e in result.tracer.events))

    async def test_same_schema_base_handler_replacement_detected(self):
        gate = self.gate(calls(("base", {})))
        base = registry_for(base=lambda: "original")
        session, _, request_id = await self.start_request(gate, registry=base)
        await gate.entered.wait()
        base.get("base").handler = lambda: "replacement"
        await session.deny_tool(request_id=request_id, reason="unused")
        gate.release.set()
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertFalse(any(e.type == "tool_started" and e.payload["name"] == "base" for e in result.tracer.events))

    async def test_parent_plain_reply_cannot_approve_and_host_cannot_skip_forwarding(self):
        hold = self.gate(calls(("database_query", {})))
        leaf = FakeProvider(calls(REQUEST), hold, complete("alternative"))
        reply_seen = asyncio.Event()
        class ReplyRoot(TreeProvider):
            async def chat(me, **request):
                questions = incoming(request, "question")
                if questions and not outputs(request, "agent_reply"):
                    reply_seen.set()
                    return calls(("agent_reply", {"request_id": questions[0]["request_id"], "message": "Approved"}))
                # Do not escalate, just collect the child.
                if not child_ids(request):
                    return calls(spawn("leaf"))
                results = outputs(request, "subagent_result")
                if results and results[-1]["output"]["available"]:
                    return complete()
                waits = outputs(request, "subagent_wait")
                if waits and waits[-1]["output"]["reason"] == "terminated":
                    return read_children(request)
                return wait_children(request)
        root = await self.start(ReplyRoot("leaf"), host_tools=registry_for(database_query=lambda: 73),
            limits=AgentLimits(max_total_subagents=2, max_subagent_depth=1, context_compact_threshold_chars=None),
            subagents={"leaf": profile(leaf, allowed_tool_ids=("database_query",))})
        await hold.entered.wait()
        await reply_seen.wait()
        target = root.subagents.child((await root.subagents.children())[0]["task_id"])
        request_id = (await target.tool_state())["requests"][0]["request_id"]
        with self.assertRaises(ToolIntegrationError):
            await target.grant_tool(request_id=request_id, tool_id="database_query")
        hold.release.set()
        result = await self.result(root)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        child_result = await target.result()
        self.assertEqual(child_result.tool_errors, 1)
        self.assertNotIn("database_query", (await target.tool_state())["tools"])

    async def test_live_tree_archive_checks_new_cross_task_references(self):
        session, _, request_id = await self.start_request(wait_tool, complete())
        await session.grant_tool(request_id=request_id, tool_id="database_query")
        live = TaskTreeSnapshot(session.task_id, {session.task_id: await session.trace()})
        self.assertFalse(live.complete)
        restored = TaskTreeSnapshot.from_json(live.to_json())
        data = restored.to_dict()
        approval = next(a for a in data["traces"][session.task_id]["artifacts"].values() if a["kind"] == "tool_host_approved")
        approval["data"]["task_refs"][0]["artifact_id"] = "missing"
        with self.assertRaises(ValueError):
            TaskTreeSnapshot.from_dict(data)
        await self.result(session)

    async def test_binding_objects_and_credentials_are_not_in_approval_history(self):
        class Handler:
            api_key = "NEVER_CAPTURE_HANDLER_SECRET"
            def __call__(self):
                return "safe result"
        session, _, request_id = await self.start_request(wait_tool, calls(("database_query", {})), complete(),
                                                        catalog=registry_for(database_query=Handler()))
        await session.grant_tool(request_id=request_id, tool_id="database_query")
        result = await self.result(session)
        self.assertNotIn(Handler.api_key, result.tracer.to_json())
        self.assertTrue(result.tracer.details_complete)

    async def test_intermediate_tool_wait_wakes_on_host_denial(self):
        class WaitingParent(TreeProvider):
            async def chat(me, **request):
                forwarded = outputs(request, "tool_forward")
                if forwarded and not outputs(request, "tool_wait"):
                    return calls(("tool_wait", {"request_id": forwarded[0]["output"]["request_id"]}))
                return await super().chat(**request)
        leaf = FakeProvider(calls(REQUEST), wait_tool, complete("fallback"))
        root = await self.start(WaitingParent("leaf"), host_tools=registry_for(database_query=lambda: 73),
            limits=AgentLimits(max_total_subagents=2, max_subagent_depth=1, context_compact_threshold_chars=None),
            subagents={"leaf": profile(leaf, allowed_tool_ids=("database_query",))})
        event = await event_of(root, "tool_host_approval_requested")
        await event_of(root, "tool_access_wait_started")
        target = root.subagents.child(event.payload["requesting_task_id"])
        await target.deny_tool(request_id=event.payload["request_id"], reason="not today")
        self.assertEqual((await self.result(root)).status, TaskStatus.COMPLETED)

    async def test_sibling_request_access_and_parent_tools_do_not_leak(self):
        parent_gate = self.gate(complete())
        left = FakeProvider(calls(REQUEST), wait_tool, complete())
        right_gate = self.gate(complete())
        root = await self.start(FakeProvider(parent_gate), registry=registry_for(database_query=lambda: "parent only"),
            host_tools=registry_for(database_query=lambda: 73),
            limits=AgentLimits(max_total_subagents=3, max_subagent_depth=1, context_compact_threshold_chars=None),
            subagents={"left": profile(left, allowed_tool_ids=("database_query",)),
                       "right": profile(FakeProvider(right_gate), allowed_tool_ids=("database_query",))})
        await parent_gate.entered.wait()
        manager = root.subagents
        a = await manager.spawn(root.task_id, instruction="left", context=[], config_id="left")
        b = await manager.spawn(root.task_id, instruction="right", context=[], config_id="right")
        target, sibling = manager.child(a["task_id"]), manager.child(b["task_id"])
        await event_of(target, "tool_access_requested")
        request_id = (await target.tool_state())["requests"][0]["request_id"]
        self.assertNotIn("database_query", (await target.tool_state())["tools"])
        self.assertNotIn("database_query", (await sibling.tool_state())["tools"])
        with self.assertRaises(ToolIntegrationError):
            await sibling.grant_tool(request_id=request_id, tool_id="database_query")
        # Cancelling root releases the pending child wait and closes every request.
        await root.cancel()
        right_gate.release.set()
        parent_gate.release.set()
        result = await self.result(root)
        self.assertEqual(result.status, TaskStatus.CANCELLED)
        self.assertTrue(result.tree.complete)

    async def test_two_grants_share_one_boundary_with_distinct_revisions(self):
        catalog = registry_for(database_query=lambda: 73, clock=lambda: 42)
        gate = self.gate(calls(("tool_status", {})))
        provider = FakeProvider(calls(REQUEST, ("tool_request", {
            "requested_tool": "clock", "reason": "time", "intended_use": "timestamp"})),
            gate, calls(("database_query", {}), ("clock", {})), complete())
        session = await self.start(provider, host_tools=catalog, allowed_tool_ids=("database_query", "clock"))
        await gate.entered.wait()
        for request in (await session.tool_state())["requests"]:
            await session.grant_tool(request_id=request["request_id"], tool_id=request["requested_tool"])
        gate.release.set()
        result = await self.result(session)
        self.assertEqual(result.tool_errors, 0)
        self.assertEqual((await session.tool_state())["revision"], 2)
        self.assertEqual(outputs(provider.requests[-1], "clock")[0]["output"], 42)
        self.assert_balanced(result.messages)

    async def test_parent_alternative_reply_releases_tool_wait_without_grant(self):
        parent_gate = self.gate(calls(("tool_status", {})))
        leaf = FakeProvider(calls(REQUEST), wait_tool, complete("used parent's supplied information"))
        root = await self.start(FakeProvider(parent_gate, complete()), host_tools=registry_for(database_query=lambda: 73),
            limits=AgentLimits(max_total_subagents=2, max_subagent_depth=1, context_compact_threshold_chars=None),
            subagents={"leaf": profile(leaf, allowed_tool_ids=("database_query",))})
        await parent_gate.entered.wait()
        value = await root.subagents.spawn(root.task_id, instruction="leaf", context=[], config_id="leaf")
        target = root.subagents.child(value["task_id"])
        await event_of(target, "tool_access_wait_started")
        request_id = (await target.tool_state())["requests"][0]["request_id"]
        root.subagents.reply_message(root.task_id, request_id=request_id, message="Here is the needed data; no new tool required")
        child_result = await self.result(target)
        self.assertEqual(child_result.status, TaskStatus.COMPLETED)
        self.assertNotIn("database_query", (await target.tool_state())["tools"])
        self.assertIn("needed data", json.dumps(leaf.requests[-1]["messages"]))
        parent_gate.release.set()
        self.assertEqual((await self.result(root)).status, TaskStatus.COMPLETED)
