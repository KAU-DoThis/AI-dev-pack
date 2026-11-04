"""Opt-in real Ollama Planning/Replanning verification with controlled local tools.

Uses preinstalled models only. No pulls, server configuration changes, repository
edits, or automatic retries. Fixture tools operate on an in-memory source string;
the unavailable runtime is deliberately simulated, not a machine diagnosis.
Each run preserves trace, final output, checks, fixture operations and metadata.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import json
from dataclasses import asdict
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from time import perf_counter
from urllib.request import urlopen

from shs_agent import AgentLimits, ProblemSolver, TaskStatus, TracerSnapshot
from shs_agent.llm import ChatOptions
from shs_agent.llm.provider.ollama import OllamaLLM
from shs_agent.tool import Tool, ToolRegistry, ToolSpec


MODELS = ("gemma4:e4b", "qwen3.5:9b")
HOST = "http://localhost:11434"
PLAN_EVENTS = {"plan_created", "plan_updated", "plan_revised"}
INSTRUCTION = """Fix the in-memory add_numbers fixture and honestly report validation.
This is a controlled integration exercise. Tools only affect an in-memory fixture,
not repository files. Do not install anything, request extra tools, or ask the host.

Use Planning explicitly: first create a three-item plan with stable IDs research,
edit, verify; edit depends on research and verify depends on edit. Keep the plan
updated as you read actual results. Use inspect_fixture to investigate, mark research
completed with its returned source_ref, then apply_fixture_fix and mark edit completed
with its returned source_ref. Call run_fixture_tests once to attempt runtime validation.
Read each tool result before deciding what succeeded; do not retry failed validation.

If runtime validation is unavailable, use plan_revise to change only the validation
work: preserve the research and edit items including their completed results and
evidence, replace verify with a new item review depending on edit, and record the
observed error reference and reason. The host permits static_review_fixture as a
limited fallback. Run it, record its actual result and source_ref, and explicitly
state that runtime tests were not run. Static review is not a runtime test pass.

Before finishing, call plan_read with revision=1 to confirm the original verify
item remains in history. Keep all current revision numbers consistent; read the
current plan if needed. Complete by calling task_complete alone, with output a JSON
object (no Markdown) containing runtime_tests, static_review and limitations.
Use runtime_tests='not_run' if the runtime was unavailable, and static_review='passed'
only after a successful static review. Explain the unverified behavior in limitations.
"""

CONTRACT_REMINDERS = """
Contract reminders for this run:
- Your first tool call must be plan_create alone, before inspect_fixture. Every new
  item must include both item_id and a nonempty description. status_note does not
  replace description. This applies to the new review item in plan_revise too.
- Evidence for a completed item goes in that item's source_refs inside updates,
  not only in the top-level source_refs describing why the revision changed.
- Preserve research and edit in plan_revise using item_id-only entries.
- Use the exact observed error reference fixture:runtime:missing in the top-level
  source_refs for that revise. Do not invent tool-call IDs.
- After the static review tool returns, update review to completed and attach its
  returned source_ref to that item before reading revision 1 and completing the task.
