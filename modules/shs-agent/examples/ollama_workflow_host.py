"""Opt-in small real-model workflow, one run, installed model only.

python examples/ollama_workflow_host.py --model gemma4:e4b
No downloads, server changes, model substitution or automatic workflow retries.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
from time import perf_counter

from shs_agent import AgentLimits, ProblemSolver, SubagentConfig
from shs_agent.llm import ChatOptions
from shs_agent.tool import ToolExecutor, ToolRegistry

if __package__:
    from .full_workflow_host import TreeObserver, evaluate, host_decisions, save_run, terminal_display
    from .workflow_support import configuration
else:
    from full_workflow_host import TreeObserver, evaluate, host_decisions, save_run, terminal_display
    from workflow_support import configuration


TASK = """Compute statistics on a tiny synthetic ledger. First ask the Host once to
confirm currency. Optionally create a short plan with plan_create(items, reason).
Then create exactly one child with subagent_spawn(instruction, context=[], config_id='analyst').
Tell it to call ledger_read and policy_read, filter rows using included_status,
calculate count and sum of amount, and return JSON with count, total, ids, currency.
Use actual task_id returned by spawn with subagent_wait(task_ids=[id]), then
subagent_result(task_id=id). Read and review that output. Finish with task_complete
alone and its output as a JSON string with exactly count, total, ids, currency.
Do not invent data or task IDs. Ordinary text is not completion. You have no ledger
tool yourself. Do not use conversation_search/read for data that has not been read.
If a child fails, report failure honestly; do not retry or create another child.
"""


def observed_features(tree):
    events = [e for trace in tree.traces.values() for e in trace.events]
    def event(kind):
        return any(e.type == kind for e in events)
    checks = {
        "llm_call": event("llm_call_finished"),
        "planning": event("plan_created"),
        "replanning": event("plan_revised"),
        "subagent_creation": event("subagent_created"),
        "recursive_depth_2": any(t.state().task.depth >= 2 for t in tree.traces.values()),
        "child_result_delivered": event("subagent_result_delivered"),
        "agent_communication": event("agent_message_received"),
        "host_answer_applied": any(e.type == "command_applied" and e.payload.get("command_type") == "answer" for e in events),
        "tool_denial": event("tool_host_denied"),
        "tool_approval": event("tool_host_approved"),
        "tool_application": event("tool_access_applied"),
        "ledger_handler_returned": any(e.type == "tool_finished" and e.payload.get("name") == "ledger_read"
                                       and e.payload.get("status") == "succeeded" for e in events),
    }
    return {name: "observed" if value else "not_observed" for name, value in checks.items()}


async def run(provider, model, *, display=None):
    executions, audit = [], []
    fixture = configuration(executions)
    registry = ToolRegistry()
    registry.register(fixture["host_tools"].get("ledger_read"))
    registry.register(fixture["subagents"]["B"].registry.get("policy_read"))
    limits = AgentLimits(max_steps=12, max_tool_calls=24, max_total_subagents=1,
                         max_subagent_depth=1, context_compact_threshold_chars=None,
                         host_response_timeout=None)
    options = ChatOptions(temperature=0, seed=73, max_tokens=1024,
                          extra={"think": False, "options": {"num_ctx": 8192}, "keep_alive": "1m"})
    root = await ProblemSolver(provider).start(TASK, model=model, options=options, limits=limits,
        subagents={"analyst": SubagentConfig(registry=registry, executor=ToolExecutor(registry))})
    queue = asyncio.Queue()
    observer = TreeObserver(root, decisions=queue, display=display)
    began = perf_counter()
    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(observer.run())
            group.create_task(host_decisions(root, queue, audit))
            result = await root.result()
    finally:
        if not root.done:
            await root.cancel()
            await root.result()
    observer.compare(result.tree)
    summary = {"model": model, "engine_status": result.status.value, "stop_reason": result.stop_reason,
               "elapsed_seconds": perf_counter() - began, "business_review": evaluate(result),
               "handler_executions": executions, "host_actions": audit,
               "features": observed_features(result.tree), "live_matches_archive": True,
               "complete": result.tree.complete, "details_complete": result.tree.details_complete,
               "limits": asdict(limits), "options": asdict(options),
               "tasks": {key: {"status": t.state().task.status.value, "reason": t.state().task.stop_reason,
                               "llm_calls": sum(e.type == "llm_call_started" for e in t.events),
                               "tool_calls": t.state().agent.tool_calls, "tool_errors": t.state().agent.tool_errors}
                         for key, t in result.tree.traces.items()}}
    return result, observer, summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="exact existing Ollama model tag")
    parser.add_argument("--host", default="http://localhost:11434")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    # Optional SDK is imported only in this opt-in entry point.
    from shs_agent.llm.provider.ollama import OllamaLLM
    provider = OllamaLLM(host=args.host)
    if args.model not in provider.get_available_models():
        parser.error("model is not installed; this script never downloads models")
    started = datetime.now(timezone.utc)
    directory = args.output_dir or Path("build") / ("phase8_ollama_" + started.strftime("%Y%m%dT%H%M%SZ"))
    result, observer, summary = asyncio.run(run(provider, args.model, display=None if args.quiet else terminal_display))
    summary.update(started_at=started.isoformat(), host=args.host)
    save_run(directory, result, observer, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("output:", result.output)
    print("archive:", directory / "task_tree.json")
    return 0 if summary["business_review"]["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
