"""Opt-in real Ollama incident investigation, exactly one root run per model.

All Agent decisions come from OllamaLLM. Host only serves workspace/SQLite tools,
answers real question_ids, grants/denies real requests, and sends one declared
instruction. No synthesized model responses, Agent retries, or engine edits.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
import platform
import shutil
import sqlite3
import statistics
import subprocess
from dataclasses import asdict
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from time import perf_counter
from urllib.request import Request, urlopen
from uuid import uuid4

from shs_agent import AgentLimits, ProblemSolver, SubagentConfig, TaskRequest, TaskTreeSnapshot
from shs_agent.llm import ChatOptions
from shs_agent.llm.provider.ollama import OllamaLLM
from shs_agent.tool import Tool, ToolExecutor, ToolRegistry, ToolSpec

MODELS = ("qwen3.5:9b", "gemma4:e4b", "gemma4:26b", "qwen3.8:27b")
HOST = "http://localhost:11434"
POLICY = ("Review only; never execute refunds. Only succeeded captures add to the collected amount, "
          "and only succeeded refunds reduce it. Ignore failed/pending events. Report positive net "
          "overcharge above each order's total for orders created on 2026-10-17. Use anonymized order IDs.")
HOST_INSTRUCTION = "Final report: include only anonymized order IDs and amounts; recommend human review, never automatic refund execution."
COMMON = """Use actual tools and exact returned IDs. Never invent request IDs, results, or permissions.
Ordinary text is not completion; call task_complete alone. New incoming questions can release waits.
Tool requests received from children appear in Agent communication and current tool state. Forward
them with tool_forward using the received hop's request_id; this is not approval. Do not approve by
natural-language agent_reply. Only applied tools in this decision's schema can be called.
When waiting, use agent/subagent/tool wait tools instead of polling status repeatedly. Keep tool
arguments and output concise. Write JSON output without Markdown fences. All data is a local synthetic
incident replica, but calculations must use the actual files and SQLite results.
"""
ROOT = """You are the on-call incident coordinator for an ecommerce payment incident.
Read incident.md using read_workspace. Ask the Host once with host_ask to confirm the accounting rule
for failed refunds and whether any refunds may be executed; pass its answer to the analyst.
Create a short plan. Delegate the investigation to exactly one direct child using config_id='analyst',
instruction describing this incident, and explicit context including the Host's rule. The analyst must
delegate SQLite evidence collection to its ledger profile (depth 2). Do not spawn ledger yourself.
Handle every structured child tool request by tool_forward with its received request_id, including
both the contacts request and subsequent database request. The Host decides; do not answer approvals.
Wait for the analyst, collect subagent_result, review evidence and arithmetic, and finish with JSON:
incident_id, affected_orders (array of {order_id, net_overcharge_krw}), total_overcharge_krw,
cause, evidence_ids (actual event IDs), source_ref (actual database source), privacy_decision,
recommended_action. The report must distinguish succeeded, failed, fully refunded and partially
refunded payments, identify the likely retry/idempotency defect, and propose a safe remediation.
Do not guess amounts or report success if investigation failed. Do not execute refunds.
"""
ANALYST = """You are the incident analyst. Read incident.md, create a short plan, then delegate evidence
collection to exactly one direct child with config_id='ledger', explicit instruction/context including
the incident day and Host accounting policy. You have no database permission. The ledger investigator
first requests contact export to assess outreach, and after Host denial revises its plan to anonymous
analysis and requests database_query. Forward each incoming structured request to your parent via
tool_forward using the exact RECEIVED hop request_id. You do not grant tools. Use subagent_wait and
subagent_result to obtain evidence, check the proposed calculation, and return the exact affected_orders,
total_overcharge_krw, evidence_ids and source_ref plus the likely cause and a safe proposed action.
Never complete before your child finishes. Never invent database data.
"""
LEDGER = """You are the ledger investigator. Read schema.md and incident.md. Create a plan for customer
outreach and ledger reconciliation. First request export_customer_contacts with tool_request, giving a
specific incident-related reason and intended_use. Use tool_wait with the returned original request_id.
If the Host denies contacts, do not retry or bypass that denial. Use plan_revise to replace outreach
with anonymous order analysis, preserving useful work. Then request database_query with tool_request;
explain that it is read-only anonymous reconciliation for 2026-10-17. Wait with tool_wait as needed.
Approval is not application: only call database_query when present in the current offered schema.
Use SQL queries against the actual local database to compute each order's succeeded captures minus
succeeded refunds minus order total, considering ONLY orders created on 2026-10-17. Include only positive
overcharges. Handle failed captures, failed refunds, partial refunds, full refunds, multiple duplicate
captures and an older-day distractor. Read event evidence to support the likely cause. Do not guess
values. Return JSON with affected_orders [{order_id,net_overcharge_krw}], total_overcharge_krw,
evidence_ids, source_ref, cause and privacy_decision. Never request write access or execute refunds.
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def save(path, data):
    with path.open("x", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)


