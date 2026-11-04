"""Opt-in local integration: python tests/verify_ollama_tracer.py.

Requires the existing Ollama SDK/server and both preinstalled models. Does not
pull models or run as part of offline unittest discovery. Writes reviewable
snapshots under build/ollama_state_tracer/ by default.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from time import perf_counter

from shs_agent import AgentLimits, ProblemSolver, TaskStatus, TracerSnapshot
from shs_agent.llm import ChatOptions
from shs_agent.llm.provider.ollama import OllamaLLM
from shs_agent.tool import Tool, ToolRegistry, ToolSpec


async def verify(model: str, output_dir: Path) -> dict:
    provider = OllamaLLM()
    executed = []

    def add_numbers(a: int, b: int) -> dict:
        executed.append((a, b))
        return {"sum": a + b, "evidence": "local-tool-result"}

    registry = ToolRegistry()
    registry.register(Tool(ToolSpec("add_numbers", "Add two integer values.", {
        "type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        "required": ["a", "b"],
    }), add_numbers))
    started = perf_counter()
    session = await ProblemSolver(provider).start(
        "Call add_numbers with a=19 and b=23. After reading the tool result, call "
        "task_complete alone with output exactly '42'. Use no other tools.",
        model=model, registry=registry, limits=AgentLimits(max_steps=5, max_tool_calls=5),
        options=ChatOptions(temperature=0, max_tokens=2048, extra={"think": True, "keep_alive": 0}),
    )
    observed_states, live = [], []
    async for event in session.events():
        live.append(event)
        for artifact_id in event.artifact_ids:
            assert (await session.artifact(artifact_id)).task_id == session.task_id
        if event.type in {"llm_call_started", "tool_started"}:
            state = await session.state()
            observed_states.append(state.to_dict())
        if event.type in {"llm_call_started", "llm_call_finished", "tool_finished", "finished"}:
            print(json.dumps({"model": model, "event": event.type,
                              "sequence": event.sequence, "status": event.payload.get("status")}), flush=True)
    result = await session.result()
    snapshot = result.tracer
    restored = TracerSnapshot.from_json(snapshot.to_json())
    path = output_dir / (model.replace(":", "_").replace(".", "_") + ".json")
    path.write_text(snapshot.to_json(indent=2), encoding="utf-8")
    assert tuple(live) == snapshot.events
    assert restored.to_json() == snapshot.to_json()
    assert restored.state() == await session.state()
    assert snapshot.complete
    assert snapshot.events[-1].type == "finished"
    assert snapshot.details_complete, [a.issues for a in snapshot.artifacts.values() if not a.details_complete]
    assert result.status == TaskStatus.COMPLETED, (result.status, result.stop_reason, result.warnings)
    assert result.output == "42", result.output
    assert executed == [(19, 23)], executed
    llm = [event.payload for event in snapshot.events if event.type == "llm_call_finished"]
    assert all(set(call["artifacts"]) == {"common_request", "adapter_request", "adapter_response",
                                          "common_response", "normalized_response"} for call in llm)
    assert all(call["response_received"] and call["validation_succeeded"] for call in llm)
    disclosed = [a.data["fields"].get("thinking") for a in snapshot.artifacts.values()
                 if a.kind == "llm_common_response"]
    return {"model": model, "status": result.status.value, "output": result.output,
            "steps": result.steps, "tool_calls": result.tool_calls,
            "events": len(snapshot.events), "artifacts": len(snapshot.artifacts),
            "details_complete": snapshot.details_complete, "elapsed_seconds": round(perf_counter() - started, 3),
            "disclosed_thinking_chars": [len(value or "") for value in disclosed],
            "usage": [call["usage"] for call in llm], "observed_states": observed_states,
            "trace_file": str(path.resolve())}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path("build/ollama_state_tracer"))
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    summaries = []
    for model in ("gemma4:e4b", "qwen3.5:9b"):
        summaries.append(await verify(model, args.output_dir))
        (args.output_dir / "summary.json").write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({key: value for key, value in summaries[-1].items()
                          if key not in {"observed_states", "usage"}}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
