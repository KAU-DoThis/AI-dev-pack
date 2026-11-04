import copy
import json
import unittest
from dataclasses import FrozenInstanceError

from shs_agent import (AgentActivity, AgentLimits, PlanError, PlanItemStatus,
                       ProblemSolver, SessionState, TaskRequest, TaskStatus, TracerSnapshot)
from shs_agent.agent.context import ContextBuilder, TaskContext
from shs_agent.agent.planning import Planning
from shs_agent.llm import LLMResponse

from tests.fakes import AsyncCase, FakeProvider, calls, complete, event_of, registry_for, tool_results


PLAN_EVENTS = {"plan_created", "plan_updated", "plan_revised"}
LIMITS = AgentLimits(context_compact_threshold_chars=None)


def item(item_id="research", description="Investigate", **kwargs):
    return {"item_id": item_id, "description": description, **kwargs}


def create(*items):
    return calls(("plan_create", {"items": list(items) or [item()], "reason": "Organize this task"}))


def current_plan(request):
    messages = [m for m in request["messages"] if m.get("content", "").startswith("Current task plan")]
    if not messages:
        return None
    assert len(messages) == 1
    assert messages[0]["role"] == "assistant"
    return json.loads(messages[0]["content"].split("\n", 1)[1])


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.planning = Planning("task-a")

    def test_create_read_and_detached_serialization(self):
        source = [item(source_refs=["message:3"])]
        first = self.planning.create(source, "initial", ["file:input.py"])
        source[0]["source_refs"].append("tampered")
        source.append(item("later"))
        self.assertEqual(first.revision, 1)
        self.assertEqual(first.task_id, "task-a")
        self.assertEqual(first.items[0].source_refs, ("message:3",))
        data = first.to_dict()
        data["items"][0]["source_refs"].append("changed copy")
        self.assertEqual(self.planning.snapshot(1), first)
        with self.assertRaises(FrozenInstanceError):
            first.items[0].status = PlanItemStatus.FAILED
        with self.assertRaises(PlanError):
            self.planning.create([item("replacement")], "overwrite")
        self.assertEqual(self.planning.snapshot(), first)

    def test_invalid_creation_is_atomic_and_recoverable(self):
        invalid = [
            [item(), item()], [item(depends_on=["missing"])],
            [item(depends_on=["research"])],
            [item("a", depends_on=["b"]), item("b", depends_on=["a"])],
            [item("a"), item("b", depends_on=["a", "a"])],
            [item(status="verified")], [item(description=" ")],
            [item(source_refs="wrong")], [item(status_note=None)],
            [item(extra=True)], [None], None,
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(PlanError):
                self.planning.create(value, "initial")
            self.assertIsNone(self.planning.snapshot())
        self.assertEqual(self.planning.create([item()], "valid").revision, 1)

    def test_bad_batch_update_keeps_all_previous_values(self):
        before = self.planning.create([item(), item("edit", "Fix")], "initial")
        with self.assertRaises(PlanError):
            self.planning.update([{"item_id": "research", "status": "completed"},
                                  {"item_id": "missing", "status": "completed"}], 1, "invalid")
        self.assertEqual(self.planning.snapshot(), before)
        after = self.planning.update([{"item_id": "research", "status": "completed"}], 1, "fixed")
        self.assertEqual(after.revision, 2)
        self.assertEqual(after.items[1], before.items[1])

    def test_revision_checks_missing_plan_and_noop(self):
        self.assertIsNone(self.planning.snapshot())
        with self.assertRaises(PlanError):
            self.planning.update([], 1, "missing")
        first = self.planning.create([item()], "initial")
        for revision in (True, 0, -1, 2, "1", 1.0):
            with self.subTest(revision=revision), self.assertRaises(PlanError):
                self.planning.update([], revision, "stale or invalid")
            with self.assertRaises(PlanError):
                self.planning.snapshot(revision)
        self.assertIs(self.planning.update([{"item_id": "research"}], 1, "repeat"), first)
        self.assertIs(self.planning.revise([{"item_id": "research"}], 1, "repeat"), first)
        self.assertIs(self.planning.update([], 1, "empty patch"), first)

    def test_revise_preserves_completed_results_and_excluded_history(self):
        self.planning.create([item(), item("verify", "Run tests", depends_on=["research"])], "initial")
        completed = self.planning.update([{"item_id": "research", "status": "completed",
                                          "source_refs": ["tool:research-call"], "status_note": "Found cause"}],
                                         1, "Research done")
        revised = self.planning.revise([{"item_id": "research"},
                                        item("review", "Static review", depends_on=["research"])],
                                       2, "Test runtime unavailable", ["tool:test-error"])
        self.assertEqual(revised.items[0], completed.items[0])
        self.assertEqual([x.item_id for x in self.planning.snapshot(2).items], ["research", "verify"])
        self.assertEqual(revised.plan_id, completed.plan_id)
        self.assertEqual(revised.source_refs, ("tool:test-error",))
        with self.assertRaises(PlanError):
            self.planning.revise([item("verify", "Different meaning")], 3, "ID reuse")
        self.assertEqual(self.planning.snapshot(), revised)

    def test_rework_requires_reason_and_keeps_prior_completion(self):
        first = self.planning.create([item(status="completed", source_refs=["message:3"])], "initial")
        with self.assertRaises(PlanError):
            self.planning.update([{"item_id": "research", "status": "pending"}], 1, " ")
        rework = self.planning.update([{"item_id": "research", "status": "in_progress",
                                       "status_note": "New evidence needs checking"}], 1, "Source changed")
        self.assertEqual(rework.reason, "Source changed")
        self.assertEqual(rework.items[0].source_refs, first.items[0].source_refs)
        self.assertEqual(self.planning.snapshot(1).items[0].status, PlanItemStatus.COMPLETED)

    def test_dependency_edits_require_revise_and_validate_whole_graph(self):
        before = self.planning.create([item("a"), item("b", depends_on=["a"])], "initial")
        for operation in (
            lambda: self.planning.update([{"item_id": "b", "depends_on": []}], 1, "wrong tool"),
            lambda: self.planning.revise([{"item_id": "a", "depends_on": ["b"]}, {"item_id": "b"}], 1, "cycle"),
            lambda: self.planning.revise([{"item_id": "b"}], 1, "dangling"),
            lambda: self.planning.update([{"item_id": "a"}, {"item_id": "a"}], 1, "duplicate"),
        ):
            with self.assertRaises(PlanError):
                operation()
            self.assertEqual(self.planning.snapshot(), before)
        after = self.planning.revise([{"item_id": "b", "depends_on": []}], 1, "Independent alternative")
        self.assertEqual(after.items[0].depends_on, ())

    def test_dependencies_do_not_schedule_or_enforce_one_active_item(self):
        plan = self.planning.create([item("a", status="in_progress"),
                                     item("b", status="in_progress", depends_on=["a"])], "parallel descriptions")
        self.assertEqual(len(plan.items), 2)
        done = self.planning.update([{"item_id": "b", "status": "completed"}], 1, "LLM assessment")
        self.assertEqual(done.items[0].status, PlanItemStatus.IN_PROGRESS)

    def test_large_dependency_chain_and_empty_replacement(self):
        items = [item(str(i), depends_on=[str(i - 1)] if i else []) for i in range(1500)]
        before = self.planning.create(items, "long plan")
        after = self.planning.revise([], 1, "All work superseded")
        self.assertEqual(after.items, ())
        self.assertEqual(len(self.planning.snapshot(1).items), len(before.items))

    def test_publish_failure_preserves_owner_revision(self):
        def reject(*args):
            raise PlanError("unresolved artifact")
        planning = Planning("task-a", publish=reject)
        with self.assertRaises(PlanError):
            planning.create([item()], "initial")
        self.assertIsNone(planning.snapshot())

    def test_builder_preserves_source_and_only_inserts_current_snapshot(self):
        context = TaskContext("task-a", TaskRequest("original", system_prompt="system"), LIMITS)
        context.memo.write("known fact", "fact", ["message:0"], step=0)
        context.conversation.add_instruction("host constraint")
        self.planning.create([item(description="old plan description")], "initial")
        plan = self.planning.update([{"item_id": "research", "description": "new description"}], 1, "Clarify")
        state = context.snapshot(plan=plan)
        before = copy.deepcopy(state)
        builder = ContextBuilder(LIMITS)
        built = builder.build(state)
        self.assertNotIn("old plan description", str(built))
        self.assertEqual(built[0], {"role": "system", "content": "system"})
        self.assertEqual(current_plan({"messages": built}), plan.to_dict())
        self.assertEqual([m["content"] for m in built if m["role"] == "user"], ["original", "host constraint"])
        built[0]["content"] = "mutated"
        self.assertEqual(state, before)
        self.assertEqual(builder.build(state)[0]["content"], "system")
        self.assertIsNone(context.snapshot().plan)  # Context never owns a stored plan.
        self.assertEqual(len(context.memo.snapshot()), 1)


class PlanningRuntimeTests(AsyncCase):
    async def test_no_plan_task_keeps_original_execution_and_result(self):
        provider = FakeProvider(LLMResponse("intermediate"), complete("finished"))
        session = await self.start(provider, limits=LIMITS)
        result = await self.result(session)
        self.assertEqual((result.status, result.output, result.steps, result.tool_calls),
                         (TaskStatus.COMPLETED, "finished", 2, 1))
        self.assertIsNone(result.plan)
        self.assertIsNone((await session.state()).task.plan_id)
        self.assertFalse([e for e in result.tracer.events if e.type in PLAN_EVENTS])
        self.assertFalse([a for a in result.tracer.artifacts.values() if a.kind == "plan_revision"])
        for request in provider.requests:
            self.assertIsNone(current_plan(request))

    async def test_environment_failure_replan_preserves_work_and_full_trace(self):
        holder = {}
        cause = {}

        async def record_research(request):
            event = await event_of(holder["session"], "tool_finished", lambda e: e.payload["name"] == "investigate")
            cause["research"] = event.payload["result_ref"]["artifact_id"]
            return calls(("plan_update", {"updates": [{"item_id": "research", "status": "completed",
                           "source_refs": ["artifact:" + cause["research"]], "status_note": "Cause identified"}],
                           "expected_revision": 1, "reason": "Research found the affected function"}),
                         ("memo_write", {"content": "Affected function found", "kind": "fact"}))

        async def replan(request):
            plan = current_plan(request)
            self.assertEqual(plan["revision"], 3)
            self.assertEqual(plan["items"][0]["status"], "completed")
            self.assertEqual(plan["items"][2]["status"], "pending")
            self.assertIn("RuntimeError", request["messages"][-1]["content"])
            self.assertIn("test runtime unavailable", request["messages"][-1]["content"])
            event = await event_of(holder["session"], "tool_finished", lambda e: e.payload["name"] == "verify")
            cause["error"] = event.payload["result_ref"]["artifact_id"]
            return calls(("plan_revise", {"expected_revision": 3, "reason": "Test runtime unavailable; use permitted static review",
                "source_refs": ["artifact:" + cause["error"]], "items": [
                    {"item_id": "research"}, {"item_id": "edit"},
                    item("review", "Review statically; report tests as unexecuted", depends_on=["edit"]),
                ]}))

        def review(request):
            plan = current_plan(request)
            self.assertEqual(plan["revision"], 4)
            self.assertEqual([i["item_id"] for i in plan["items"]], ["research", "edit", "review"])
            self.assertEqual(plan["items"][0]["source_refs"], ["artifact:" + cause["research"]])
            return calls(("static_review", {}))

        def fail_verification():
            raise RuntimeError("test runtime unavailable")

        provider = FakeProvider(
            create(item(), item("edit", "Apply fix", depends_on=["research"]),
                   item("verify", "Run tests", depends_on=["edit"])),
            calls(("investigate", {})), record_research, calls(("edit", {})),
            calls(("plan_update", {"updates": [{"item_id": "edit", "status": "completed"}],
                                   "expected_revision": 2, "reason": "Fix applied"}), ("verify", {})),
            replan, review,
            calls(("plan_update", {"expected_revision": 4, "reason": "Static review done; tests not executed",
                "updates": [{"item_id": "review", "status": "completed",
                             "status_note": "Static review only; runtime tests remain unverified"}]})),
            complete("Fix and static review complete. Runtime tests were not executed."),
        )
        session = await self.start(provider, limits=LIMITS, registry=registry_for(
            investigate=lambda: "Affected function found", edit=lambda: "Fake edit applied",
            verify=fail_verification, static_review=lambda: {"tests_executed": False, "review": "no issue found"}))
        holder["session"] = session
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual((result.plan.revision, result.tool_errors), (5, 1))
        self.assertIn("not executed", result.output)
        self.assertEqual(len(result.memos), 1)
        self.assert_balanced(result.messages)
        for request in provider.requests:
            self.assert_balanced(request["messages"])

        # The exported trace alone resolves detail and every historical revision.
        trace = TracerSnapshot.from_json(result.tracer.to_json())
        self.assertEqual(trace.to_json(), result.tracer.to_json())
        self.assertTrue(trace.complete)
        self.assertTrue(trace.details_complete)
        changes = [e for e in trace.events if e.type in PLAN_EVENTS]
        self.assertEqual([e.type for e in changes],
                         ["plan_created", "plan_updated", "plan_updated", "plan_revised", "plan_updated"])
        self.assertEqual([e.payload["previous_revision"] for e in changes], [None, 1, 2, 3, 4])
        self.assertEqual(len([a for a in trace.artifacts.values() if a.kind == "plan_revision"]), 5)
        previous_id = None
        for event in changes:
            payload = event.payload
            self.assertEqual(payload["before_artifact_id"], previous_id)
            after = trace.artifact(payload["after_artifact_id"])
            self.assertEqual(after.data["revision"], payload["revision"])
            self.assertEqual(after.operation_id, payload["tool_execution_id"])
            request = trace.artifact(payload["request_artifact_id"])
            self.assertEqual(request.data["llm_call_id"], payload["llm_call_id"])
            self.assertIn(payload["request_artifact_id"], after.references)
            response = trace.artifact(request.data["response_artifact_id"])
            self.assertTrue(response.data["tool_calls"])
            self.assertTrue(any(a.kind == "llm_common_request" and a.operation_id == payload["llm_call_id"]
                                for a in trace.artifacts.values()))
            if previous_id:
                self.assertIn(previous_id, after.references)
            previous_id = after.artifact_id
        revision4 = trace.artifact(changes[3].payload["after_artifact_id"])
        self.assertIn(cause["error"], revision4.references)
        self.assertEqual(trace.artifact(cause["error"]).data["status"], "failed")
        revision3 = trace.artifact(changes[3].payload["before_artifact_id"])
        self.assertIn("verify", [i["item_id"] for i in revision3.data["items"]])
        # Reconstruct every point, including before creation and the exact change events.
        expected = None
        for event in trace.events:
            if event.type in PLAN_EVENTS:
                expected = event.payload["after_artifact_id"]
            state = trace.state(at_sequence=event.sequence)
            self.assertEqual(state.task.plan_artifact_id, expected)
            if expected:
                self.assertEqual(trace.artifact(expected).data["revision"], state.task.plan_revision)
        self.assertEqual(trace.artifact(expected).data, result.plan.to_dict())

    async def test_rejected_tools_recover_without_success_events_or_revision_changes(self):
        provider = FakeProvider(
            create(item(), item()), create(),
            create(item("overwrite")),
            calls(("plan_update", {"updates": [{"item_id": "missing", "status": "completed"}],
                                    "expected_revision": 1, "reason": "bad update"})),
            calls(("plan_revise", {"items": [item(depends_on=["research"])],
                                    "expected_revision": 1, "reason": "bad graph"})),
            calls(("plan_update", {"updates": [{"item_id": "research", "status": "completed"}],
                                    "expected_revision": 1, "reason": "recovered"})), complete(),
        )
        session = await self.start(provider, limits=LIMITS)
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual((result.plan.revision, result.tool_errors), (2, 4))
        self.assertEqual([e.type for e in result.tracer.events if e.type in PLAN_EVENTS],
                         ["plan_created", "plan_updated"])
        for index in (0, 2, 3, 4):
            self.assertEqual(tool_results(result)[index]["error"]["type"], "PlanError")
        self.assertEqual(current_plan(provider.requests[5])["revision"], 1)

    async def test_read_historical_revision_and_noop_do_not_mutate(self):
        provider = FakeProvider(
            calls(("plan_read", {})), create(),
            calls(("plan_update", {"updates": [{"item_id": "research", "status": "completed"}],
                                    "expected_revision": 1, "reason": "done"})),
            calls(("plan_read", {"revision": 1}), ("plan_read", {}),
                  ("plan_update", {"updates": [], "expected_revision": 2, "reason": "no change"}),
                  ("plan_revise", {"items": [{"item_id": "research"}], "expected_revision": 2, "reason": "repeat"})),
            complete(),
        )
        result = await self.result(await self.start(provider, limits=LIMITS))
        outputs = tool_results(result)
        self.assertIsNone(outputs[0]["output"]["plan"])
        self.assertEqual(outputs[3]["output"]["plan"]["items"][0]["status"], "pending")
        self.assertEqual(outputs[4]["output"]["plan"]["revision"], 2)
        self.assertFalse(outputs[5]["output"]["changed"])
        self.assertFalse(outputs[6]["output"]["changed"])
        self.assertEqual(len([e for e in result.tracer.events if e.type in PLAN_EVENTS]), 2)

    async def test_unknown_artifact_rejection_does_not_publish_partial_change(self):
        provider = FakeProvider(create(), calls(("plan_update", {
            "updates": [{"item_id": "research", "status": "completed"}], "expected_revision": 1,
            "reason": "unresolved evidence", "source_refs": ["artifact:unknown"]})), complete())
        result = await self.result(await self.start(provider, limits=LIMITS))
        self.assertEqual(result.plan.revision, 1)
        self.assertEqual(result.plan.items[0].status, PlanItemStatus.PENDING)
        self.assertEqual(len([a for a in result.tracer.artifacts.values() if a.kind == "plan_revision"]), 1)
        self.assertEqual(tool_results(result)[1]["error"]["type"], "PlanError")
        TracerSnapshot.from_json(result.tracer.to_json())

    async def test_live_state_cursor_and_returned_copy_isolation(self):
        gate = self.gate(complete())
        session = await self.start(FakeProvider(create(), gate), limits=LIMITS)
        initial = await session.state()
        self.assertIsNone(initial.task.plan_id)
        await gate.entered.wait()
        state = await session.state()
        self.assertEqual(state.task.plan_revision, 1)
        self.assertEqual(state.agent.activity, AgentActivity.LLM_CALL)
        artifact = await session.artifact(state.task.plan_artifact_id)
        self.assertEqual((artifact.data["plan_id"], artifact.data["revision"]),
                         (state.task.plan_id, state.task.plan_revision))
        artifact.data["items"][0]["description"] = "tampered"
        self.assertEqual((await session.artifact(artifact.artifact_id)).data["items"][0]["description"], "Investigate")
        gate.release.set()
        result = await self.result(session)
        replay = [e async for e in session.events(after_sequence=state.last_event_sequence)]
        self.assertEqual([e.sequence for e in replay],
                         list(range(state.last_event_sequence + 1, len(result.tracer.events) + 1)))
        self.assertEqual(result.tracer.state(at_sequence=state.last_event_sequence), state)
        self.assertEqual(result.tracer.state(), await session.state())
        result.plan.to_dict()["items"][0]["description"] = "changed result"
        result.tracer.artifacts[artifact.artifact_id].data["items"].clear()
        fresh = await session.result()
        self.assertEqual(fresh.plan.items[0].description, "Investigate")
        self.assertEqual(len(fresh.tracer.artifact(artifact.artifact_id).data["items"]), 1)

    async def test_two_tasks_have_independent_plan_state_and_artifacts(self):
        solver = ProblemSolver()
        gates = [self.gate(complete("first")), self.gate(complete("second"))]
        sessions = []
        for index, gate in enumerate(gates):
            session = await solver.start(str(index), model="fake", provider=FakeProvider(
                create(item(description=f"task-{index}")), gate), limits=LIMITS)
            self.sessions.append(session)
            sessions.append(session)
        for gate in gates:
            await gate.entered.wait()
        states = [await session.state() for session in sessions]
        self.assertNotEqual(states[0].task.plan_id, states[1].task.plan_id)
        self.assertNotEqual(states[0].task.plan_artifact_id, states[1].task.plan_artifact_id)
        with self.assertRaises(KeyError):
            await sessions[0].artifact(states[1].task.plan_artifact_id)
        for index, (gate, session) in enumerate(zip(gates, sessions)):
            gate.release.set()
            result = await self.result(session)
            self.assertEqual(result.plan.task_id, session.task_id)
            self.assertEqual(result.plan.items[0].description, f"task-{index}")
            self.assertTrue(all(a.task_id == session.task_id for a in result.tracer.artifacts.values()))

    async def test_host_instruction_requires_llm_replan_at_existing_boundary(self):
        stale = self.gate(calls(("plan_revise", {"items": [item("stale")],
                                                "expected_revision": 1, "reason": "stale proposal"})))

        def react(request):
            self.assertIn({"role": "user", "content": "Use static validation"}, request["messages"])
            self.assertEqual(current_plan(request)["revision"], 1)
            return calls(("plan_revise", {"items": [item("static", "Static validation")],
                                          "expected_revision": 1, "reason": "Host changed validation conditions"}))

        provider = FakeProvider(create(), stale, react, complete())
        session = await self.start(provider, limits=LIMITS)
        await stale.entered.wait()
        await session.send_instruction("Use static validation")
        self.assertEqual((await session.state()).task.plan_revision, 1)
        stale.release.set()
        result = await self.result(session)
        self.assertEqual(result.plan.revision, 2)
        self.assertEqual(result.plan.items[0].item_id, "static")
        self.assertNotIn("stale", [a.data["items"][0]["item_id"] for a in result.tracer.artifacts.values()
                                  if a.kind == "plan_revision"])

    async def test_item_status_neither_completes_nor_fails_task(self):
        for status in ("pending", "failed", "completed"):
            with self.subTest(status=status):
                provider = FakeProvider(create(item(status=status)), LLMResponse("continue working"), complete())
                result = await self.result(await self.start(provider, limits=LIMITS))
                self.assertEqual((result.status, result.steps), (TaskStatus.COMPLETED, 3))
                self.assertEqual(result.plan.items[0].status.value, status)

    async def test_failure_and_limits_return_current_plan_and_history(self):
        cases = [
            (FakeProvider(create(), RuntimeError("provider failed")), LIMITS, TaskStatus.FAILED, "runtime_error"),
            (FakeProvider(create()), AgentLimits(max_steps=1, context_compact_threshold_chars=None),
             TaskStatus.LIMIT_REACHED, "max_steps"),
            (FakeProvider(create(), complete()), AgentLimits(max_tool_calls=1, context_compact_threshold_chars=None),
             TaskStatus.LIMIT_REACHED, "max_tool_calls"),
            (FakeProvider(create(), calls(("host_ask", {"question": "Need decision"}))),
             AgentLimits(host_response_timeout=0.01, context_compact_threshold_chars=None),
             TaskStatus.INCOMPLETE, "host_response_timeout"),
        ]
        for provider, limits, status, reason in cases:
            with self.subTest(reason=reason):
                result = await self.result(await self.start(provider, limits=limits))
                self.assertEqual((result.status, result.stop_reason), (status, reason))
                self.assertEqual(result.plan.revision, 1)
                self.assertEqual(result.plan.items[0].status, PlanItemStatus.PENDING)
                trace = TracerSnapshot.from_json(result.tracer.to_json())
                self.assertTrue(trace.complete)
                self.assertEqual(trace.artifact(trace.state().task.plan_artifact_id).data, result.plan.to_dict())

    async def test_cancel_during_tool_keeps_current_plan_and_real_activity(self):
        gate = self.gate("tool result")
        provider = FakeProvider(create(), calls(("slow", {})))
        session = await self.start(provider, limits=LIMITS, registry=registry_for(slow=gate))
        await gate.entered.wait()
        before = await session.state()
        await session.cancel()
        during = await session.state()
        self.assertTrue(during.task.cancel_requested)
        self.assertEqual(during.agent.activity, AgentActivity.TOOL_EXECUTION)
        self.assertEqual(during.task.plan_artifact_id, before.task.plan_artifact_id)
        gate.release.set()
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.CANCELLED)
        self.assertEqual(result.plan.revision, 1)
        self.assertTrue(TracerSnapshot.from_json(result.tracer.to_json()).complete)

    async def test_trace_reference_validation_and_legacy_states(self):
        result = await self.result(await self.start(FakeProvider(create(), complete()), limits=LIMITS))
        data = result.tracer.to_dict()
        plan_id = result.tracer.state().task.plan_artifact_id
        data["artifacts"].pop(plan_id)
        with self.assertRaisesRegex(ValueError, "missing artifact"):
            TracerSnapshot.from_dict(data)
        plain = await self.result(await self.start(FakeProvider(complete()), limits=LIMITS))
        old = plain.tracer.to_dict()
        for event in old["events"]:
            if "state" in event["payload"]:
                for key in ("plan_id", "plan_revision", "plan_artifact_id"):
                    event["payload"]["state"]["task"].pop(key)
        restored = TracerSnapshot.from_dict(old)
        self.assertIsNone(restored.state().task.plan_id)
        self.assertEqual(SessionState.from_dict(restored.state().to_dict()), restored.state())

    async def test_no_subscriber_trace_retains_removed_and_reworked_items(self):
        result = await ProblemSolver(FakeProvider(
            create(item(status="completed", source_refs=["file:analysis.txt"]), item("tests", "Run tests")),
            calls(("plan_revise", {"expected_revision": 1, "reason": "Requirements changed; redo affected research",
                "items": [{"item_id": "research", "status": "in_progress"}, item("review", "New validation")]})),
            complete("Remaining validation is unexecuted"),
        )).solve("task without a subscriber", model="fake", limits=LIMITS)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        trace = TracerSnapshot.from_json(result.tracer.to_json())
        revisions = [trace.artifact(e.payload["after_artifact_id"]).data
                     for e in trace.events if e.type in PLAN_EVENTS]
        self.assertEqual(len(revisions), 2)
        self.assertEqual(revisions[0]["items"][0]["status"], "completed")
        self.assertEqual(revisions[1]["items"][0]["status"], "in_progress")
        self.assertEqual(revisions[1]["items"][0]["source_refs"], ["file:analysis.txt"])
        self.assertEqual(revisions[0]["items"][1]["item_id"], "tests")
        self.assertEqual(revisions[1]["items"][1]["item_id"], "review")
        self.assertEqual(result.plan.items[1].status, PlanItemStatus.PENDING)

    async def test_current_plan_survives_conversation_compaction(self):
        provider = FakeProvider(create(), LLMResponse("progress"), LLMResponse("older history summary"), complete())
        result = await self.result(await self.start(provider, limits=AgentLimits(
            context_compact_threshold_chars=256, context_keep_recent_messages=1)))
        self.assertEqual((result.status, result.context_compactions), (TaskStatus.COMPLETED, 1))
        self.assertEqual(provider.requests[2]["tools"], [])
        self.assertEqual(current_plan(provider.requests[3]), result.plan.to_dict())
        self.assertEqual(result.memos, ())
        self.assertEqual(len([e for e in result.tracer.events if e.type in PLAN_EVENTS]), 1)
