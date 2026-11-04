"""Read a saved TaskTreeSnapshot. No Provider, Session or tool execution.

python examples/replay_task_tree.py build/full_workflow_demo/task_tree.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from shs_agent import TaskTreeSnapshot


def replay(tree, *, details=False):
    lines = [f"root={tree.root_task_id} complete={tree.complete} details_complete={tree.details_complete}",
             "Sequences below are per task; task grouping is not a global causal order."]
    for task_id, trace in tree.traces.items():
        state = trace.state()
        task = state.task
        lines.append(f"{'  ' * task.depth}{task.instruction}: {task_id} parent={task.parent_task_id} "
                     f"root={task.root_task_id} depth={task.depth} model={state.agent.model} "
                     f"{task.status.value}/{task.stop_reason}")
        lines.append("  result=" + json.dumps(tree.result(task_id), ensure_ascii=False))
        for event in trace.events:
            if details or event.type.startswith(("plan_", "agent_", "subagent_", "tool_access_", "tool_host_")) or event.type in {
                    "question", "question_closed", "host_answer_applied", "command_received", "command_applied", "command_rejected",
                    "llm_call_started", "llm_call_finished", "tool_started", "tool_finished", "finished"}:
                lines.append(f"  seq={event.sequence} {event.type} refs={list(event.artifact_ids)} "
                             + json.dumps(event.payload, ensure_ascii=False))
                for artifact_id in event.artifact_ids:
                    artifact = trace.artifact(artifact_id)
                    if details or artifact.kind.startswith(("plan", "agent_", "tool_host_")):
                        lines.append("    artifact=" + json.dumps(artifact.to_dict(), ensure_ascii=False))
        if details:
            # Some raw results are reached through references, not direct event IDs.
            lines.append("  all_artifacts=" + json.dumps(
                {key: value.to_dict() for key, value in trace.artifacts.items()}, ensure_ascii=False))
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--details", action="store_true")
    args = parser.parse_args()
    print(replay(TaskTreeSnapshot.from_json(args.archive.read_text(encoding="utf-8")), details=args.details))


if __name__ == "__main__":
    main()
