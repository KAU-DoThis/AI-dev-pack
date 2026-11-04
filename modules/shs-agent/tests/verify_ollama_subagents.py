"""Opt-in real Ollama root/direct-child verification; no pulls or server changes.

The real parent chooses every spawn/wait/result tool call. Host gates only expose
observable overlap of task/tool execution. Overlapping SDK requests include queue
time and are explicitly not evidence of concurrent server token evaluation.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
from dataclasses import asdict
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from time import perf_counter
from urllib.request import urlopen
from uuid import uuid4

from shs_agent import AgentLimits, ProblemSolver, SubagentConfig, TaskStatus, TaskTreeSnapshot
from shs_agent.llm import ChatOptions
from shs_agent.llm.provider.ollama import OllamaLLM
from shs_agent.tool import Tool, ToolExecutor, ToolRegistry, ToolSpec

MODELS = ("gemma4:e4b", "qwen3.5:9b")
HOST = "http://localhost:11434"
SERVER_LOG = Path.home() / "AppData/Local/Ollama/server.log"
INSTRUCTION = """Perform a controlled sub-agent integration exercise with two independent partitions, A and B.
Use the supplied tools; do not ask the host, install anything or invent evidence.
This is a test of your actual tool decisions, not a request for a description of tool usage.

1. First emit exactly TWO subagent_spawn tool calls in the SAME response, with no
   other tools. Both use config_id='worker' and explicitly include context=[] (or
   a background string array). Delegate A to one child and B to the other.
   Each child's instruction must say: call read_partition with your assigned
   partition (A or B), compute the sum of its values, write a memo with kind='finding'
   and the returned source_ref in source_refs, then call task_complete alone with
   output a JSON object containing partition, sum and source_ref. Do not read the
   other partition. The read tool may wait for the parent probe; this is expected.
2. After BOTH spawn responses, call parent_probe once and subagent_status to inspect
   your children. parent_probe lets both children finish their gated reads. Do not
   wait for a child before creating the other child or before calling parent_probe.
3. Call subagent_wait with BOTH actual returned task IDs. Then call subagent_result
   for EACH actual task ID. Read their output and memos before your final decision.
   Never substitute a made-up ID or assume a child finished based on spawn alone.
4. Complete using task_complete ALONE. Its output must be a JSON object, no Markdown:
   {"partitions":{"A":<sum>,"B":<sum>},"total":<A+B>,
    "source_refs":[<exact A source_ref>,<exact B source_ref>],"parent_ref":<probe source_ref>}.
   Use values and source references actually returned by tools/children.