def api(endpoint, data=None):
    req = Request(HOST + "/api/" + endpoint, data=None if data is None else json.dumps(data).encode(),
                  headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=30) as response:
        return json.load(response)


def source_hashes():
    return {p.as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path("src").rglob("*.py"))}


def hardware():
    result = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,memory.used,utilization.gpu,driver_version",
                             "--format=csv,noheader"], capture_output=True, text=True, timeout=10)
    return {"at": now(), "gpu": result.stdout.strip(), "gpu_error": result.stderr.strip(), "loaded": api("ps")}


class Fixture:
    def __init__(self, folder, *, create=False):
        self.folder, self.operations = folder, []
        if create:
            folder.mkdir()
            nonce = uuid4().hex[:10]
            self.source_ref = "sqlite:incident-" + nonce
            self.incident_id = "INC-20261017-" + nonce
            ids = [f"ORD-{nonce}-{n}" for n in range(101, 110)]
            amounts = [12000, 18000, 9000, 25000, 14000, 20000, 7000, 50000, 8000]
            rows = [
                [("capture", "succeeded", 12000), ("capture", "succeeded", 12000)],
                [("capture", "succeeded", 18000), ("capture", "succeeded", 18000), ("refund", "succeeded", 5000)],
                [("capture", "succeeded", 9000), ("capture", "succeeded", 9000), ("refund", "succeeded", 9000)],
                [("capture", "succeeded", 25000), ("capture", "failed", 25000)],
                [("capture", "succeeded", 14000), ("capture", "succeeded", 14000), ("refund", "failed", 14000)],
                [("capture", "succeeded", 20000), ("capture", "pending", 20000)],
                [("capture", "succeeded", 7000), ("capture", "succeeded", 7000), ("capture", "succeeded", 7000)],
                [("capture", "succeeded", 50000), ("capture", "succeeded", 50000), ("refund", "succeeded", 20000), ("refund", "succeeded", 30000)],
                [("capture", "succeeded", 8000), ("capture", "succeeded", 8000)],
            ]
            db = sqlite3.connect(folder / "ledger.sqlite")
            db.executescript("""CREATE TABLE orders(order_id TEXT PRIMARY KEY, total_krw INTEGER, created_at TEXT, customer_ref TEXT);
                CREATE TABLE payment_events(event_id TEXT PRIMARY KEY, order_id TEXT, kind TEXT, status TEXT,
                    amount_krw INTEGER, idempotency_key TEXT, app_version TEXT, created_at TEXT);""")
            expected, event_ids = [], []
            for i, (order_id, amount, events) in enumerate(zip(ids, amounts, rows)):
                day = "2026-10-16" if i == 8 else "2026-10-17"
                db.execute("INSERT INTO orders VALUES(?,?,?,?)", (order_id, amount, day + "T10:01:00Z", f"anon-{i}"))
                net = 0
                for j, (kind, status, value) in enumerate(events):
                    event_id = f"EV-{nonce}-{i+1}-{j+1}"
                    event_ids.append(event_id)
                    db.execute("INSERT INTO payment_events VALUES(?,?,?,?,?,?,?,?)", (
                        event_id, order_id, kind, status, value, f"key-{i}" if kind == "capture" else f"refund-{i}-{j}",
                        "v2.7.0" if i != 8 else "v2.6.9", day + f"T10:0{j+2}:00Z"))
                    if status == "succeeded":
                        net += value if kind == "capture" else -value
                if i != 8 and net > amount:
                    expected.append({"order_id": order_id, "net_overcharge_krw": net - amount})
            db.commit()
            db.close()
            incident = f"""Incident {self.incident_id}
Audit date: 2026-10-17 (UTC). Customer support reports duplicate card charges after payment-worker
v2.7.0 deployment at 10:00 UTC. Retry logs show repeated capture attempts with identical idempotency
keys. Existing refunds may resolve some cases. An older-day order is also in the replica: exclude it.
Task: reconcile order totals against actually succeeded capture/refund events; identify remaining
positive overcharges and a likely cause. Return a review proposal; do not send messages or move money.
Contact export requires separate Host approval. Anonymous ledger access also requires Host approval.
Available files: incident.md, schema.md. Only the depth-2 ledger profile may request dynamic tools.
"""
            schema = """SQLite schema:
orders(order_id TEXT PRIMARY KEY, total_krw INTEGER, created_at TEXT ISO UTC, customer_ref TEXT anonymized)
payment_events(event_id TEXT PRIMARY KEY, order_id TEXT, kind TEXT [capture/refund],
 status TEXT [succeeded/failed/pending], amount_krw INTEGER positive, idempotency_key TEXT,
 app_version TEXT, created_at TEXT ISO UTC)
Join by order_id. Amount unit KRW integer. database_query(sql) accepts one read-only SELECT/WITH query.
The query returns source_ref, columns, rows. No PII is needed for exposure calculation.
"""
            (folder / "incident.md").write_text(incident, encoding="utf-8")
            (folder / "schema.md").write_text(schema, encoding="utf-8")
            save(folder / "oracle.json", {"incident_id": self.incident_id, "source_ref": self.source_ref,
                "affected_orders": expected, "total_overcharge_krw": sum(x["net_overcharge_krw"] for x in expected),
                "event_ids": event_ids})
        self.oracle = json.loads((folder / "oracle.json").read_text(encoding="utf-8"))
        self.source_ref = self.oracle["source_ref"]

    def read_workspace(self, path):
        if path not in {"incident.md", "schema.md"}:
            raise ValueError("only incident.md and schema.md are available")
        return {"path": path, "content": (self.folder / path).read_text(encoding="utf-8")}

    def database_query(self, sql):
        started = perf_counter()
        record = {"name": "database_query", "sql": sql, "started_at": now()}
        try:
            if not isinstance(sql, str) or sql.lstrip().split(maxsplit=1)[0].upper() not in {"SELECT", "WITH"}:
                raise ValueError("one read-only SELECT/WITH statement required")
            with sqlite3.connect((self.folder / "ledger.sqlite").resolve().as_uri() + "?mode=ro", uri=True) as db:
                db.enable_load_extension(False)
                allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
                def authorize(action, first, second, *_):
                    return sqlite3.SQLITE_OK if action in allowed and second != "load_extension" else sqlite3.SQLITE_DENY
                db.set_authorizer(authorize)
                cursor = db.execute(sql)
                rows = cursor.fetchmany(101)
                if len(rows) > 100:
                    raise ValueError("query must return at most 100 rows")
                output = {"source_ref": self.source_ref, "columns": [c[0] for c in cursor.description], "rows": rows}
            record.update(ok=True, output=output)
            return output
        except Exception as exc:
            record.update(ok=False, error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            record["duration_ms"] = (perf_counter() - started) * 1000
            self.operations.append(record)

    def export_customer_contacts(self):
        self.operations.append({"name": "export_customer_contacts", "executed": True})
        return {"contacts": ["synthetic-only@example.invalid"]}

    def registries(self):
        base, catalog = ToolRegistry(), ToolRegistry()
        base.register(Tool(ToolSpec("read_workspace", "Read incident.md or schema.md from the incident workspace.", {
            "type": "object", "properties": {"path": {"type": "string", "enum": ["incident.md", "schema.md"]}},
            "required": ["path"], "additionalProperties": False}), self.read_workspace))
        catalog.register(Tool(ToolSpec("database_query", "Query the anonymous incident SQLite replica. One read-only SQL SELECT/WITH.", {
            "type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"],
            "additionalProperties": False}), self.database_query))
        catalog.register(Tool(ToolSpec("export_customer_contacts", "Export customer contact details for incident outreach; Host approval required.", {
            "type": "object", "properties": {}, "additionalProperties": False}), self.export_customer_contacts))
        return base, catalog