"""


class FixtureRuntimeUnavailable(RuntimeError):
    pass


class Fixture:
    def __init__(self):
        self.source = "def add_numbers(a, b):\n    return a - b\n"
        self.operations = []

    def inspect(self):
        result = {"source": self.source, "requirement": "Return the sum of a and b",
                  "source_ref": "fixture:source:v0"}
        self.operations.append({"name": "inspect_fixture", "output": result})
        return result

    def fix(self):
        self.source = "def add_numbers(a, b):\n    return a + b\n"
        result = {"source": self.source, "source_ref": "fixture:source:v1", "runtime_tests": "not_run"}
        self.operations.append({"name": "apply_fixture_fix", "output": result})
        return result

    def tests(self):
        message = ("fixture:runtime:missing: controlled fixture runtime is unavailable; "
                   "no runtime tests were executed. Static review is permitted but cannot establish a test pass.")
        self.operations.append({"name": "run_fixture_tests", "error": message, "runtime_tests": "not_run"})
        raise FixtureRuntimeUnavailable(message)

    def review(self):
        tree = ast.parse(self.source)
        expected = ast.parse("def add_numbers(a, b):\n    return a + b\n")
        matched = ast.dump(tree) == ast.dump(expected)
        result = {"static_review": "passed" if matched else "failed", "runtime_tests": "not_run",
                  "source_ref": "fixture:static-review:v1", "method": "AST structure comparison only"}
        self.operations.append({"name": "static_review_fixture", "output": result})
        return result

    def registry(self):
        registry = ToolRegistry()
        for name, description, handler in (
            ("inspect_fixture", "Read the in-memory fixture and expected behavior.", self.inspect),
            ("apply_fixture_fix", "Apply the prepared addition fix to the in-memory fixture. Does not run tests.", self.fix),
            ("run_fixture_tests", "Attempt runtime tests for the fixture. May return an environment error.", self.tests),
            ("static_review_fixture", "Compare fixture AST with the required addition structure; does not execute code or tests.", self.review),
        ):
            registry.register(Tool(ToolSpec(name, description, {
                "type": "object", "properties": {}, "additionalProperties": False,
            }), handler))
        return registry


def plan_in(messages):
    values = [json.loads(message["content"].split("\n", 1)[1]) for message in messages
              if message.get("content", "").startswith("Current task plan")]
    return values[0] if len(values) == 1 else None


def assess(result, fixture, live, states, live_errors):
    trace = result.tracer
    restored = TracerSnapshot.from_json(trace.to_json())
    changes = [e for e in trace.events if e.type in PLAN_EVENTS]
    tools = [e for e in trace.events if e.type == "tool_finished"]
    created = [e for e in changes if e.type == "plan_created"]
    revised = [e for e in changes if e.type == "plan_revised"]
    failed = [e for e in tools if e.payload["name"] == "run_fixture_tests" and e.payload["status"] == "failed"]
    requests = {a.operation_id: a for a in trace.artifacts.values() if a.kind == "llm_common_request"}
    checks = {
        "task_completed": result.status == TaskStatus.COMPLETED and result.stop_reason == "task_complete",
        "live_events_equal_trace": tuple(live) == trace.events,
        "live_artifacts_and_plan_state_consistent": not live_errors,
        "sampled_state_cursors_replay": all(restored.state(at_sequence=s.last_event_sequence) == s for s in states),
        "json_round_trip": restored.to_json() == trace.to_json(),
        "trace_complete": trace.complete,
        "details_complete": trace.details_complete,
        "exactly_one_initial_plan": len(created) == 1,
        "plan_revised": bool(revised),
        "one_intentional_environment_error": len(failed) == 1,
        "no_unexpected_tool_errors": result.tool_errors == len(failed),
        "four_planning_tools_executed": {"plan_create", "plan_read", "plan_update", "plan_revise"}.issubset(
            {e.payload["name"] for e in tools if e.payload["status"] == "succeeded"}),
        "fixture_operations_in_order_once": [op["name"] for op in fixture.operations] == [
            "inspect_fixture", "apply_fixture_fix", "run_fixture_tests", "static_review_fixture"],
    }
    expected_ref = None
    state_history_ok = True
    revision_links_ok = True
    for event in trace.events:
        if event.type in PLAN_EVENTS:
            payload = event.payload
            revision_links_ok &= payload["before_artifact_id"] == expected_ref
            expected_ref = payload["after_artifact_id"]
            artifact = restored.artifact(expected_ref)
            request = restored.artifact(payload["request_artifact_id"])
            revision_links_ok &= (artifact.operation_id == payload["tool_execution_id"]
                                  and request.data["llm_call_id"] == payload["llm_call_id"]
                                  and payload["request_artifact_id"] in artifact.references)
        state_history_ok &= restored.state(at_sequence=event.sequence).task.plan_artifact_id == expected_ref
    checks["all_historical_states_match_revisions"] = state_history_ok
    checks["revision_before_after_and_call_links"] = revision_links_ok
    checks["final_plan_matches_trace"] = bool(result.plan and expected_ref
        and restored.artifact(expected_ref).data == result.plan.to_dict())
    checks["initial_three_item_plan"] = bool(created and
        [i["item_id"] for i in restored.artifact(created[0].payload["after_artifact_id"]).data["items"]]
        == ["research", "edit", "verify"])
    checks["plan_created_before_fixture_work"] = bool(created and tools and
        next((e.payload["name"] for e in tools if e.payload["status"] == "succeeded"), None) == "plan_create")

    checks["error_and_current_plan_in_replanning_input"] = False
    checks["completed_work_and_evidence_preserved"] = False
    checks["completed_item_values_preserved"] = False
    checks["validation_replaced_with_pending_review"] = False
    checks["error_reference_and_reason_recorded"] = False
    checks["next_decision_uses_revised_plan"] = False
    if revised:
        event = revised[0]
        payload = event.payload
        before = restored.artifact(payload["before_artifact_id"]).data
        after = restored.artifact(payload["after_artifact_id"]).data
        prior_items = {i["item_id"]: i for i in before["items"]}
        next_items = {i["item_id"]: i for i in after["items"]}
        request = requests[payload["llm_call_id"]].data
        checks["error_and_current_plan_in_replanning_input"] = bool(failed and failed[0].sequence < event.sequence
            and plan_in(request["messages"]) == before
            and any(m["role"] == "tool" and "fixture:runtime:missing" in m.get("content", "")
                    for m in request["messages"]))
        checks["completed_work_and_evidence_preserved"] = all(
            key in prior_items and prior_items[key] == next_items.get(key)
            and prior_items[key]["status"] == "completed"
            and ref in prior_items[key]["source_refs"]
            for key, ref in (("research", "fixture:source:v0"), ("edit", "fixture:source:v1")))
        checks["completed_item_values_preserved"] = all(
            key in prior_items and prior_items[key] == next_items.get(key)
            and prior_items[key]["status"] == "completed" for key in ("research", "edit"))
        checks["validation_replaced_with_pending_review"] = (
            "verify" in prior_items and "verify" not in next_items and "review" in next_items
            and next_items["review"]["status"] == "pending" and next_items["review"]["depends_on"] == ["edit"])
        checks["error_reference_and_reason_recorded"] = (
            bool(payload["reason"].strip()) and "fixture:runtime:missing" in payload["source_refs"])
        later_inputs = [e for e in trace.events if e.type == "llm_common_request" and e.sequence > event.sequence]
        if later_inputs:
            messages = restored.artifact(later_inputs[0].payload["artifact_id"]).data["messages"]
            at_input = restored.state(at_sequence=later_inputs[0].sequence).task.plan_artifact_id
            checks["next_decision_uses_revised_plan"] = (
                plan_in(messages) == restored.artifact(at_input).data
                and plan_in(messages)["revision"] >= after["revision"])

    historical = [e for e in tools if e.payload["name"] == "plan_read" and e.payload["status"] == "succeeded"]
    checks["original_revision_read_through_tool"] = any(
        restored.artifact(restored.artifact(e.payload["result_ref"]["artifact_id"]).data["raw_result_artifact_id"])
        .data["output"]["plan"] == restored.artifact(created[0].payload["after_artifact_id"]).data
        for e in historical) if created else False
    try:
        output = json.loads(result.output)
    except (ValueError, TypeError):
        output = {}
    checks["final_output_distinguishes_unexecuted_tests"] = bool(isinstance(output, dict)
        and output.get("runtime_tests") == "not_run" and output.get("static_review") == "passed"
        and isinstance(output.get("limitations"), str) and output["limitations"].strip())
    final_items = {i.item_id: i for i in result.plan.items} if result.plan else {}
    checks["final_review_links_actual_static_evidence"] = bool("review" in final_items
        and final_items["review"].status.value == "completed"
        and "fixture:static-review:v1" in final_items["review"].source_refs
        and any(op.get("output", {}).get("static_review") == "passed" for op in fixture.operations))
    llm = [e.payload for e in trace.events if e.type == "llm_call_finished"]
    checks["all_five_llm_observation_boundaries"] = bool(llm) and all(
        set(call["artifacts"]) == {"common_request", "adapter_request", "adapter_response", "common_response", "normalized_response"}
        and call["response_received"] and call["validation_succeeded"] for call in llm)
    checks["completion_updates_read_actual_tool_evidence"] = all(
        any(e.type in PLAN_EVENTS and
            any(i["item_id"] == key and i["status"] == "completed" and ref in i["source_refs"]
                for i in restored.artifact(e.payload["after_artifact_id"]).data["items"])
            and any(m["role"] == "tool" and m.get("name") == name and ref in m.get("content", "")
                    for m in requests[e.payload["llm_call_id"]].data["messages"])
            for e in changes)
        for key, name, ref in (("research", "inspect_fixture", "fixture:source:v0"),
                              ("edit", "apply_fixture_fix", "fixture:source:v1"),
                              ("review", "static_review_fixture", "fixture:static-review:v1")))
    return checks


async def verify(model, output_dir, max_tokens, num_ctx, instruction):
    fixture = Fixture()
    options = ChatOptions(temperature=0, max_tokens=max_tokens,
                          extra={"think": True, "options": {"num_ctx": num_ctx}})
    limits = AgentLimits(max_steps=18, max_tool_calls=32, host_response_timeout=10,
                         context_compact_threshold_chars=None)
    started = perf_counter()
    session = await ProblemSolver(OllamaLLM(HOST)).start(
        instruction, model=model, registry=fixture.registry(), options=options, limits=limits)
    live, states, live_errors = [], [], []
    async for event in session.events():
        live.append(event)
        for artifact_id in event.artifact_ids:
            try:
                artifact = await session.artifact(artifact_id)
                if artifact.task_id != session.task_id:
                    live_errors.append(f"wrong task for artifact {artifact_id}")
            except KeyError:
                live_errors.append(f"missing live artifact {artifact_id}")
        if event.type in PLAN_EVENTS or event.type in {"llm_call_started", "tool_started", "finished"}:
            state = await session.state()
            states.append(state)
            if state.task.plan_artifact_id:
                artifact = await session.artifact(state.task.plan_artifact_id)
                if (artifact.data["plan_id"], artifact.data["revision"]) != (state.task.plan_id, state.task.plan_revision):
                    live_errors.append(f"plan mismatch at cursor {state.last_event_sequence}")
        if event.type in PLAN_EVENTS or event.type in {"llm_call_started", "llm_call_finished", "tool_finished", "finished"}:
            print(json.dumps({"model": model, "event": event.type, "sequence": event.sequence,
                "step": event.step, "name": event.payload.get("name"), "status": event.payload.get("status"),
                "revision": event.payload.get("revision"), "error": event.payload.get("result", {}).get("error")}), flush=True)
    result = await session.result()
    elapsed = round(perf_counter() - started, 3)
    stem = model.replace(":", "_").replace(".", "_")
    trace_path = output_dir / f"{stem}.trace.json"
    trace_path.write_text(result.tracer.to_json(indent=2), encoding="utf-8")
    (output_dir / f"{stem}.result.json").write_text(json.dumps({
        "task_id": result.task_id, "status": result.status.value, "output": result.output,
        "stop_reason": result.stop_reason, "warnings": result.warnings,
        "plan": result.plan.to_dict() if result.plan else None, "fixture_operations": fixture.operations,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        checks = assess(result, fixture, live, states, live_errors)
    except Exception as exc:
        checks = {"audit_completed": False}
        live_errors.append(f"Audit error: {type(exc).__name__}: {exc}")
    llm = [e.payload for e in result.tracer.events if e.type == "llm_call_finished"]
    summary = {
        "model": model, "task_id": result.task_id, "status": result.status.value, "stop_reason": result.stop_reason,
        "accepted": bool(checks) and all(checks.values()), "checks": checks, "audit_errors": live_errors,
        "steps": result.steps, "tool_calls": result.tool_calls, "tool_errors": result.tool_errors,
        "revision": result.plan.revision if result.plan else None, "output": result.output,
        "events": len(result.tracer.events), "artifacts": len(result.tracer.artifacts),
        "complete": result.tracer.complete, "details_complete": result.tracer.details_complete,
        "elapsed_seconds": elapsed, "options": asdict(options), "limits": asdict(limits),
        "usage": [call["usage"] for call in llm],
        "disclosed_thinking_chars": [len(a.data["fields"].get("thinking") or "")
            for a in result.tracer.artifacts.values() if a.kind == "llm_common_response"],
        "observed_states": [s.to_dict() for s in states], "trace_file": str(trace_path.resolve()),
    }
    (output_dir / f"{stem}.summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k not in {"usage", "observed_states", "options", "limits"}},
                     ensure_ascii=False), flush=True)
    return summary


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", choices=MODELS, action="append", help="Default: both models, sequentially")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--num-ctx", type=int, default=16384)
    parser.add_argument("--contract-reminders", action="store_true", help="Supplemental explicit field/evidence guidance")
    args = parser.parse_args()
    output_dir = args.output_dir or Path("build/ollama_planning") / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir.mkdir(parents=True, exist_ok=False)
    models = args.model or list(MODELS)
    instruction = INSTRUCTION + (CONTRACT_REMINDERS if args.contract_reminders else "")
    # Read-only local preflight. Never download an absent model.
    with urlopen(HOST + "/api/tags", timeout=10) as response:
        available = json.load(response)["models"]
    with urlopen(HOST + "/api/version", timeout=10) as response:
        server = json.load(response)
    missing = set(models) - {m["name"] for m in available}
    if missing:
        raise RuntimeError(f"Required models are not installed; no download attempted: {sorted(missing)}")
    metadata = {"started_at": datetime.now(timezone.utc).isoformat(), "host": HOST, "server": server,
                "ollama_sdk": version("ollama"), "models": [m for m in available if m["name"] in models],
                "instruction": instruction, "contract_reminders": args.contract_reminders,
                "fixture": "in-memory; intentional unavailable-runtime error"}
    (output_dir / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output_dir": str(output_dir.resolve()), "models": models, "server": server}), flush=True)
    summaries = []
    for model in models:
        summaries.append(await verify(model, output_dir, args.max_tokens, args.num_ctx, instruction))
        (output_dir / "summary.json").write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if all(summary["accepted"] for summary in summaries) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
