"""Offline terminal Host: python examples/full_workflow_host.py [--review-memory].

The observer and decision coroutine are independent. No stdin, server, custom
Runtime, scheduler, or automatic Memory storage is involved.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import json
from pathlib import Path

from shs_agent import ProblemSolver, SessionState, TaskStatus, TaskTreeSnapshot

if __package__:
    from .workflow_support import WorkflowProvider, configuration, ROOT_TASK, OperationGate, NARROW_INSTRUCTION
else:
    from workflow_support import WorkflowProvider, configuration, ROOT_TASK, OperationGate, NARROW_INSTRUCTION


class TreeObserver:
    """Host-side subscription management using only existing public APIs.

    Each task starts with a detached trace prefix and continues after its cursor.
    Historical creation events discover already finished children too. TaskGroup
    joins dynamically registered watchers, including grandchildren created while
    another watcher is ending. Receipt order is NOT a causal/global event order.
    """

    def __init__(self, root, *, decisions=None, display=None):
        self.root, self.decisions, self.display = root, decisions, display
        self.records = []
        self.artifacts = {}
        self.snapshots = {}
        self.sessions = {}
        self.ready = asyncio.Event()
        self._group = None

    def target(self, task_id):
        return self.root if task_id == self.root.task_id else self.root.subagents.child(task_id)

    def _register(self, session, trace=None):
        if session.task_id not in self.sessions:
            self.sessions[session.task_id] = session
            self._group.create_task(self._watch(session, trace))

    async def run(self):
        try:
            if self.root.subagents is None:
                traces = {self.root.task_id: await self.root.trace()}
            else:
                traces = (await self.root.subagents.snapshot()).traces
            async with asyncio.TaskGroup() as group:
                self._group = group
                for task_id, trace in traces.items():
                    self._register(self.target(task_id), trace)
                self.ready.set()
        finally:
            if self.decisions is not None:
                self.decisions.put_nowait(None)

    async def _watch(self, session, trace):
        trace = trace if trace is not None else await session.trace()
        snapshot = trace.state()
        self.snapshots[session.task_id] = snapshot
        state = None
        for event in trace.events:
            state = await self._record(session, event, state, historical=True)
        async for event in session.events(after_sequence=snapshot.last_event_sequence):
            state = await self._record(session, event, state, historical=False)

    async def _record(self, session, event, previous, *, historical):
        # State is reconstructed at the event's cursor. A new state() call here
        # could already be ahead of the event, especially during prefix replay.
        if event.type in {"created", "state_changed", "status_changed", "finished",
                          "plan_created", "plan_updated", "plan_revised"}:
            state = SessionState.from_dict({**event.payload["state"], "last_event_sequence": event.sequence})
        else:
            state = replace(previous, last_event_sequence=event.sequence)
        refs = [(session.task_id, key) for key in event.artifact_ids]
        if state.task.plan_artifact_id:
            refs.append((session.task_id, state.task.plan_artifact_id))
        while refs:
            key = refs.pop()
            task_id, artifact_id = key
            if key not in self.artifacts:
                detail = await self.target(task_id).artifact(artifact_id)
                self.artifacts[key] = detail
                refs.extend((task_id, ref) for ref in detail.references)
                if isinstance(detail.data, dict):
                    refs.extend((ref["task_id"], ref["artifact_id"]) for ref in detail.data.get("task_refs", []))
        record = {"receipt_index": len(self.records) + 1, "historical": historical,
                  "event": json.loads(json.dumps(event.to_dict())), "state": state.to_dict()}
        if event.type == "agent_message_received":
            record["messages"] = [a.data for a in self.artifacts.values() if a.kind == "agent_message_requested"
                                  and a.data.get("message_id") == event.payload.get("message_id")]
        if event.type in {"plan_created", "plan_updated", "plan_revised"}:
            record["plan"] = self.artifacts[(session.task_id, state.task.plan_artifact_id)].data
        self.records.append(record)
        if event.type == "subagent_created":
            self._register(self.target(event.payload["task_id"]))
        if self.decisions is not None:
            self.decisions.put_nowait((session, event))
        if self.display is not None:
            self.display(record)
        return state

    def compare(self, tree):
        """Validate event identity, state at every cursor and captured details."""
        if set(self.sessions) != set(tree.traces):
            raise AssertionError("live and final task sets differ")
        for task_id, trace in tree.traces.items():
            records = [r for r in self.records if r["event"]["task_id"] == task_id]
            if [r["event"] for r in records] != json.loads(json.dumps([e.to_dict() for e in trace.events])):
                raise AssertionError(f"missing, duplicate or changed events: {task_id}")
            for record in records:
                if record["state"] != trace.state(at_sequence=record["event"]["sequence"]).to_dict():
                    raise AssertionError(f"state reconstruction mismatch: {task_id}")
            snapshot = self.snapshots[task_id]
            if snapshot != trace.state(at_sequence=snapshot.last_event_sequence):
                raise AssertionError(f"snapshot cursor mismatch: {task_id}")
        for (task_id, artifact_id), artifact in self.artifacts.items():
            if artifact != tree.artifact({"task_id": task_id, "artifact_id": artifact_id}):
                raise AssertionError(f"artifact mismatch: {artifact_id}")
        tree.validate()


def terminal_display(record):
    event, state = record["event"], record["state"]
    if event["type"] in {"created", "finished", "question", "question_closed", "command_received", "command_applied",
            "command_rejected", "plan_created", "plan_updated", "plan_revised", "llm_call_started",
            "llm_call_finished", "tool_started", "tool_finished", "subagent_created", "subagent_terminated",
            "subagent_result_delivered", "agent_message_received", "tool_host_approval_requested",
            "tool_host_approved", "tool_host_denied", "tool_access_applied", "tool_access_apply_failed"}:
        task, agent = state["task"], state["agent"]
        print(f"receipt={record['receipt_index']} task={task['task_id']} parent={task['parent_task_id']} "
              f"root={task['root_task_id']} depth={task['depth']} seq={event['sequence']} "
              f"{task['status']}/{agent['activity']} model={agent['model']} "
              f"plan={task['plan_revision']} operation={agent['operation_id']} "
              f"{event['type']} refs={event['artifact_ids']}")
        if event["type"] in {"question", "question_closed", "agent_message_received", "command_received"}:
            print("  ", json.dumps(event["payload"], ensure_ascii=False))
        if "messages" in record:
            print("  messages:", json.dumps(record["messages"], ensure_ascii=False))
        if "plan" in record:
            print("  plan:", json.dumps(record["plan"], ensure_ascii=False))


async def host_decisions(root, queue, audit, *, answer_gate=None):
    """Fixed policy for this demo. A real UI can submit commands independently.

    Receipts only confirm enqueueing. command_applied/rejected and question
    closure events in the journal establish whether an answer actually applied.
    """
    while (item := await queue.get()) is not None:
        session, event = item
        if event.type == "question":
            if answer_gate is not None:
                await answer_gate.wait()  # Does not block the independent observer.
            state = await session.state()
            if not session.done and state.agent.question_id == event.payload["question_id"]:
                receipt = await session.answer(question_id=event.payload["question_id"], text="Use KRW.")
                audit.append({"action": "answer_queued", "task_id": session.task_id,
                              "question_id": event.payload["question_id"], "receipt": receipt})
        elif event.type == "tool_host_approval_requested":
            task_id = event.payload["requesting_task_id"]
            target = root if task_id == root.task_id else root.subagents.child(task_id)
            request_id, name = event.payload["request_id"], event.payload["requested_tool"]
            state = await target.tool_state()
            current = next(r for r in state["requests"] if r["request_id"] == request_id)
            if target.done or current["status"] != "awaiting_host":
                continue
            if name == "ledger_read":
                receipt = await target.grant_tool(request_id=request_id, tool_id=name)
                assert receipt["status"] == "approved"
                assert name not in (await target.tool_state())["tools"]
            else:
                receipt = await target.deny_tool(request_id=request_id, reason="Use anonymous ledger only")
            audit.append({"action": receipt["status"], "task_id": task_id, "request_id": request_id})


def evaluate(result, *, narrowed=False):
    """Scenario-specific Host acceptance. Never change TaskStatus or trace."""
    expected = {"count": 3, "total": 2500, "ids": ["T1", "T2", "T4"], "currency": "KRW"}
    if narrowed:
        expected = {"count": 1, "total": 1200, "ids": ["T1"], "currency": "KRW"}
    try:
        answer = json.loads(result.output)
    except (ValueError, TypeError):
        answer = None
    traces = result.tree.traces.values() if result.tree else (result.tracer,)
    successful_reads = [e for trace in traces for e in trace.events
                        if e.type == "tool_finished" and e.payload["name"] == "ledger_read"
                        and e.payload["executed"] and e.payload["status"] == "succeeded"]
    checks = {"output_matches_fixture": answer == expected, "ledger_handler_returned": bool(successful_reads),
              "engine_completed": result.status == TaskStatus.COMPLETED}
    return {"accepted": all(checks.values()), "checks": checks}


def memory_candidates(result, review):
    """Explicit opt-in, Host-reviewed evidence candidates; never persist Memory."""
    if not review["accepted"]:
        return []
    candidates = []
    for task_id, trace in result.tree.traces.items():
        for event in trace.events:
            if (event.type == "tool_finished" and event.payload["name"] == "ledger_read"
                    and event.payload["status"] == "succeeded"):
                candidates.append({"content": json.loads(result.output), "status": "candidate_for_host_review",
                    "source": {"task_id": task_id, "artifact_id": event.payload["result_ref"]["artifact_id"]},
                    "output_task_id": result.task_id})
    return candidates


async def control_leaf(root, gate, action, audit):
    await gate.entered.wait()
    try:
        entry = next(item for item in await root.subagents.descendants() if item["state"]["task"]["depth"] == 2)
        target = root.subagents.child(entry["task_id"])
        before = await target.state()
        receipt = (await target.send_instruction(NARROW_INSTRUCTION) if action == "instruction"
                   else await target.cancel())
        audit.append({"action": action + "_queued", "task_id": target.task_id,
                      "receipt": receipt, "before": before.to_dict()})
    finally:
        gate.release.set()


async def run_workflow(*, display=None, control=None):
    if control not in {None, "instruction", "cancel"}:
        raise ValueError("control must be instruction, cancel or None")
    executions, audit = [], []
    gate = OperationGate() if control else None
    root = await ProblemSolver(WorkflowProvider()).start(
        ROOT_TASK, model="scripted-fake", **configuration(executions, ledger_gate=gate))
    queue = asyncio.Queue()
    observer = TreeObserver(root, decisions=queue, display=display)
    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(observer.run())
            group.create_task(host_decisions(root, queue, audit))
            operator = group.create_task(control_leaf(root, gate, control, audit)) if gate else None
            result = await root.result()
            if operator is not None and not operator.done():
                # An early task failure may never reach the example's gate.
                operator.cancel()
    finally:
        # Host failure/interrupt aborts its own run cooperatively, never an engine timeout.
        if gate is not None:
            gate.release.set()
        if not root.done:
            await root.cancel()
            await root.result()
    restored = TaskTreeSnapshot.from_json(result.tree.to_json())
    observer.compare(restored)
    review = evaluate(result, narrowed=control == "instruction")
    return result, observer, {"engine_status": result.status.value, "stop_reason": result.stop_reason,
                             "business_review": review, "handler_executions": executions, "host_actions": audit,
                             "live_matches_archive": True}


def save_run(directory, result, observer, summary):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "task_tree.json").write_text(result.tree.to_json(indent=2), encoding="utf-8")
    (directory / "live.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n"
                                               for r in observer.records), encoding="utf-8")
    (directory / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("build/full_workflow_demo"))
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--review-memory", action="store_true", help="extract candidates only; no Memory storage")
    parser.add_argument("--control", choices=["instruction", "cancel"], help="control only the leaf during its handler")
    args = parser.parse_args()
    result, observer, summary = asyncio.run(run_workflow(
        display=None if args.quiet else terminal_display, control=args.control))
    if args.review_memory:
        summary["memory_candidates"] = memory_candidates(result, summary["business_review"])
    save_run(args.output_dir, result, observer, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("output:", result.output)
    print("archive:", args.output_dir / "task_tree.json")
    return 0 if summary["business_review"]["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