Planning is optional. Do not claim inference was parallel or faster; the host measures that separately.
"""


def get_api(endpoint):
    with urlopen(HOST + "/api/" + endpoint, timeout=10) as response:
        return json.load(response)


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def read_log():
    try:
        return SERVER_LOG.read_bytes()
    except OSError:
        return b""


def log_evidence(before, after):
    text = after.decode("utf-8", errors="replace")
    values = re.findall(r"OLLAMA_NUM_PARALLEL:([^\s\]]+)", text)
    appended = after[len(before):] if after.startswith(before) else after
    lines = appended.decode("utf-8", errors="replace").splitlines()
    # Do not copy entire server environment/config lines (could include secrets).
    selected = [line for line in lines if any(pattern in line for pattern in (
        "n_seq_max", "n_ctx ", "n_ctx_per_seq", "--parallel", "parallel=", "--model", "offloaded"))
        and 'msg="server config"' not in line]
    return {"log_path": str(SERVER_LOG), "readable": bool(after),
            "prefix_preserved": after.startswith(before), "before_bytes": len(before), "after_bytes": len(after),
            "configured_num_parallel": values[-1] if values else None,
            "new_runner_lines": selected,
            "new_n_seq_max": [int(match) for line in selected
                              for match in re.findall(r"n_seq_max\s*=\s*(\d+)", line)]}


class Fixture:
    def __init__(self, gate_timeout):
        self.values = {"A": [13, 8, 21], "B": [5, 17, 9]}
        nonce = uuid4().hex[:12]
        self.refs = {part: f"fixture:{nonce}:{part}" for part in self.values}
        self.parent_ref = f"fixture:{nonce}:parent"
        self.entered = {part: asyncio.Event() for part in self.values}
        self.release = asyncio.Event()
        self.gate_timeout = gate_timeout
        self.operations = []
        self.root = None
        self.probe = None

    async def read_partition(self, partition):
        if partition not in self.values:
            raise ValueError("partition must be A or B")
        if self.entered[partition].is_set():
            raise ValueError("each partition should be read once in this exercise")
        record = {"name": "read_partition", "partition": partition, "entered": perf_counter()}
        self.operations.append(record)
        self.entered[partition].set()
        try:
            await asyncio.wait_for(self.release.wait(), self.gate_timeout)
            output = {"partition": partition, "values": self.values[partition], "source_ref": self.refs[partition]}
            record["output"] = output
            return output
        except TimeoutError:
            record["error"] = "gate_timeout: parent_probe was not reached in time"
            raise RuntimeError(record["error"])
        finally:
            record["returned"] = perf_counter()

    async def parent_probe(self):
        record = {"name": "parent_probe", "entered": perf_counter()}
        self.operations.append(record)
        try:
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in self.entered.values())),
                                   self.gate_timeout)
            states = await self.root.subagents.children()
            self.probe = {"observed_at": datetime.now(timezone.utc).isoformat(),
                          "monotonic": perf_counter(), "children": states,
                          "both_gates_entered": all(event.is_set() for event in self.entered.values()),
                          "release_was_closed": not self.release.is_set()}
            output = {"source_ref": self.parent_ref, "partitions": ["A", "B"],
                      "both_child_reads_entered": self.probe["both_gates_entered"]}
            record["output"] = output
            return output
        finally:
            record["returned"] = perf_counter()
            self.release.set()

    def registry(self, child):
        registry = ToolRegistry()
        if child:
            registry.register(Tool(ToolSpec("read_partition", "Read your assigned partition's values and evidence. May wait for the parent probe.", {
                "type": "object", "properties": {"partition": {"type": "string", "enum": ["A", "B"]}},
                "required": ["partition"], "additionalProperties": False,
            }), self.read_partition))
        else:
            registry.register(Tool(ToolSpec("parent_probe", "After both spawns, inspect both child read gates and release them; returns a source_ref.", {
                "type": "object", "properties": {}, "additionalProperties": False,
            }), self.parent_probe))
        return registry


def raw_tools(trace, name):
    values = []
    for event in trace.events:
        if event.type != "tool_finished" or event.payload["name"] != name or event.payload["status"] != "succeeded":
            continue
        detail = trace.artifact(event.payload["result_ref"]["artifact_id"])
        raw_id = detail.data.get("raw_result_artifact_id")
        if raw_id:
            values.append((event, trace.artifact(raw_id).data["output"]))
    return values


def rpc_overlap(tree):
    windows = []
    for task_id, trace in tree.traces.items():
        calls = {}
        for event in trace.events:
            if event.type in {"llm_adapter_request", "llm_adapter_response"}:
                record = calls.setdefault(event.payload["llm_call_id"], {"task_id": task_id,
                    "llm_call_id": event.payload["llm_call_id"]})
                key = "start" if event.type == "llm_adapter_request" else "end"
                record[key] = event.payload["observed_at"]
        windows.extend(value for value in calls.values() if "start" in value and "end" in value)
    overlaps = []
    for index, first in enumerate(windows):
        for second in windows[index + 1:]:
            if first["task_id"] == second["task_id"]:
                continue
            start = max(datetime.fromisoformat(first["start"]), datetime.fromisoformat(second["start"]))
            end = min(datetime.fromisoformat(first["end"]), datetime.fromisoformat(second["end"]))
            if end > start:
                overlaps.append({"first": first["llm_call_id"], "second": second["llm_call_id"],
                                 "tasks": [first["task_id"], second["task_id"]],
                                 "overlap_seconds": round((end - start).total_seconds(), 6)})
    endpoints = [(datetime.fromisoformat(w[edge]), delta) for w in windows
                 for edge, delta in (("start", 1), ("end", -1))]
    active = maximum = 0
    for _, delta in sorted(endpoints):
        active += delta
        maximum = max(active, maximum)
    return {"boundary": "SDK call request/response observation; includes transport, scheduling and queue time",
            "windows": windows, "overlaps": overlaps, "max_in_flight": maximum,
            "server_parallel_token_evaluation_proven": False}


def assess(result, fixture, live, states, live_errors):
    tree = result.tree
    restored = TaskTreeSnapshot.from_json(tree.to_json())
    root_trace = tree.traces[result.task_id]
    children = {key: trace for key, trace in tree.traces.items() if key != result.task_id}
    creations = [event for event in root_trace.events if event.type == "subagent_created"]
    spawns = raw_tools(root_trace, "subagent_spawn")
    reads = raw_tools(root_trace, "subagent_result")
    waits = raw_tools(root_trace, "subagent_wait")
    spawn_calls = []
    provenance = len(creations) == 2
    for event in creations:
        source = root_trace.artifact(event.payload["artifact_id"]).data["source"]
        request = root_trace.artifact(source["request_artifact_id"])
        response = root_trace.artifact(request.data["response_artifact_id"])
        provenance &= source["task_id"] == result.task_id and request.data["name"] == "subagent_spawn"
        provenance &= any(call["name"] == "subagent_spawn" and call["id"] == source["tool_call_id"]
                          for call in response.data["tool_calls"])
        spawn_calls.append(source["llm_call_id"])
    child_outputs, child_evidence = {}, {}
    child_tool_paths = True
    for task_id, trace in children.items():
        saved = tree.result(task_id)
        try:
            output = json.loads(saved["output"])
        except (ValueError, TypeError):
            output = {}
        partition = output.get("partition") if isinstance(output, dict) else None
        if partition in fixture.values:
            child_outputs[partition] = output
            child_evidence[partition] = any(memo["task_id"] == task_id and fixture.refs[partition] in memo["source_refs"]
                                           for memo in saved["memos"])
        records = raw_tools(trace, "read_partition")
        child_tool_paths &= bool(len(records) == 1 and partition in fixture.values
            and records[0][1]["partition"] == partition and raw_tools(trace, "memo_write")
            and raw_tools(trace, "task_complete"))
    probe = fixture.probe or {}
    task_overlap = bool(probe.get("both_gates_entered") and probe.get("release_was_closed")
        and len(probe.get("children", [])) == 2 and all(not child["done"]
            and child["state"]["agent"]["activity"] == "tool_execution" for child in probe["children"]))
    try:
        output = json.loads(result.output)
    except (ValueError, TypeError):
        output = {}
    correct_final = isinstance(output, dict) and output.get("partitions") == {"A": 42, "B": 31}
    correct_final &= isinstance(output, dict) and output.get("total") == 73
    refs_correct = isinstance(output, dict) and isinstance(output.get("source_refs"), list)
    refs_correct &= bool(isinstance(output, dict) and set(output.get("source_refs", [])) == set(fixture.refs.values())
                         and output.get("parent_ref") == fixture.parent_ref)
    available_reads = [(event, value) for event, value in reads if value.get("available")]
    complete_events = raw_tools(root_trace, "task_complete")
    all_calls = [event for trace in tree.traces.values() for event in trace.events if event.type == "llm_call_finished"]
    final_request = None
    if complete_events:
        final_id = complete_events[-1][0].payload["llm_call_id"]
        final_request = next(a.data for a in root_trace.artifacts.values()
                             if a.kind == "llm_common_request" and a.operation_id == final_id)
    checks = {
        "root_completed": result.status == TaskStatus.COMPLETED,
        "exactly_two_registered_children": len(children) == len(creations) == len(spawns) == 2,
        "spawns_chosen_by_real_parent_response": provenance,
        "two_spawns_in_one_llm_response": len(spawn_calls) == 2 and len(set(spawn_calls)) == 1,
        "spawn_returns_before_child_completion": len(spawns) == 2 and all(not value["done"] for _, value in spawns),
        "parent_work_while_both_child_tools_active": task_overlap,
        "status_tool_executed": bool(raw_tools(root_trace, "subagent_status")),
        "wait_selected_both_children": bool(waits and any({c["task_id"] for c in value["children"]} == set(children)
                                                         and value["reason"] == "terminated" for _, value in waits)),
        "results_collected_for_both_children": {value["task_id"] for _, value in available_reads} == set(children) and len(children) == 2,
        "results_read_before_parent_completion": bool(complete_events and len(available_reads) == 2 and
            all(event.sequence < complete_events[-1][0].sequence for event, _ in available_reads)),
        "final_decision_contains_both_result_messages": bool(final_request and
            len([m for m in final_request["messages"] if m["role"] == "tool" and m.get("name") == "subagent_result"]) == 2),
        "both_children_completed": len(children) == 2 and all(tree.result(key)["status"] == "completed" for key in children),
        "children_used_real_tool_memo_completion_loop": len(children) == 2 and child_tool_paths,
        "child_answers_match_actual_fixture": set(child_outputs) == {"A", "B"} and all(
            child_outputs[part].get("sum") == sum(values) and child_outputs[part].get("source_ref") == fixture.refs[part]
            for part, values in fixture.values.items()),
        "child_memo_provenance": len(child_evidence) == 2 and all(child_evidence.values()),
        "parent_final_values_correct": bool(correct_final),
        "parent_final_evidence_exact": bool(refs_correct),
        "no_tool_errors": all(trace.state().agent.tool_errors == 0 for trace in tree.traces.values()),
        "tree_complete": tree.complete,
        "tree_details_complete": tree.details_complete,
        "json_roundtrip_and_cross_task_refs": tree.to_json() == restored.to_json(),
        "all_live_event_streams_match_final_traces": all(tuple(live.get(key, [])) == trace.events for key, trace in tree.traces.items()),
        "live_artifacts_resolved": not live_errors,
        "live_state_cursors_replay": all(restored.traces[key].state(at_sequence=s.last_event_sequence) == s
                                          for key, values in states.items() for s in values),
        "all_five_llm_observation_boundaries": bool(all_calls) and all(set(event.payload["artifacts"]) == {
            "common_request", "adapter_request", "adapter_response", "common_response", "normalized_response"}
            and event.payload["validation_succeeded"] for event in all_calls),
    }
    return checks


async def verify(model, output_dir, args):
    started, log_before = perf_counter(), read_log()
    fixture = Fixture(args.gate_timeout)
    provider = OllamaLLM(HOST)
    # Existing SDK, host-side HTTP timeout only; no server configuration mutation.
    from ollama import Client
    provider.client = Client(host=HOST, timeout=180)
    options = ChatOptions(temperature=0, max_tokens=4096,
                          extra={"think": True, "options": {"num_ctx": 16384}})
    child_registry = fixture.registry(True)
    child_limits = AgentLimits(max_steps=8, max_tool_calls=12, host_response_timeout=10,
                                context_compact_threshold_chars=None)
    root_limits = AgentLimits(max_steps=14, max_tool_calls=28, max_total_subagents=2,
                               max_subagent_depth=1, host_response_timeout=10,
                               context_compact_threshold_chars=None)
    config = SubagentConfig(provider=provider, model=model, options=options, limits=child_limits,
                            registry=child_registry, executor=ToolExecutor(child_registry))
    root = await ProblemSolver().start(INSTRUCTION, provider=provider, model=model,
        options=options, registry=fixture.registry(False), limits=root_limits, subagents={"worker": config})
    fixture.root = root
    live, states, errors, watchers = {}, {}, [], []

    async def observe(session):
        live[session.task_id], states[session.task_id] = [], []
        async for event in session.events():
            live[session.task_id].append(event)
            for artifact_id in event.artifact_ids:
                try:
                    artifact = await session.artifact(artifact_id)
                    if artifact.task_id != session.task_id:
                        errors.append("wrong task for live artifact")
                except KeyError:
                    errors.append("missing live artifact " + artifact_id)
            if event.type in {"llm_call_started", "tool_started", "finished"}:
                states[session.task_id].append(await session.state())
            if event.type == "subagent_created":
                watchers.append(asyncio.create_task(observe(root.subagents.child(event.payload["task_id"]))))
            if event.type in {"subagent_created", "subagent_result_delivered", "subagent_wait_finished", "tool_finished", "finished"}:
                print(json.dumps({"model": model, "task_id": session.task_id, "event": event.type,
                    "sequence": event.sequence, "name": event.payload.get("name"), "status": event.payload.get("status"),
                    "error": event.payload.get("result", {}).get("error")}), flush=True)

    root_watcher = asyncio.create_task(observe(root))
    timed_out = False

    async def watchdog():
        nonlocal timed_out
        await asyncio.sleep(args.run_timeout)
        timed_out = True
        fixture.release.set()
        if not root.done:
            await root.cancel()

    timer = asyncio.create_task(watchdog())
    try:
        result = await root.result()
        await root_watcher
        await asyncio.gather(*watchers)
    finally:
        timer.cancel()
        await asyncio.gather(timer, return_exceptions=True)
        fixture.release.set()
    elapsed = round(perf_counter() - started, 3)
    stem = model.replace(":", "_").replace(".", "_")
    # Save primary evidence before auditing, even if the audit itself finds an issue.
    (output_dir / f"{stem}.tree.json").write_text(result.tree.to_json(indent=2), encoding="utf-8")
    save(output_dir / f"{stem}.result.json", {"root_task_id": root.task_id,
        "tasks": {key: result.tree.result(key) for key in result.tree.traces},
        "fixture_operations": fixture.operations, "probe": fixture.probe,
        "expected": {"partitions": {"A": 42, "B": 31}, "total": 73,
                     "source_refs": fixture.refs, "parent_ref": fixture.parent_ref}})
    try:
        checks = assess(result, fixture, live, states, errors)
    except Exception as exc:
        checks = {"audit_completed": False}
        errors.append(f"Audit error: {type(exc).__name__}: {exc}")
    checks["no_watchdog_timeout"] = not timed_out
    concurrency = rpc_overlap(result.tree)
    logs = log_evidence(log_before, read_log())
    save(output_dir / f"{stem}.server_evidence.json", logs)
    task_stats = {}
    for key, trace in result.tree.traces.items():
        calls = [event.payload for event in trace.events if event.type == "llm_call_finished"]
        task_stats[key] = {"status": trace.state().task.status, "steps": trace.state().agent.steps,
            "tool_calls": trace.state().agent.tool_calls, "tool_errors": trace.state().agent.tool_errors,
            "usage": [value["usage"] for value in calls], "events": len(trace.events), "artifacts": len(trace.artifacts),
            "thinking_chars": sum(len(artifact.data["fields"].get("thinking") or "")
                                  for artifact in trace.artifacts.values() if artifact.kind == "llm_common_response")}
    summary = {"model": model, "root_task_id": root.task_id, "status": result.status.value,
        "accepted": all(checks.values()), "checks": checks, "errors": errors, "elapsed_seconds": elapsed,
        "output": result.output, "task_stats": task_stats, "concurrency": concurrency,
        "server_evidence": logs, "loaded_after": await asyncio.to_thread(get_api, "ps"),
        "options": asdict(options), "root_limits": asdict(root_limits), "child_limits": asdict(child_limits),
        "live_states": {key: [state.to_dict() for state in values] for key, values in states.items()}}
    save(output_dir / f"{stem}.summary.json", summary)
    print(json.dumps({"model": model, "accepted": summary["accepted"], "checks": checks,
                      "elapsed_seconds": elapsed, "max_sdk_in_flight": concurrency["max_in_flight"],
                      "configured_num_parallel": logs["configured_num_parallel"],
                      "new_n_seq_max": logs["new_n_seq_max"]}), flush=True)
    return summary


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", choices=MODELS, action="append")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--gate-timeout", type=float, default=120)
    parser.add_argument("--run-timeout", type=float, default=360)
    args = parser.parse_args()
    models = args.model or list(MODELS)
    folder = args.output_dir or Path("build/ollama_subagents") / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
    folder.mkdir(parents=True, exist_ok=False)
    available = get_api("tags")["models"]
    if set(models) - {value["name"] for value in available}:
        raise RuntimeError("required model is absent; no pull was attempted")
    save(folder / "metadata.json", {"started_at": datetime.now(timezone.utc).isoformat(),
        "server": get_api("version"), "ollama_sdk": version("ollama"), "host": HOST,
        "models": [value for value in available if value["name"] in models],
        "loaded_before": get_api("ps"), "instruction": INSTRUCTION,
        "gate_timeout": args.gate_timeout, "run_timeout": args.run_timeout,
        "protocol": "real model tool selection; Host does not invoke spawn or result tools",
        "inference_parallelism": "SDK windows do not prove server token-evaluation concurrency"})
    print(json.dumps({"output_dir": str(folder.resolve()), "models": models}), flush=True)
    summaries = []
    for model in models:
        summaries.append(await verify(model, folder, args))
        save(folder / "summary.json", summaries)
    return 0 if all(value["accepted"] for value in summaries) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