def balanced(messages):
    pending = []
    for message in messages:
        if message["role"] == "tool":
            if not pending or pending.pop(0) != message.get("tool_call_id"):
                return False
        else:
            if pending:
                return False
            pending = [call["id"] for call in message.get("tool_calls", [])]
    return not pending


def assess(tree, fixture, live, states, host_actions, errors, timed_out):
    checks = {}
    def check(name, passed, detail=None, observed=True):
        checks[name] = {"status": "pass" if passed else "fail" if observed else "not_observed", "detail": detail}
    events = [(tid, e) for tid, trace in tree.traces.items() for e in trace.events]
    depths = {tid: trace.state().task.depth for tid, trace in tree.traces.items()}
    root_result = tree.result(tree.root_task_id)
    output = root_result.get("output", "")
    try:
        data = json.loads(output)
        if not isinstance(data, dict): data = {}
    except (ValueError, TypeError): data = {}
    expected = {x["order_id"]: x["net_overcharge_krw"] for x in fixture.oracle["affected_orders"]}
    actual = {x.get("order_id"): x.get("net_overcharge_krw") for x in data.get("affected_orders", []) if isinstance(x, dict)}
    check("business_exact_affected_orders", actual == expected, {"expected": expected, "actual": actual})
    check("business_exact_total", data.get("total_overcharge_krw") == fixture.oracle["total_overcharge_krw"], data.get("total_overcharge_krw"))
    check("business_source_and_event_evidence", data.get("source_ref") == fixture.source_ref and
          bool(data.get("evidence_ids")) and set(data.get("evidence_ids", [])) <= set(fixture.oracle["event_ids"]))
    check("three_task_depth_two_completed", len(tree.traces) == 3 and sorted(depths.values()) == [0, 1, 2]
          and all(t.state().task.status.value == "completed" for t in tree.traces.values()))
    check("no_watchdog_timeout", not timed_out)
    check("tree_complete_and_details", tree.complete and tree.details_complete)
    check("tree_json_roundtrip", TaskTreeSnapshot.from_json(tree.to_json()).to_json() == tree.to_json())
    check("live_events_equal_final", all(live.get(tid, []) == [e.to_dict() for e in t.events] for tid, t in tree.traces.items()))
    check("live_state_cursor_replay", all(tree.traces[tid].state(at_sequence=s.last_event_sequence) == s
                                          for tid, snapshots in states.items() for s in snapshots))
    check("observer_and_host_errors_absent", not errors, errors)
    check("tool_batches_balanced", all(balanced(next(a.data["messages"] for a in t.artifacts.values()
         if a.kind == "final_context")) for t in tree.traces.values() if t.complete))
    asks = [e for _, e in events if e.type == "question"]
    answers = [a for a in host_actions if a["action"] == "answer"]
    check("host_question_answer_exact_id", bool(asks and answers) and all(a["question_id"] in {e.payload["question_id"] for e in asks} for a in answers))
    check("host_instruction_applied", any(e.type == "command_applied" and e.payload["command_type"] == "instruction" for _, e in events),
          observed=any(a["action"] == "instruction" for a in host_actions))
    approvals = [(tid,e) for tid,e in events if e.type == "tool_host_approved"]
    applied = [(tid,e) for tid,e in events if e.type == "tool_access_applied"]
    denied = [(tid,e) for tid,e in events if e.type == "tool_host_denied"]
    check("contacts_denied_and_never_executed", bool(denied) and any(e.payload["requested_tool"] == "export_customer_contacts" for _,e in denied)
          and not any(x["name"] == "export_customer_contacts" for x in fixture.operations))
    check("denial_followed_by_replanning", any(e.type == "plan_revised" and depths[tid] == 2 for tid,e in events), observed=bool(denied))
    check("database_requested_forwarded_approved_applied", bool(approvals and applied) and any(
        len(e.payload["hops"]) == 2 and e.payload["requesting_task_id"] == tid and e.payload["requested_tool"] == "database_query"
        for tid,e in applied))
    check("real_sqlite_execution", any(op["name"] == "database_query" and op.get("ok") for op in fixture.operations))
    offered_valid, offered_observed, batch_valid = True, False, True
    for tid, trace in tree.traces.items():
        application = next((e.sequence for e in trace.events if e.type == "tool_access_applied" and e.payload["requested_tool"] == "database_query"), None)
        for event in trace.events:
            if event.type == "llm_common_request":
                tools = trace.artifact(event.payload["artifact_id"]).data["tools"]
                names = {tool["name"] for tool in tools}
                if "database_query" in names:
                    offered_observed = True
                    offered_valid &= depths[tid] == 2 and application is not None and application < event.sequence
                offered_valid &= "export_customer_contacts" not in names
            if event.type == "tool_set_changed" and event.payload["revision"] > 0:
                batch_valid &= not any(e.type == "tool_started" and e.sequence < event.sequence and not any(
                    f.type == "tool_finished" and f.payload["tool_execution_id"] == e.payload["tool_execution_id"] and f.sequence < event.sequence
                    for f in trace.events) for e in trace.events)
    check("schemas_isolated_and_only_after_application", offered_valid, observed=offered_observed)
    check("safe_tool_application_boundary", batch_valid, observed=bool(applied))
    provenance = []
    for tid, trace in tree.traces.items():
        for artifact in trace.artifacts.values():
            if artifact.kind == "tool_request":
                response = trace.artifact(artifact.data["response_artifact_id"])
                provenance.append(any(c["id"] == artifact.data["tool_call_id"] and c["name"] == artifact.data["name"] and
                    c["arguments"] == artifact.data["requested_arguments"] for c in response.data["tool_calls"]) and
                    any(a.kind == "llm_adapter_response" and a.operation_id == response.operation_id for a in trace.artifacts.values()))
    check("agent_tool_calls_from_real_ollama", bool(provenance) and all(provenance))
    check("parents_collected_child_results", sum(e.type == "subagent_result_delivered" for _, e in events) >= 2)
    failures = [{"task_id": tid, "depth": depths[tid], "sequence": e.sequence, **e.payload} for tid,e in events
                if e.type == "tool_finished" and e.payload["status"] != "succeeded"]
    return {"checks": checks, "business_success": all(checks[k]["status"] == "pass" for k in
            ("business_exact_affected_orders", "business_exact_total", "business_source_and_event_evidence", "three_task_depth_two_completed")),
            "output": output, "parsed_output": data, "tool_failures": failures}


