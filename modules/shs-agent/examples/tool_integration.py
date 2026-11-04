"""Offline Root -> A -> A-1 -> Host approval -> actual database tool demo.

python examples/tool_integration.py --schema-dir docs/reports/phase_7_tool_integration
Uses scripted Fake decisions and an in-memory Host handler; no server/database.
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


def call(name, **arguments):
    return LLMResponse(None, [ToolCall(name, arguments)])


def results(request, name):
    values = [json.loads(m["content"]) for m in request["messages"]
              if m["role"] == "tool" and m.get("name") == name]
    assert all(value["ok"] for value in values), values
    return [value["output"] for value in values]


class DemoProvider:
    def __init__(self):
        self.schemas = {}
        self.leaf_offers = []

    async def chat(self, **request):
        name = request["messages"][1]["content"]
        self.schemas.setdefault(name, copy.deepcopy(request["tools"]))
        state = next(json.loads(m["content"].split("\n", 1)[1]) for m in request["messages"]
                     if m.get("content", "").startswith("Current tool availability"))
        if name == "A-1":
            self.leaf_offers.append(copy.deepcopy(request["tools"]))
            if not results(request, "tool_request"):
                return call("tool_request", requested_tool="database_query", reason="need transaction history",
                            intended_use="read the last three months")
            own = state["requests"][0]
            if own["status"] != "applied":
                assert own["status"] in {"requested", "awaiting_host", "approved"}, own
                return call("tool_wait", request_id=own["request_id"])
            assert "database_query" in {t["name"] for t in request["tools"]}
            if not results(request, "database_query"):
                return call("database_query", months=3)
            output = json.dumps(results(request, "database_query")[0])
            if not results(request, "parent_notify"):
                return call("parent_notify", message=output)
            return call("task_complete", output=output)
        children = results(request, "subagent_spawn")
        if not children:
            child = "A" if name == "Root" else "A-1"
            return call("subagent_spawn", instruction=child, context=[], config_id=child)
        received = [json.loads(m["content"].split("\n", 1)[1]) for m in request["messages"]
                    if m["role"] == "user" and m.get("content", "").startswith("Agent communication")]
        for message in received:
            if message["kind"] != "question":
                continue
            known = next((r for r in state["requests"] if any(h["request_id"] == message["request_id"] for h in r["hops"])), None)
            if known and known["status"] == "requested" and known["hops"][-1]["request_id"] == message["request_id"]:
                return call("tool_forward", request_id=message["request_id"])
        outputs = results(request, "subagent_result")
        if outputs and outputs[-1]["available"]:
            return call("task_complete", output=outputs[-1]["output"])
        waits = results(request, "subagent_wait")
        if waits and waits[-1]["reason"] == "terminated":
            return call("subagent_result", task_id=children[0]["task_id"])
        return call("subagent_wait", task_ids=[children[0]["task_id"]])


async def demo():
    executions = []
    def database_query(months):
        executions.append(months)
        return {"months": months, "transactions": 73}
    catalog = ToolRegistry()
    catalog.register(Tool(ToolSpec("database_query", "Read the Host's demo transactions", {
        "type": "object", "properties": {"months": {"type": "integer", "minimum": 1}},
        "required": ["months"], "additionalProperties": False}), database_query))
    empty = ToolRegistry()
    provider = DemoProvider()
    root = await ProblemSolver(provider).start("Root", model="scripted-fake", host_tools=catalog,
        limits=AgentLimits(max_total_subagents=3, max_subagent_depth=2, context_compact_threshold_chars=None),
        subagents={
            "A": SubagentConfig(registry=empty, executor=ToolExecutor(empty), allowed_subagent_config_ids=("A-1",)),
            "A-1": SubagentConfig(registry=empty, executor=ToolExecutor(empty), allowed_tool_ids=("database_query",)),
        })
    queue = asyncio.Queue()
    watchers = []

    async def observe(session):
        async for event in session.events():
            # Observation never waits for a person/approval. A GUI or server can
            # consume the same queue and submit commands from a separate callback.
            queue.put_nowait((session, event))
            if event.type == "subagent_created":
                child = root.subagents.child(event.payload["task_id"])
                watchers.append(asyncio.create_task(observe(child)))

    async def host_decisions():
        while True:
            value = await queue.get()
            if value is None:
                return
            session, event = value
            if event.type == "tool_host_approval_requested":
                target = root.subagents.child(event.payload["requesting_task_id"])
                state = await target.state()
                # Explicit, fixed approval in this Fake Host example only.
                assert state.task.depth == 2 and event.payload["requested_tool"] == "database_query"
                before = await target.tool_state()
                assert "database_query" not in before["tools"]
                receipt = await target.grant_tool(request_id=event.payload["request_id"], tool_id="database_query")
                assert receipt["status"] == "approved"
                assert "database_query" not in (await target.tool_state())["tools"]

    watchers.append(asyncio.create_task(observe(root)))
    decisions = asyncio.create_task(host_decisions())
    try:
        result = await root.result()
        await asyncio.gather(*watchers)
        queue.put_nowait(None)
        await decisions
    finally:
        if not root.done:
            await root.cancel()
            await root.result()
        for worker in [*watchers, decisions]:
            if not worker.done():
                worker.cancel()
        await asyncio.gather(*watchers, decisions, return_exceptions=True)
    assert result.status == TaskStatus.COMPLETED, result
    assert executions == [3], executions
    tree = TaskTreeSnapshot.from_json(result.tree.to_json())
    assert tree.complete and tree.details_complete
    assert all(t.state().task.status == TaskStatus.COMPLETED for t in tree.traces.values())
    assert "database_query" not in {t["name"] for t in provider.leaf_offers[0]}
    assert "database_query" in {t["name"] for t in provider.leaf_offers[-1]}
    return result, provider


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema-dir", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("build/tool_integration_demo"))
    args = parser.parse_args()
    result, provider = asyncio.run(demo())
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "task_tree.json").write_text(result.tree.to_json(indent=2), encoding="utf-8")
    if args.schema_dir:
        args.schema_dir.mkdir(parents=True, exist_ok=True)
        for name, filename in (("Root", "root"), ("A", "depth_1"), ("A-1", "depth_2")):
            (args.schema_dir / f"{filename}_internal_tools.schema.json").write_text(
                json.dumps(provider.schemas[name], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        added = [t for t in provider.schemas["Root"] if t["name"] in {"tool_request", "tool_forward", "tool_wait", "tool_status"}]
        (args.schema_dir / "tool_integration_tools.schema.json").write_text(
            json.dumps(added, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (args.schema_dir / "depth_2_after_grant.schema.json").write_text(
            json.dumps(provider.leaf_offers[-1], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"completed: {result.output}; tasks={len(result.tree.traces)}; real handler executions=1")
    print(args.output_dir / "task_tree.json")


if __name__ == "__main__":
    main()
