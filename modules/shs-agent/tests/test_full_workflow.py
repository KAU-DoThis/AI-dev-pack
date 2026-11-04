"""Phase 8 Host integration; gates establish ordering, timeouts only catch hangs."""
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

from shs_agent import AgentLimits, ProblemSolver, TaskStatus, TaskTreeSnapshot, ToolIntegrationError
from shs_agent.llm import LLMResponse
from examples.full_workflow_host import (TreeObserver, evaluate, host_decisions,
                                        memory_candidates, run_workflow, save_run)
from examples.replay_task_tree import replay
from examples.workflow_support import WorkflowProvider, configuration, NARROW_INSTRUCTION
from tests.fakes import AsyncCase, FakeProvider, calls, complete, event_of, registry_for
from tests.test_subagent import profile, spawn, wait_children, read_children
from tests.test_tool_integration import TreeProvider, REQUEST, wait_tool


class FullWorkflowTests(AsyncCase):
    async def workflow(self, **kwargs):
        return await asyncio.wait_for(run_workflow(**kwargs), 15)

    async def test_full_workflow_live_archive_actual_tools_and_host_review(self):
        result, observer, summary = await self.workflow()
        self.assertEqual(summary["handler_executions"], ["policy_read", "ledger_read"])
        self.assertTrue(summary["business_review"]["accepted"])
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual(json.loads(result.output)["total"], 2500)
        tree = TaskTreeSnapshot.from_json(result.tree.to_json())
        self.assertTrue(tree.complete and tree.details_complete)
        self.assertEqual(len(tree.traces), 4)
        self.assertEqual(sorted(t.state().task.depth for t in tree.traces.values()), [0, 1, 1, 2])
        self.assertTrue(result.memos)
        self.assertEqual(result.plan.items[0].status, "completed")
        observer.compare(tree)
        leaf = next(t for t in tree.traces.values() if t.state().task.depth == 2)
        self.assertEqual(tree.result(leaf.task_id)["output"], result.output)
        self.assertTrue(any(e.type == "plan_revised" for e in leaf.events))
        self.assertEqual(sum(t.state().agent.tool_errors for t in tree.traces.values()), 0)
        statuses = {e.payload["status"] for t in tree.traces.values() for e in t.events
                    if e.type.startswith(("tool_access_", "tool_host_", "tool_approval_")) and "status" in e.payload}
        self.assertTrue({"requested", "awaiting_host", "denied", "approved", "applied"} <= statuses)
        offered = [a for a in leaf.artifacts.values() if a.kind == "llm_common_request"]
        self.assertNotIn("ledger_read", [s["name"] for s in offered[0].data["tools"]])
        self.assertIn("ledger_read", [s["name"] for s in offered[-1].data["tools"]])
        for trace in tree.traces.values():
            self.assertEqual(trace.state().task.status, TaskStatus.COMPLETED)
            for artifact in trace.artifacts.values():
                if isinstance(artifact.data, dict):
                    for ref in artifact.data.get("task_refs", []):
                        tree.artifact(ref)
            if trace is not leaf:
                for artifact in trace.artifacts.values():
                    if artifact.kind == "llm_common_request":
                        self.assertNotIn("ledger_read", [s["name"] for s in artifact.data["tools"]])
        questions = [e for e in result.tracer.events if e.type == "question"]
        self.assertEqual(len(questions), 1)
        answer = next(a for a in summary["host_actions"] if a["action"] == "answer_queued")
        self.assertEqual(answer["question_id"], questions[0].payload["question_id"])
        self.assertTrue(any(e.type == "command_applied" and e.payload["message_id"] == answer["receipt"]
                            for e in result.tracer.events))
        # A -> Root -> B mediation has actual linked request IDs, not fabricated messages.
        self.assertTrue(any(a.kind == "agent_message_requested" and a.data.get("related_request_id")
                            for a in result.tracer.artifacts.values()))
        candidates = memory_candidates(result, summary["business_review"])
        self.assertEqual(len(candidates), 1)
        tree.artifact(candidates[0]["source"])
        with tempfile.TemporaryDirectory() as directory:
            save_run(Path(directory), result, observer, summary)
            restored = TaskTreeSnapshot.from_json((Path(directory) / "task_tree.json").read_text(encoding="utf-8"))
            with patch.object(ProblemSolver, "start", side_effect=AssertionError("replay executed work")), \
                 patch.object(WorkflowProvider, "chat", side_effect=AssertionError("replay called provider")):
                text = replay(restored, details=True)
            self.assertIn("plan_revised", text)
            self.assertIn("2500", text)
            persisted = [json.loads(line) for line in (Path(directory) / "live.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(persisted, json.loads(json.dumps(observer.records)))

    async def test_completed_engine_does_not_imply_business_acceptance_or_memory(self):
        class WrongSummaryProvider(WorkflowProvider):
            def decide(self, role, request):
                response = super().decide(role, request)
                if role == "Root" and response.tool_calls[0].name == "task_complete":
                    return complete('{"total": 999}')
                return response
        result_session = await ProblemSolver(WrongSummaryProvider()).start(
            "Root", model="fake", **configuration([]))
        self.sessions.append(result_session)
        queue = asyncio.Queue()
        observer = TreeObserver(result_session, decisions=queue)
        async with asyncio.TaskGroup() as group:
            group.create_task(observer.run())
            group.create_task(host_decisions(result_session, queue, []))
            result = await asyncio.wait_for(result_session.result(), 15)
        original = result.tree.to_json()
        self.assertEqual(result.output, '{"total": 999}')
        review = evaluate(result)
        self.assertFalse(review["accepted"])
        self.assertEqual(memory_candidates(result, review), [])
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual(result.tree.to_json(), original)
        observer.compare(result.tree)

    async def test_leaf_instruction_during_tool_changes_only_target_plan_and_output(self):
        result, observer, summary = await self.workflow(control="instruction")
        self.assertTrue(summary["business_review"]["accepted"])
        self.assertEqual(json.loads(result.output)["total"], 1200)
        action = next(a for a in summary["host_actions"] if a["action"] == "instruction_queued")
        self.assertEqual(action["before"]["agent"]["activity"], "tool_execution")
        target = result.tree.traces[action["task_id"]]
        received = next(e.sequence for e in target.events if e.type == "command_received"
                        and e.payload["message_id"] == action["receipt"])
        finished = next(e.sequence for e in target.events if e.type == "tool_finished" and e.payload["name"] == "ledger_read")
        applied = next(e.sequence for e in target.events if e.type == "command_applied"
                       and e.payload["message_id"] == action["receipt"])
        self.assertLess(received, finished)
        self.assertLess(finished, applied)
        self.assertEqual(result.tree.result(target.task_id)["plan"]["items"][0]["description"], NARROW_INSTRUCTION)
        for task_id, trace in result.tree.traces.items():
            instructions = [e for e in trace.events if e.type == "command_received" and e.payload["command_type"] == "instruction"]
            self.assertEqual(len(instructions), int(task_id == target.task_id))
        observer.compare(TaskTreeSnapshot.from_json(result.tree.to_json()))

    async def test_cancel_leaf_preserves_handler_result_and_parent_collection(self):
        result, _, summary = await self.workflow(control="cancel")
        self.assertFalse(summary["business_review"]["accepted"])
        tree = result.tree
        leaf = next(t for t in tree.traces.values() if t.state().task.depth == 2)
        self.assertEqual(leaf.state().task.status, TaskStatus.CANCELLED)
        self.assertTrue(tree.complete and tree.details_complete)
        self.assertTrue(any(e.type == "tool_finished" and e.payload["name"] == "ledger_read"
                            and e.payload["status"] == "succeeded" for e in leaf.events))
        self.assertEqual(tree.result(leaf.task_id)["status"], "cancelled")
        self.assertEqual(tree.result(result.task_id)["status"], "completed")

    async def held_recursive(self, *, failing="none"):
        leaf_gate, sibling_gate = self.gate(complete("leaf")), self.gate(complete("sibling"))
        failure_gate = self.gate(LLMResponse(None))
        middle = FakeProvider(calls(spawn("leaf")), failure_gate if failing == "middle" else wait_children,
                              read_children, complete("middle"))
        parent = FakeProvider(calls(spawn("A"), spawn("B")), failure_gate if failing == "root" else wait_children,
                              read_children, complete("root"))
        root = await self.start(parent, subagents={
            "A": profile(middle, allowed_subagent_config_ids=("leaf",)),
            "B": profile(FakeProvider(sibling_gate)), "leaf": profile(FakeProvider(leaf_gate)),
        }, limits=AgentLimits(max_steps=15, max_total_subagents=3, max_subagent_depth=2,
                              context_compact_threshold_chars=None))
        await asyncio.wait_for(asyncio.gather(leaf_gate.entered.wait(), sibling_gate.entered.wait()), 3)
        return root, leaf_gate, sibling_gate, failure_gate

    async def test_middle_failure_recursively_joins_leaf_and_preserves_other_branch(self):
        root, leaf_gate, sibling_gate, failure = await self.held_recursive(failing="middle")
        await failure.entered.wait()
        observer = TreeObserver(root)
        watcher = asyncio.create_task(observer.run())
        failure.release.set()
        leaf_entry = next(r for r in await root.subagents.descendants() if r["state"]["task"]["depth"] == 2)
        leaf = root.subagents.child(leaf_entry["task_id"])
        await event_of(leaf, "command_received", lambda e: e.payload["command_type"] == "cancel")
        self.assertFalse(root.done)
        leaf_gate.release.set()
        sibling_gate.release.set()
        result = await self.result(root)
        await watcher
        states = {t.state().task.instruction: t.state().task.status for t in result.tree.traces.values()}
        self.assertEqual(states["delegated A"], TaskStatus.FAILED)
        self.assertEqual(states["delegated leaf"], TaskStatus.CANCELLED)
        self.assertEqual(states["delegated B"], TaskStatus.COMPLETED)
        observer.compare(TaskTreeSnapshot.from_json(result.tree.to_json()))

    async def test_root_failure_and_cancellation_keep_complete_recursive_archives(self):
        for action in ("fail", "cancel"):
            with self.subTest(action=action):
                root, leaf_gate, sibling_gate, failure = await self.held_recursive(failing="root")
                await failure.entered.wait()
                observer = TreeObserver(root)
                watcher = asyncio.create_task(observer.run())
                if action == "cancel":
                    await root.cancel()
                failure.release.set()
                leaf_entry = next(r for r in await root.subagents.descendants() if r["state"]["task"]["depth"] == 2)
                leaf = root.subagents.child(leaf_entry["task_id"])
                await event_of(leaf, "command_received", lambda e: e.payload["command_type"] == "cancel")
                self.assertFalse(root.done)
                leaf_gate.release.set()
                sibling_gate.release.set()
                result = await self.result(root)
                await watcher
                self.assertEqual(result.status, TaskStatus.FAILED if action == "fail" else TaskStatus.CANCELLED)
                self.assertTrue(result.tree.complete and result.tree.details_complete)
                self.assertTrue(all(t.state().task.status == TaskStatus.CANCELLED
                                    for key, t in result.tree.traces.items() if key != root.task_id))
                observer.compare(TaskTreeSnapshot.from_json(result.tree.to_json()))

    async def test_late_tree_observer_snapshot_cursor_and_already_completed_child(self):
        root_gate, a_gate = self.gate(calls(spawn("A"))), self.gate(calls(spawn("leaf")))
        parent = FakeProvider(calls(spawn("B")), root_gate, wait_children, read_children, complete())
        root = await self.start(parent, subagents={
            "A": profile(FakeProvider(a_gate, wait_children, read_children, complete()),
                         allowed_subagent_config_ids=("leaf",)),
            "leaf": profile(FakeProvider(complete("leaf"))), "B": profile(FakeProvider(complete("B"))),
        }, limits=AgentLimits(max_total_subagents=3, max_subagent_depth=2, context_compact_threshold_chars=None))
        await root_gate.entered.wait()
        b = root.subagents.child((await root.subagents.children())[0]["task_id"])
        await self.result(b)
        snapshot = await root.subagents.snapshot()
        self.assertFalse(snapshot.complete)
        self.assertTrue(snapshot.traces[b.task_id].complete)
        observer = TreeObserver(root)
        watcher = asyncio.create_task(observer.run())
        await observer.ready.wait()
        root_gate.release.set()
        await a_gate.entered.wait()
        during_creation = await root.subagents.snapshot()
        self.assertEqual(len(during_creation.traces), 3)
        a_gate.release.set()
        result = await self.result(root)
        await watcher
        observer.compare(TaskTreeSnapshot.from_json(result.tree.to_json()))
        self.assertEqual(len(observer.sessions), 4)
        self.assertTrue(all(r["historical"] for r in observer.records if r["event"]["task_id"] == b.task_id))
        # Attaching after every task closed still recovers the entire history once.
        late = TreeObserver(root)
        await late.run()
        late.compare(result.tree)

    async def test_host_answer_wait_does_not_block_descendant_observation(self):
        child_gate = self.gate(complete("child done"))
        parent = FakeProvider(calls(spawn("child")), calls(("host_ask", {"question": "Proceed?"})),
                              wait_children, read_children, complete())
        root = await self.start(parent, subagents={"child": profile(FakeProvider(child_gate))},
            limits=AgentLimits(max_total_subagents=1, max_subagent_depth=1, host_response_timeout=None))
        queue, audit = asyncio.Queue(), []
        answer_gate, child_observed = asyncio.Event(), asyncio.Event()
        def display(record):
            if record["event"]["type"] == "finished" and record["event"]["task_id"] != root.task_id:
                child_observed.set()
        observer = TreeObserver(root, decisions=queue, display=display)
        watcher = asyncio.create_task(observer.run())
        controller = asyncio.create_task(host_decisions(root, queue, audit, answer_gate=answer_gate))
        try:
            await event_of(root, "question")
            await child_gate.entered.wait()
            child_gate.release.set()
            await asyncio.wait_for(child_observed.wait(), 3)
            self.assertFalse(root.done)
            self.assertEqual(audit, [])
            answer_gate.set()
            result = await self.result(root)
            await watcher
            await controller
            observer.compare(result.tree)
        finally:
            answer_gate.set()
            for task in (watcher, controller):
                if not task.done():
                    task.cancel()
            await asyncio.gather(watcher, controller, return_exceptions=True)

    async def test_closed_host_question_is_rejected_not_reported_answered(self):
        gate = self.gate(complete())
        root = await self.start(FakeProvider(calls(("host_ask", {"question": "Q?"})), gate))
        question = await event_of(root, "question")
        await root.send_instruction("Question no longer needed")
        await gate.entered.wait()
        receipt = await root.answer(question_id=question.payload["question_id"], text="Too late")
        rejection = await event_of(root, "command_rejected", lambda e: e.payload["message_id"] == receipt)
        self.assertEqual(rejection.payload["reason"], "no_matching_pending_question")
        gate.release.set()
        result = await self.result(root)
        self.assertFalse(any(e.type == "command_applied" and e.payload["message_id"] == receipt for e in result.tracer.events))
        with self.assertRaises(RuntimeError):
            await root.answer(question_id=question.payload["question_id"], text="After finish")

    async def test_cancel_pending_tool_request_closes_hops_and_rejects_late_host_decision(self):
        leaf_provider = FakeProvider(calls(REQUEST), wait_tool, complete())
        root = await self.start(TreeProvider("middle"),
            host_tools=registry_for(database_query=lambda: 73), subagents={
                "middle": profile(TreeProvider("leaf"), allowed_subagent_config_ids=("leaf",)),
                "leaf": profile(leaf_provider, allowed_tool_ids=("database_query",)),
            }, limits=AgentLimits(max_total_subagents=2, max_subagent_depth=2,
                                  context_compact_threshold_chars=None))
        approval = await event_of(root, "tool_host_approval_requested")
        leaf = root.subagents.child(approval.payload["requesting_task_id"])
        observer = TreeObserver(root)
        watcher = asyncio.create_task(observer.run())
        await root.cancel()
        result = await self.result(root)
        await watcher
        self.assertEqual((await leaf.tool_state())["requests"][0]["status"], "closed")
        with self.assertRaises(ToolIntegrationError):
            await leaf.grant_tool(request_id=approval.payload["request_id"], tool_id="database_query")
        with self.assertRaises(ToolIntegrationError):
            await leaf.deny_tool(request_id=approval.payload["request_id"], reason="Too late")
        self.assertFalse(any(r.status == "pending" for r in await leaf.requests()))
        self.assertTrue(any(e.type == "tool_access_closed" for e in result.tree.traces[leaf.task_id].events))
        observer.compare(TaskTreeSnapshot.from_json(result.tree.to_json()))

    async def test_no_subscriber_recursive_solve_and_replay_without_runtime(self):
        parent = FakeProvider(calls(spawn("A")), wait_children, read_children, complete("reviewed"))
        middle = FakeProvider(calls(spawn("leaf")), wait_children, read_children, complete("A"))
        result = await ProblemSolver(parent).solve("Root", model="fake", subagents={
            "A": profile(middle, allowed_subagent_config_ids=("leaf",)),
            "leaf": profile(FakeProvider(complete("leaf"))),
        }, limits=AgentLimits(max_total_subagents=2, max_subagent_depth=2))
        tree = TaskTreeSnapshot.from_json(result.tree.to_json())
        self.assertTrue(tree.complete and tree.details_complete)
        self.assertEqual(len(tree.traces), 3)
        self.assertIn("reviewed", replay(tree))
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "tree.json"
            archive.write_text(tree.to_json(), encoding="utf-8")
            # A fresh isolated process has no live Provider/Registry/Session objects.
            script = ("import sys; sys.path[:0] = " + repr(sys.path) + "; "
                      "from pathlib import Path; from shs_agent import TaskTreeSnapshot; "
                      "from examples.replay_task_tree import replay; "
                      f"t=TaskTreeSnapshot.from_json(Path({str(archive)!r}).read_text(encoding='utf-8')); "
                      "assert t.complete; assert 'reviewed' in replay(t); "
                      "assert not any(n in sys.modules for n in ('ollama','openai','sqlalchemy','torch'))")
            subprocess.run([sys.executable, "-I", "-S", "-c", script], check=True, timeout=15)

    async def test_single_task_start_solve_and_optional_features_remain_optional(self):
        for method in ("solve", "start"):
            solver = ProblemSolver(FakeProvider(complete("small")))
            result = await getattr(solver, method)("small", model="fake")
            if method == "start":
                self.assertIsNone(result.subagents)
                observer = TreeObserver(result)
                watcher = asyncio.create_task(observer.run())
                self.assertEqual((await result.tool_state())["requests"], [])
                result = await result.result()
                await watcher
                self.assertEqual(len(observer.sessions), 1)
            self.assertEqual(result.status, TaskStatus.COMPLETED)
            self.assertIsNone(result.plan)
            self.assertIsNone(result.tree)
            self.assertTrue(result.tracer.complete)