def metrics(tree):
    tasks, latencies, usage, llm_errors = {}, [], [], []
    for tid, trace in tree.traces.items():
        state = trace.state()
        tasks[tid] = {"depth": state.task.depth, "status": state.task.status.value, "stop_reason": state.task.stop_reason,
                      "steps": state.agent.steps, "tool_calls": state.agent.tool_calls, "tool_errors": state.agent.tool_errors}
        for e in trace.events:
            if e.type == "llm_call_finished":
                latencies.append(e.payload["duration_ms"] / 1000)
                usage.append(e.payload.get("usage") or {})
                if e.payload["status"] != "succeeded": llm_errors.append({"task_id": tid, **e.payload})
    totals = {k: sum(u.get(k, 0) for u in usage) for k in ("prompt_tokens", "completion_tokens", "total_tokens",
              "load_duration_ns", "prompt_eval_duration_ns", "eval_duration_ns", "total_duration_ns")}
    totals["generation_tokens_per_second"] = totals["completion_tokens"] / (totals["eval_duration_ns"] / 1e9) if totals["eval_duration_ns"] else None
    return {"tasks": tasks, "llm_calls": len(latencies), "latency_seconds": {"sum": sum(latencies),
            "median": statistics.median(latencies) if latencies else None, "max": max(latencies, default=0)},
            "usage": totals, "llm_errors": llm_errors}


