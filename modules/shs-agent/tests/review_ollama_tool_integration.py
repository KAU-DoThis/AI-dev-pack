"""Review saved real-model evidence without contacting Ollama or changing raw traces.

Usage: python tests/review_ollama_tool_integration.py RUN_DIRECTORY
The original run's summaries remain unchanged. Unexercised architecture checks
are reported as not_observed, not a vacuous pass or a proven engine failure.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def review(folder):
    summary, tree, result = (read(folder / name) for name in ("summary.json", "tree.json", "result.json"))
    traces = tree["traces"]
    depths = {tid: t["depth"] for tid, t in summary["tasks"].items()}
    events = [(tid, e) for tid, tr in traces.items() for e in tr["events"]]
    counters = Counter(e["type"] for _, e in events)
    started_at = next(e["timestamp"] for e in traces[tree["root_task_id"]]["events"] if e["type"] == "created")
    terminal_failures = sorted([{"task_id": tid, "depth": depths[tid], "timestamp": e["timestamp"],
                                "status": e["payload"]["status"], "stop_reason": e["payload"]["stop_reason"]}
                               for tid, e in events if e["type"] == "finished" and e["payload"]["status"] != "completed"],
                              key=lambda x: x["timestamp"])
    first_failure = terminal_failures[0] if terminal_failures else None
    if first_failure:
        first_failure["seconds_after_root_creation"] = (datetime.fromisoformat(first_failure["timestamp"]) - datetime.fromisoformat(started_at)).total_seconds()
    finished = [(tid, e) for tid, e in events if e["type"] == "tool_finished"]
    tools = Counter(e["payload"]["name"] for _, e in finished)
    failures, empty, plain, responses, mismatches, semantic_rejections, questions = [], [], [], [], [], [], []
    observations, lifecycle, schema_observations, offered_violations, discarded = [], [], [], [], []
    for tid, trace in traces.items():
        artifacts = trace["artifacts"]
        by_operation = defaultdict(dict)
        for artifact in artifacts.values():
            by_operation[artifact.get("operation_id")][artifact["kind"]] = artifact
        applied = [e for e in trace["events"] if e["type"] == "tool_access_applied"]
        for e in trace["events"]:
            p = e["payload"]
            ref = {"task_id": tid, "depth": depths[tid], "sequence": e["sequence"], "event_id": e["event_id"]}
            if e["type"] in {"tool_access_requested", "tool_access_forwarded", "tool_host_approval_requested", "tool_host_denied",
                             "tool_host_approved", "tool_access_denied", "tool_approval_received", "tool_access_applied",
                             "tool_set_changed", "plan_revised", "command_applied"}:
                lifecycle.append({**ref, "type": e["type"], "payload": p})
            if e["type"] == "response_discarded":
                discarded.append({**ref, **p})
            if e["type"] == "question":
                questions.append({**ref, **p})
            if e["type"] == "tool_finished" and isinstance(p.get("result", {}).get("output"), dict):
                output = p["result"]["output"]
                if output.get("accepted") is False:
                    semantic_rejections.append({**ref, "name": p["name"], "output": output})
            if e["type"] == "tool_finished" and p["status"] != "succeeded":
                a = artifacts[p["arguments_ref"]["artifact_id"]]
                failures.append({**ref, "name": p["name"], "error": p["result"].get("error"),
                                 "arguments": a["data"]["requested_arguments"], "artifact_id": a["artifact_id"]})
            if e["type"] == "llm_common_request":
                request = artifacts[p["artifact_id"]]["data"]
                names = [t["name"] for t in request["tools"]]
                messages = request["messages"]
                schema_observations.append({**ref, "artifact_id": p["artifact_id"], "names": names,
                    "message_count": len(messages),
                    "message_json_chars": sum(len(json.dumps(m)) for m in messages),
                    "largest_messages": [{"index": i, "role": m["role"], "name": m.get("name"),
                        "json_chars": len(json.dumps(m))} for i, m in sorted(enumerate(messages),
                        key=lambda x: len(json.dumps(x[1])), reverse=True)[:5]]})
                if "export_customer_contacts" in names or ("database_query" in names and
                    (depths[tid] != 2 or not any(a["sequence"] < e["sequence"] and
                        a["payload"]["requested_tool"] == "database_query" for a in applied))):
                    offered_violations.append(ref)
        for operation_id, group in by_operation.items():
            if "llm_adapter_response" not in group:
                continue
            adapter = group["llm_adapter_response"]
            raw = adapter["data"]["response"]
            message = raw.get("message") or {}
            fields = group.get("llm_common_response", {}).get("data", {}).get("fields", {})
            raw_calls = [c["function"] for c in message.get("tool_calls") or []]
            common_calls = [{"name": c["name"], "arguments": c["arguments"]} for c in fields.get("tool_calls") or []]
            preserved = (message.get("content") == fields.get("content") and raw_calls == common_calls)
            record = {"task_id": tid, "depth": depths[tid], "operation_id": operation_id,
                      "artifact_id": adapter["artifact_id"], "timestamp": adapter["timestamp"],
                      "done_reason": raw.get("done_reason"), "content": message.get("content"),
                      "tool_names": [c["name"] for c in raw_calls],
                      "prompt_tokens": raw.get("prompt_eval_count", 0), "completion_tokens": raw.get("eval_count", 0),
                      "eval_duration_ns": raw.get("eval_duration", 0), "load_duration_ns": raw.get("load_duration", 0),
                      "total_duration_ns": raw.get("total_duration", 0), "adapter_preserved": preserved}
            responses.append(record)
            if not preserved:
                mismatches.append(record)
            if not message.get("content") and not raw_calls:
                empty.append(record)
            elif message.get("content") and not raw_calls:
                plain.append(record)
        observations.append({"task_id": tid, "depth": depths[tid], "artifact_count": len(artifacts),
                             "event_count": len(trace["events"])})
    meaningful = [r for r in responses if r["content"] or r["tool_names"]]
    count = sum(r["completion_tokens"] for r in meaningful)
    duration = sum(r["eval_duration_ns"] for r in meaningful) / 1e9
    timings = sorted(responses, key=lambda r: r["timestamp"])
    resources = read(folder / "resources.json")
    loaded = []
    for sample in resources:
        for model in sample.get("loaded", {}).get("models", []):
            if model["name"] == summary["model"]:
                loaded.append({"at": sample["at"], **{k: model.get(k) for k in ("size", "size_vram", "context_length")}})
    failure_counts = Counter((f["name"], (f["error"] or {}).get("message")) for f in failures)
    checks = {k: dict(v) for k, v in summary["checks"].items()}
    applicable = {
        "host_question_answer_exact_id": bool(counters["question"]),
        "host_instruction_applied": any(a["action"] == "instruction" for a in result["host_actions"]),
        "contacts_denied_and_never_executed": bool(counters["tool_host_denied"]),
        "denial_followed_by_replanning": bool(counters["tool_host_denied"]),
        "database_requested_forwarded_approved_applied": bool(counters["tool_host_approved"]),
        "schemas_isolated_and_only_after_application": bool(counters["tool_access_applied"]),
        "safe_tool_application_boundary": bool(counters["tool_access_applied"]),
        "real_sqlite_execution": any(op["name"] == "database_query" for op in result["fixture_operations"]),
        "tool_batches_balanced": bool(finished),
        "agent_tool_calls_from_real_ollama": bool(finished),
        "parents_collected_child_results": len(traces) == 3,
    }
    for key, observed in applicable.items():
        if not observed:
            checks[key] = {"status": "not_observed", "detail": "This workflow did not exercise the prerequisite; see lifecycle and tools."}
    checks["adapter_preserves_sdk_content_and_tool_calls"] = {
        "status": "fail" if mismatches else "pass" if responses else "not_observed",
        "detail": {"responses": len(responses), "mismatches": len(mismatches)}}
    checks["no_forbidden_schema_exposure"] = {"status": "fail" if offered_violations else "pass",
                                             "detail": {"requests": len(schema_observations), "violations": offered_violations}}
    source_files = ["tree.json", "summary.json", "result.json", "resources.json", "setup.json", "live.jsonl"]
    oracle = read(folder.parent / "fixture" / "oracle.json")
    with sqlite3.connect((folder.parent / "fixture" / "ledger.sqlite").resolve().as_uri() + "?mode=ro", uri=True) as db:
        event_orders = dict(db.execute("SELECT event_id, order_id FROM payment_events"))
    task_outputs = []
    for tid, value in result["tasks"].items():
        try:
            output = json.loads(value.get("output", ""))
            if not isinstance(output, dict): output = {}
        except (TypeError, ValueError):
            output = {}
        affected = output.get("affected_orders", [])
        evidence = output.get("evidence_ids", [])
        valid_rows = isinstance(affected, list) and all(isinstance(x, dict) and isinstance(x.get("order_id"), str)
            and type(x.get("net_overcharge_krw")) is int and x["net_overcharge_krw"] > 0 for x in affected)
        rows_match = valid_rows and sorted(affected, key=lambda x: x["order_id"]) == sorted(oracle["affected_orders"], key=lambda x: x["order_id"])
        valid_evidence = isinstance(evidence, list) and bool(evidence) and all(isinstance(x, str) and x in event_orders for x in evidence)
        coverage = valid_evidence and {event_orders[x] for x in evidence} >= {x["order_id"] for x in oracle["affected_orders"]}
        task_outputs.append({"task_id": tid, "depth": depths[tid], "status": summary["tasks"][tid]["status"],
            "parsed_output": output, "exact_rows": bool(rows_match),
            "exact_total": type(output.get("total_overcharge_krw")) is int and output["total_overcharge_krw"] == oracle["total_overcharge_krw"],
            "exact_source": output.get("source_ref") == oracle["source_ref"], "valid_evidence": bool(valid_evidence),
            "evidence_covers_affected_orders": bool(coverage)})
    return {
        "model": summary["model"], "attempts": summary["attempts"], "root_status": summary["root_status"],
        "stop_reason": summary["stop_reason"], "business_success": summary["business_success"],
        "elapsed_seconds": summary["elapsed_seconds"], "tasks": summary["tasks"], "llm_calls": summary["llm_calls"],
        "tool_calls": len(finished), "tool_errors": len(failures), "tools": dict(tools),
        "first_task_failure": first_failure,
        "llm_latency_seconds": summary["latency_seconds"], "reported_usage": summary["usage"],
        "meaningful_generation": {"tokens": count, "eval_seconds": duration,
            "tokens_per_second": count / duration if duration and count else None,
            "excludes_empty_response_calls": len(empty), "note": "Provider eval timer only, excludes prefill/load/waits; not useful-task throughput."},
        "first_response": timings[0] if timings else None, "max_prompt_tokens": max((r["prompt_tokens"] for r in responses), default=0),
        "checks": checks, "event_counts": dict(counters), "trace_observations": observations,
        "failure_counts": [{"name": name, "error": error, "count": n} for (name, error), n in failure_counts.items()],
        "failures": failures, "empty_responses": empty, "plain_assistant_responses": plain,
        "semantic_rejections": semantic_rejections, "host_questions": questions,
        "adapter_mismatches": mismatches, "lifecycle": sorted(lifecycle, key=lambda e: (e["depth"], e["sequence"])),
        "discarded_responses": discarded,
        "schema_observations": schema_observations, "resource_loaded_samples": loaded,
        "gpu_samples": [{"at": s["at"], "gpu": s.get("gpu"), "error": s.get("error")} for s in resources],
        "host_actions": result["host_actions"], "fixture_operations": result["fixture_operations"],
        "output": summary["output"], "parsed_output": summary["parsed_output"],
        "task_output_review": task_outputs,
        "source_sha256": {name: hashlib.sha256((folder / name).read_bytes()).hexdigest() for name in source_files},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_directory", type=Path)
    args = parser.parse_args()
    metadata = read(args.run_directory / "metadata.json")
    reviews = []
    for model in metadata["models"]:
        folder = args.run_directory / model.replace(":", "_").replace(".", "_")
        if not (folder / "summary.json").exists():
            continue
        data = review(folder)
        # Derived review is reproducible and replaceable; raw experiment files are never written.
        (folder / "evidence_review.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        reviews.append({k: data[k] for k in ("model", "root_status", "stop_reason", "elapsed_seconds", "llm_calls",
                       "tool_calls", "tool_errors", "meaningful_generation", "business_success", "checks")})
    (args.run_directory / "evidence_review.json").write_text(json.dumps(reviews, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps([{k: v for k, v in r.items() if k != "checks"} for r in reviews], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
