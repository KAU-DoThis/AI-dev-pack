"""Scripted A -> parent -> B -> parent -> A demo; no model or server required.

PYTHONPATH=src python examples/communication.py
The gates prove B receives its question during an existing tool batch, then
answers at the next decision and continues its original task.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
from pathlib import Path

from shs_agent import AgentLimits, ProblemSolver, SubagentConfig, TaskStatus, TaskTreeSnapshot
from shs_agent.agent.tools.schemas import COMMUNICATION_SCHEMAS
from shs_agent.llm import LLMResponse, ToolCall
from shs_agent.tool import Tool, ToolExecutor, ToolRegistry, ToolSpec


def call(name, **arguments):
    return LLMResponse(None, [ToolCall(name, arguments)])


def results(request, name):
    values = [json.loads(message["content"]) for message in request["messages"]
              if message["role"] == "tool" and message.get("name") == name]
    assert all(value["ok"] for value in values), values
    return [value["output"] for value in values]


def messages(request, kind):
    values = [json.loads(message["content"].split("\n", 1)[1]) for message in request["messages"]
              if message["role"] == "user" and message.get("content", "").startswith("Agent communication")]
    return [value for value in values if value["kind"] == kind]


class DemoProvider:
    """Only scripted choices; production TaskRuntime still runs every task/tool."""

    def __init__(self, ask_gate):
        self.ask_gate = ask_gate
        self.schemas = {}

    async def chat(self, **request):
        task = request["messages"][1]["content"]
        self.schemas.setdefault(task, copy.deepcopy(request["tools"]))
        if task == "A":
            if not results(request, "parent_ask"):
                await self.ask_gate.wait()
                return call("parent_ask", message="What fields does the login API return?")
            if not results(request, "agent_wait_reply"):
                return call("agent_wait_reply", request_id=results(request, "parent_ask")[0]["request_id"])
            answer = results(request, "agent_wait_reply")[-1]
            assert answer["reason"] == "answered"
            return call("task_complete", output="A implemented login using " + answer["reply"]["content"])
        if task == "B":
            if not results(request, "read_api"):
                return call("read_api")
            if not results(request, "agent_reply"):
                return call("agent_reply", request_id=messages(request, "question")[0]["request_id"],
                            message=results(request, "read_api")[0])
            if not results(request, "continue_backend"):
                return call("continue_backend")
            return call("task_complete", output="B completed original backend work after answering")
        if not results(request, "subagent_spawn"):
            return LLMResponse(None, [ToolCall("subagent_spawn", {
                "instruction": name, "context": ["Login implementation"], "config_id": "worker"}) for name in ("A", "B")])
        ids = [value["task_id"] for value in results(request, "subagent_spawn")]
        if not messages(request, "question"):
            return call("subagent_wait", task_ids=ids)
        if not results(request, "subagent_ask"):
            return call("subagent_ask", task_id=ids[1], message="Please confirm the login response fields",
                        related_request_id=messages(request, "question")[0]["request_id"])
        if not results(request, "agent_wait_reply"):
            return call("agent_wait_reply", request_id=results(request, "subagent_ask")[0]["request_id"])
        if not results(request, "agent_reply"):
            answer = results(request, "agent_wait_reply")[-1]
            assert answer["reason"] == "answered"
            return call("agent_reply", request_id=messages(request, "question")[0]["request_id"],
                        message=answer["reply"]["content"])
        if results(request, "subagent_wait")[-1]["reason"] != "terminated":
            return call("subagent_wait", task_ids=ids)
        if not results(request, "subagent_result"):
            return LLMResponse(None, [ToolCall("subagent_result", {"task_id": key}) for key in ids])
        assert all(value["available"] for value in results(request, "subagent_result"))
        return call("task_complete", output="Reviewed A and B: login uses access_token and user")


async def event_of(session, kind, predicate=lambda e: True):
    async for event in session.events():
        if event.type == kind and predicate(event):
            return event
    raise AssertionError(f"{session.task_id} ended without {kind}")


async def demo():
    ask_gate, work_entered, work_release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    continued, finish_backend = asyncio.Event(), asyncio.Event()
    async def read_api():
        work_entered.set()
        await work_release.wait()
        return "access_token and user"
    async def continue_backend():
        continued.set()
        await finish_backend.wait()
        return "original backend work finished"
    registry = ToolRegistry()
    for name, handler in (("read_api", read_api), ("continue_backend", continue_backend)):
        registry.register(Tool(ToolSpec(name, name, {
            "type": "object", "properties": {}, "additionalProperties": False}), handler))
    provider = DemoProvider(ask_gate)
    session = await ProblemSolver().start("Root", model="scripted", provider=provider,
        limits=AgentLimits(max_total_subagents=2, max_subagent_depth=1, context_compact_threshold_chars=None),
        subagents={"worker": SubagentConfig(registry=registry, executor=ToolExecutor(registry))})
    observed = {}
    try:
        # Watchdog is test harness protection, never a runtime communication timeout.
        async with asyncio.timeout(10):
            await event_of(session, "subagent_wait_started")
            await work_entered.wait()
            children = [session.subagents.child(value["task_id"]) for value in await session.subagents.children()]
            ask_gate.set()
            await event_of(children[1], "agent_message_received", lambda e: e.payload["kind"] == "question")
            observed["B_when_question_queued"] = (await children[1].state()).to_dict()
            assert observed["B_when_question_queued"]["agent"]["activity"] == "tool_execution"
            assert not children[1].done
            work_release.set()
            await continued.wait()
            await event_of(children[0], "agent_message_received", lambda e: e.payload["kind"] == "reply")
            observed["B_after_answer"] = (await children[1].state()).to_dict()
            assert not children[1].done
            finish_backend.set()
            result = await session.result()
        assert result.status == TaskStatus.COMPLETED, result.warnings
        assert result.tool_errors == 0
        restored = TaskTreeSnapshot.from_json(result.tree.to_json())
        assert restored.to_dict() == result.tree.to_dict()
        assert restored.complete and restored.details_complete
        return result, provider.schemas, observed
    finally:
        ask_gate.set()
        work_release.set()
        finish_backend.set()
        if not session.done:
            await session.cancel()
            await session.result()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("build/communication_demo"))
    parser.add_argument("--schema-dir", type=Path)
    args = parser.parse_args()
    result, schemas, observed = await demo()
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "task_tree.json").write_text(result.tree.to_json(indent=2), encoding="utf-8")
    summary = {"provider": "Fake/scripted (no real model)", "status": result.status.value,
               "output": result.output, "task_id": result.task_id, "observations": observed,
               "tasks": {key: {"steps": trace.state().agent.steps, "tool_calls": trace.state().agent.tool_calls,
                               "tool_errors": trace.state().agent.tool_errors} for key, trace in result.tree.traces.items()}}
    (args.output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    directory = args.schema_dir or args.output
    directory.mkdir(parents=True, exist_ok=True)
    external = {"read_api", "continue_backend"}
    root = schemas["Root"]
    child = [schema for schema in schemas["A"] if schema["name"] not in external]
    names = {schema["name"] for schema in COMMUNICATION_SCHEMAS}
    communication = {schema["name"]: schema for schema in (*root, *child) if schema["name"] in names}
    for name, data in (("root_internal_tools.schema.json", root), ("child_internal_tools.schema.json", child),
                       ("communication_tools.schema.json", list(communication.values()))):
        (directory / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result.status.value, "output": result.output,
                      "output_directory": str(args.output), "schema_directory": str(directory)}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
