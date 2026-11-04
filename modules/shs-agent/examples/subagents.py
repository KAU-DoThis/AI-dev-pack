"""Offline, deterministic root + two children demonstration (no model server).

PYTHONPATH=src python examples/subagents.py
Writes detached traces and the actual offered schemas under build/subagents_demo/.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from shs_agent import AgentLimits, ProblemSolver, SubagentConfig, TaskTreeSnapshot
from shs_agent.llm import LLMResponse, ToolCall
from shs_agent.tool import Tool, ToolExecutor, ToolRegistry, ToolSpec


def call(*items):
    return LLMResponse(None, [ToolCall(name, args) for name, args in items])


def results(request, name):
    return [json.loads(message["content"])["output"] for message in request["messages"]
            if message["role"] == "tool" and message.get("name") == name]


class DemoProvider:
    """Scripted tool choices, not a real model or a new runtime loop."""

    def __init__(self):
        self.root_schemas = None

    async def chat(self, **request):
        instruction = request["messages"][1]["content"]
        if instruction != "root":
            if not results(request, "memo_write"):
                return call(("plan_create", {"items": [{"item_id": "local", "description": instruction}],
                                              "reason": "record delegated work"}),
                            ("memo_write", {"content": instruction + " evidence", "kind": "finding"}),
                            ("child_work", {"label": instruction}))
            return call(("task_complete", {"output": instruction + " result"}))
        self.root_schemas = request["tools"]
        if not results(request, "plan_create"):
            return call(("plan_create", {"items": [{"item_id": "review", "description": "review both results"}],
                                          "reason": "delegate independent work"}))
        if not results(request, "subagent_spawn"):
            return call(*(("subagent_spawn", {"instruction": label, "context": ["explicit delegated background"],
                                              "config_id": "worker"}) for label in ("A", "B")))
        ids = [value["task_id"] for value in results(request, "subagent_spawn")]
        if not results(request, "parent_work"):
            return call(("parent_work", {}), ("subagent_status", {}))
        if not results(request, "subagent_wait"):
            return call(("subagent_wait", {"task_ids": ids}))
        if not results(request, "subagent_result"):
            return call(*(("subagent_result", {"task_id": key}) for key in ids))
        if not results(request, "plan_update"):
            assert all(value["available"] for value in results(request, "subagent_result"))
            return call(("plan_update", {"updates": [{"item_id": "review", "status": "completed"}],
                                          "expected_revision": 1, "reason": "reviewed child results"}))
        return call(("task_complete", {"output": "A and B results reviewed"}))


async def main() -> None:
    provider = DemoProvider()
    entered = {label: asyncio.Event() for label in ("A", "B")}
    release = asyncio.Event()
    parent_worked = asyncio.Event()

    async def child_work(label):
        entered[label].set()
        await release.wait()
        return label + " evidence collected"

    async def parent_work():
        await asyncio.gather(*(event.wait() for event in entered.values()))
        assert not release.is_set()
        parent_worked.set()
        return "parent worked while both children were running"

    child_registry, root_registry = ToolRegistry(), ToolRegistry()
    child_registry.register(Tool(ToolSpec("child_work", "Collect gated evidence", {
        "type": "object", "properties": {"label": {"type": "string", "enum": ["A", "B"]}},
        "required": ["label"], "additionalProperties": False,
    }), child_work))
    root_registry.register(Tool(ToolSpec("parent_work", "Parent's independent work", {
        "type": "object", "properties": {}, "additionalProperties": False,
    }), parent_work))
    # Everything is explicit here. A profile may instead omit provider/model/options/
    # limits to use the approved parent defaults; tools are always explicit.
    worker = SubagentConfig(provider=provider, model="fake-child", options=None,
        registry=child_registry, executor=ToolExecutor(child_registry),
        limits=AgentLimits(max_steps=8, max_tool_calls=12, context_compact_threshold_chars=None))
    root = await ProblemSolver().start("root", provider=provider, model="fake-root", options=None,
        registry=root_registry, executor=ToolExecutor(root_registry),
        limits=AgentLimits(max_steps=12, max_tool_calls=20, max_total_subagents=2,
                          max_subagent_depth=1, context_compact_threshold_chars=None),
        subagents={"worker": worker})
    try:
        await asyncio.wait_for(parent_worked.wait(), 5)
        children = await root.subagents.children()
        assert len(children) == 2 and all(not child["done"] for child in children)
        for child in children:
            session = root.subagents.child(child["task_id"])
            state = await session.state()
            detail = await session.artifact(state.agent.operation_artifact_id)
            print(state.task.task_id, state.agent.activity.value, detail.kind)
        assert not (await root.subagents.snapshot()).complete
    finally:
        release.set()
    result = await root.result()
    assert result.status.value == "completed", result
    tree = await root.subagents.collect()
    restored = TaskTreeSnapshot.from_json(tree.to_json())
    assert restored.complete and restored.details_complete
    assert restored.to_json() == result.tree.to_json()
    output = Path(__file__).resolve().parents[1] / "build" / "subagents_demo" / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
    output.mkdir(parents=True, exist_ok=False)
    (output / "tree.json").write_text(tree.to_json(indent=2), encoding="utf-8")
    schemas = [schema for schema in provider.root_schemas if schema["name"] != "parent_work"]
    (output / "internal_tools.schema.json").write_text(json.dumps(schemas, indent=2, ensure_ascii=False), encoding="utf-8")
    for task_id in restored.traces:
        saved = restored.result(task_id)
        print(task_id, saved["status"], saved["output"])
    print("Fake Provider; two gated child tools and parent work overlapped. No GPU inference measured.")
    print(output)


if __name__ == "__main__":
    asyncio.run(main())