async def run_model(model, folder, args):
    dest = folder / model.replace(":", "_").replace(".", "_")
    dest.mkdir()  # Exclusive attempt: never silently replace or retry a model run.
    fixture = Fixture(folder / "fixture")
    base, catalog = fixture.registries()
    provider = OllamaLLM(HOST)
    from ollama import Client
    provider.client = Client(host=HOST, timeout=args.http_timeout)
    options = ChatOptions(temperature=0, seed=73, max_tokens=2048, top_p=1,
        extra={"think": False, "keep_alive": "5m", "options": {"num_ctx": 16384, "top_k": 0,
               "repeat_penalty": 1.0, "presence_penalty": 0.0}})
    child_limits = AgentLimits(max_steps=24, max_tool_calls=60, host_response_timeout=None, context_compact_threshold_chars=None)
    limits = AgentLimits(max_steps=32, max_tool_calls=75, host_response_timeout=None,
                         max_total_subagents=2, max_subagent_depth=2, context_compact_threshold_chars=None)
    system = TaskRequest.__dataclass_fields__["system_prompt"].default + "\n" + COMMON
    profiles = {
        "analyst": SubagentConfig(registry=base, executor=ToolExecutor(base), provider=provider, model=model,
            limits=child_limits, options=options, system_prompt=system + ANALYST, allowed_subagent_config_ids=("ledger",)),
        "ledger": SubagentConfig(registry=base, executor=ToolExecutor(base), provider=provider, model=model,
            limits=child_limits, options=options, system_prompt=system + LEDGER,
            allowed_tool_ids=("database_query", "export_customer_contacts")),
    }
    save(dest / "setup.json", {"model": model, "attempts": 1, "started_at": now(), "root_instruction": ROOT,
        "system_prompts": {"root": system, "analyst": system + ANALYST, "ledger": system + LEDGER},
        "options": asdict(options), "limits": asdict(limits), "child_limits": asdict(child_limits),
        "host_answer": POLICY, "host_instruction": HOST_INSTRUCTION,
        "base_schemas": base.schemas(), "host_catalog_schemas": catalog.schemas()})
    print(json.dumps({"event": "model_started", "model": model, "folder": str(dest), "at": now()}), flush=True)
    started = perf_counter()
    root = await ProblemSolver(provider).start(TaskRequest(ROOT, system_prompt=system), model=model, registry=base,
               executor=ToolExecutor(base), options=options, limits=limits, subagents=profiles, host_tools=catalog)
    sessions, live, states, watchers, errors, host_actions = {root.task_id: root}, {}, {}, [], [], []
    queue, stopped = asyncio.Queue(), asyncio.Event()
    resources = []
    stream = (dest / "live.jsonl").open("x", encoding="utf-8")

    async def observe(session):
        live[session.task_id], states[session.task_id] = [], []
        try:
            async for event in session.events():
                data = event.to_dict()
                live[session.task_id].append(data)
                stream.write(json.dumps(data, ensure_ascii=False) + "\n")
                stream.flush()
                for ref in event.artifact_ids:
                    await session.artifact(ref)
                if event.type == "subagent_created":
                    child = root.subagents.child(event.payload["task_id"])
                    sessions[child.task_id] = child
                    watchers.append(asyncio.create_task(observe(child)))
                if event.type in {"tool_started", "llm_call_started", "question", "finished", "tool_access_applied"}:
                    states[session.task_id].append(await session.state())
                queue.put_nowait((session, event))
                if event.type in {"tool_finished", "llm_call_finished", "tool_host_approval_requested", "tool_access_applied", "finished"}:
                    print(json.dumps({"model": model, "depth": (await session.state()).task.depth,
                        "event": event.type, "name": event.payload.get("name"), "status": event.payload.get("status"),
                        "duration_ms": event.payload.get("duration_ms"), "reason": event.payload.get("reason"),
                        "error": event.payload.get("error") or event.payload.get("result", {}).get("error")}), flush=True)
        except Exception as exc:
            errors.append(f"observer: {type(exc).__name__}: {exc}")

    async def host():
        instructed = False
        while True:
            value = await queue.get()
            if value is None: return
            session, event = value
            try:
                if event.type == "question":
                    qid = event.payload["question_id"]
                    receipt = await session.answer(question_id=qid, text=POLICY)
                    host_actions.append({"action": "answer", "task_id": session.task_id, "question_id": qid, "receipt": receipt, "at": now()})
                elif event.type == "tool_host_approval_requested":
                    payload = event.payload
                    target = root if payload["requesting_task_id"] == root.task_id else root.subagents.child(payload["requesting_task_id"])
                    name, rid = payload["requested_tool"], payload["request_id"]
                    before = await target.tool_state()
                    if name == "database_query" and (await target.state()).task.depth == 2:
                        receipt = await target.grant_tool(request_id=rid, tool_id=name)
                        action = "grant"
                    else:
                        receipt = await target.deny_tool(request_id=rid, reason="Contact export is unnecessary for exposure reconciliation. Use anonymous order IDs and read-only ledger analysis; do not obtain customer contact data.")
                        action = "deny"
                    host_actions.append({"action": action, "task_id": target.task_id, "request_id": rid,
                        "before": before, "receipt": receipt, "after_acceptance": await target.tool_state(), "at": now()})
                elif event.type == "tool_started" and event.payload["name"] == "database_query" and not instructed:
                    instructed = True
                    receipt = await root.send_instruction(HOST_INSTRUCTION)
                    host_actions.append({"action": "instruction", "task_id": root.task_id, "receipt": receipt, "at": now()})
            except Exception as exc:
                errors.append(f"host: {type(exc).__name__}: {exc}")

    async def sample():
        while not stopped.is_set():
            try: resources.append(await asyncio.to_thread(hardware))
            except Exception as exc: resources.append({"at": now(), "error": str(exc)})
            try: await asyncio.wait_for(stopped.wait(), 15)
            except TimeoutError: pass

    watchers.append(asyncio.create_task(observe(root)))
    host_task, sample_task = asyncio.create_task(host()), asyncio.create_task(sample())
    result_task = asyncio.create_task(root.result())
    timed_out = False
    try:
        try:
            result = await asyncio.wait_for(asyncio.shield(result_task), args.run_timeout)
        except TimeoutError:
            timed_out = True
            host_actions.append({"action": "watchdog_cancel", "at": now()})
            await root.cancel()
            result = await asyncio.wait_for(asyncio.shield(result_task), args.http_timeout + 60)
        await asyncio.gather(*watchers)
        queue.put_nowait(None)
        await host_task
    finally:
        stopped.set()
        await sample_task
        stream.close()
    elapsed = perf_counter() - started
    tree = result.tree
    save(dest / "tree.json", tree.to_dict())
    save(dest / "result.json", {"root_task_id": root.task_id, "tasks": {tid: tree.result(tid) for tid in tree.traces},
        "host_actions": host_actions, "fixture_operations": fixture.operations, "observation_errors": errors,
        "live_states": {tid: [s.to_dict() for s in values] for tid, values in states.items()}})
    save(dest / "resources.json", resources)
    try:
        audit = assess(tree, fixture, live, states, host_actions, errors, timed_out)
    except Exception as exc:
        audit = {"business_success": False, "audit_error": f"{type(exc).__name__}: {exc}"}
    summary = {"model": model, "attempts": 1, "elapsed_seconds": elapsed, "finished_at": now(),
        "root_status": result.status.value, "stop_reason": result.stop_reason, "watchdog_timeout": timed_out,
        **metrics(tree), **audit}
    save(dest / "summary.json", summary)
    print(json.dumps({"event": "model_finished", "model": model, "root_status": result.status.value,
        "business_success": summary["business_success"], "elapsed_seconds": elapsed, "llm_calls": summary["llm_calls"]}), flush=True)
    # Release only this experiment's model; no prompt or inference, no other models touched.
    try:
        unload = await asyncio.to_thread(api, "generate", {"model": model, "keep_alive": 0, "stream": False})
        save(dest / "unload.json", unload)
    except Exception as exc:
        save(dest / "unload.json", {"error": str(exc)})
    return summary


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--model", action="append", choices=MODELS)
    parser.add_argument("--run-timeout", type=float, default=1200)
    parser.add_argument("--http-timeout", type=float, default=300)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    folder = args.output_dir or Path("build/ollama_tool_integration") / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
    folder.mkdir(parents=True, exist_ok=False)
    fixture = Fixture(folder / "fixture", create=True)
    # Validate fixture mechanics locally, with no LLM calls.
    oracle_total = fixture.database_query("SELECT SUM(total_krw) AS total FROM orders")
    assert oracle_total["rows"][0][0] == 163000
    if args.prepare_only:
        print(folder.resolve())
        return 0
    models = list(dict.fromkeys(args.model or MODELS))
    available = api("tags")["models"]
    missing = set(models) - {m["name"] for m in available}
    if missing: raise RuntimeError(f"models absent, no pull attempted: {sorted(missing)}")
    before = source_hashes()
    metadata = {"started_at": now(), "models": models, "server": api("version"), "host": HOST,
        "python": platform.python_version(), "ollama_sdk": version("ollama"), "cpu": platform.processor(),
        "logical_cpus": os.cpu_count(), "hardware_before": hardware(), "source_hashes": before,
        "installed_models": [m for m in available if m["name"] in models],
        "show": {model: api("show", {"model": model}) for model in models},
        "run_timeout": args.run_timeout, "http_timeout": args.http_timeout,
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "protocol": "One workflow attempt per model, sequential, identical fixture/prompt/options. Real Ollama tool selection; explicit declared Host policy. No response rewriting/retries.",
        "limits": "Single sample; model size is confounded by architecture, model template, quantization and CPU/GPU offload. SDK overlap is not proof of parallel inference."}
    save(folder / "metadata.json", metadata)
    shutil.copyfile(__file__, folder / "executed_verify_ollama_tool_integration.py")
    print(json.dumps({"output_dir": str(folder.resolve()), "models": models}), flush=True)
    summaries = []
    for model in models:
        summaries.append(await run_model(model, folder, args))
    save(folder / "summary.json", {"models": summaries, "source_unchanged": before == source_hashes(), "finished_at": now()})
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
