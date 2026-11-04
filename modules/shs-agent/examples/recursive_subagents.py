"""Offline depth-3 demonstration and actual Provider tool-schema export.

PYTHONPATH=src python examples/recursive_subagents.py --schema-dir docs/reports/phase_6_recursive_subagents
Scripted decisions exercise the production Solver/Runtime; no inference server.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
from pathlib import Path

from shs_agent import AgentLimits, ProblemSolver, SubagentConfig, TaskStatus, TaskTreeSnapshot
from shs_agent.llm import LLMResponse, ToolCall
from shs_agent.tool import Tool, ToolExecutor, ToolRegistry, ToolSpec


BRANCHES = {"Root": ("A", "B"), "A": ("A-1", "A-2"), "B": ("B-1",), "A-1": ("A-1-1",)}
LEAVES = ("A-2", "B-1", "A-1-1")


def call(name, **arguments):
    return LLMResponse(None, [ToolCall(name, arguments)])


def results(request, name):
    values = [json.loads(m["content"]) for m in request["messages"]
              if m["role"] == "tool" and m.get("name") == name]
    assert all(v["ok"] for v in values), values
    return [v["output"] for v in values]


class DemoProvider:
    def __init__(self):
        self.schemas = {}

    async def chat(self, **request):
        name = request["messages"][1]["content"]
        self.schemas.setdefault(name, copy.deepcopy(request["tools"]))
        if not results(request, "plan_create"):
            return call("plan_create", items=[{"item_id": "work", "description": name}], reason="local plan")
        if not results(request, "memo_write"):
            return call("memo_write", content=name + " local memo", kind="fact")
        if name in BRANCHES:
            children = results(request, "subagent_spawn")
            if not children:
                return LLMResponse(None, [ToolCall("subagent_spawn", {
                    "instruction": child, "context": [], "config_id": child}) for child in BRANCHES[name]])
            ids = [child["task_id"] for child in children]
            if not results(request, "subagent_status"):
                return call("subagent_status")
            waits = results(request, "subagent_wait")
            if not waits or waits[-1]["reason"] != "terminated":
                return call("subagent_wait", task_ids=ids)
            outputs = results(request, "subagent_result")
            if not outputs:
                return LLMResponse(None, [ToolCall("subagent_result", {"task_id": key}) for key in ids])
            assert all(value["available"] for value in outputs)
            return call("task_complete", output=name + "(" + ",".join(value["output"] for value in outputs) + ")")
        if not results(request, "leaf_work"):
            return call("leaf_work")
        return call("task_complete", output=name)


async def demo():
    entered = {name: asyncio.Event() for name in LEAVES}
    release = asyncio.Event()
    provider = DemoProvider()
    configs = {}
    for name in ("A", "B", "A-1", *LEAVES):
        registry = ToolRegistry()
        if name in LEAVES:
            async def work(name=name):
                entered[name].set()
                await release.wait()
                return name + " evidence"
            registry.register(Tool(ToolSpec("leaf_work", "Local leaf evidence", {
                "type": "object", "properties": {}, "additionalProperties": False}), work))
        # A-1-1 is authorized to delegate, but depth=3 hides its spawn tools.
        allowed = ("A-1-1",) if name == "A-1-1" else BRANCHES.get(name, ())
        configs[name] = SubagentConfig(registry=registry, executor=ToolExecutor(registry),
                                       allowed_subagent_config_ids=allowed)
    root = await ProblemSolver(provider).start("Root", model="scripted-fake",
        limits=AgentLimits(max_total_subagents=6, max_subagent_depth=3,
                           context_compact_threshold_chars=None), subagents=configs)
    try:
        # Example watchdog only; the product's wait/cancel policies have no new deadline.
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered.values())), 10)
        manager = root.subagents
        live = await manager.snapshot()
        states = await manager.descendants()
        assert manager.total_created == 6 and len(live.traces) == 7 and not live.complete
        for item in states:
            child = manager.child(item["task_id"])
            state = await child.state()
            assert state.to_dict() == item["state"]
            if state.agent.operation_artifact_id:
                await child.artifact(state.agent.operation_artifact_id)
        release.set()
        result = await asyncio.wait_for(root.result(), 10)
        assert result.status == TaskStatus.COMPLETED, result.warnings
        assert result.output == "Root(A(A-1(A-1-1),A-2),B(B-1))"
        tree = TaskTreeSnapshot.from_json(result.tree.to_json())
        assert tree.complete and tree.details_complete
        assert tree.to_json() == (await manager.collect()).to_json()
        for trace in tree.traces.values():
            assert tree.result(trace.task_id)["tool_errors"] == 0
            for artifact in trace.artifacts.values():
                if artifact.kind.startswith(("subagent_", "agent_")):
                    for ref in artifact.data.get("task_refs", []):
                        tree.artifact(ref)
        return result, live, states, provider.schemas
    finally:
        release.set()
        if not root.done:
            await root.cancel()
        await root.result()


async def export(output_dir: Path, schema_dir: Path):
    result, live, states, schemas = await demo()
    output_dir.mkdir(parents=True, exist_ok=True)
    schema_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "task_tree.json").write_text(result.tree.to_json(indent=2), encoding="utf-8")
    (output_dir / "live_tree.json").write_text(live.to_json(indent=2), encoding="utf-8")
    sources = {"root_internal_tools": "Root", "depth_1_internal_tools": "A",
               "depth_2_internal_tools": "A-1", "depth_3_internal_tools": "A-1-1",
               "nondelegating_internal_tools": "A-2"}
    counts = {}
    for filename, name in sources.items():
        internal = [tool for tool in schemas[name] if tool["name"] != "leaf_work"]
        (schema_dir / (filename + ".schema.json")).write_text(
            json.dumps(internal, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        counts[filename] = len(internal)
    summary = {"provider": "scripted Fake; no real model", "root_task_id": result.task_id,
               "status": result.status.value, "output": result.output, "total_created": 6,
               "live_complete": live.complete, "complete": result.tree.complete,
               "details_complete": result.tree.details_complete, "schema_counts": counts,
               "live_descendants": states,
               "tasks": [{"task_id": key, "parent_task_id": trace.state().task.parent_task_id,
                          "root_task_id": trace.state().task.root_task_id, "depth": trace.state().task.depth,
                          "status": trace.state().task.status.value,
                          "events": len(trace.events), "artifacts": len(trace.artifacts),
                          "tool_errors": result.tree.result(key)["tool_errors"]}
                         for key, trace in result.tree.traces.items()]}
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k not in {"live_descendants", "tasks"}}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("build/recursive_subagents_demo"))
    parser.add_argument("--schema-dir", type=Path, default=Path("build/recursive_subagents_demo/schemas"))
    args = parser.parse_args()
    asyncio.run(export(args.output_dir, args.schema_dir))
