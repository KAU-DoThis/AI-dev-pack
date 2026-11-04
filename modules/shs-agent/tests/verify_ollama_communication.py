"""Opt-in, single-attempt real Ollama communication audit; never edits the engine.

PYTHONPATH=src python tests/verify_ollama_communication.py
Only B can read the randomized synthetic contract. Every Agent tool call comes
from the existing OllamaLLM provider. Host observers only record data/open gates;
they never spawn, ask, reply, or inject instructions on the Agents' behalf.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import platform
from dataclasses import asdict
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from time import perf_counter
from urllib.request import urlopen
from uuid import uuid4

from shs_agent import AgentLimits, ProblemSolver, SubagentConfig, TaskRequest, TaskTreeSnapshot
from shs_agent.llm import ChatOptions
from shs_agent.llm.provider.ollama import OllamaLLM
from shs_agent.tool import Tool, ToolExecutor, ToolRegistry, ToolSpec

HOST = "http://localhost:11434"
MODELS = ("gemma4:e4b", "qwen3.5:9b")
ROOT_PROMPT = """Run a real parent/child communication exercise for a login feature.
Do the work with tools, not a description of what you would do. Do not ask the Host.
The backend contract is unknown to you and A. Only the backend profile B has the
read_backend_contract tool. It returns synthetic test data, not real credentials.

First create exactly two direct children using subagent_spawn, preferably in one
response: B first with config_id='backend', then A with config_id='frontend'. Include
context=[] and a substantive instruction describing the delegated work:
- B must investigate the contract with read_backend_contract immediately, answer a
  parent question using the tool's exact contract, then finish its original backend
  work: a field/type table and a validation checklist. The read is intentionally
  gated until the parent's question arrives; this is normal, not a failure.
- A must request the login API contract with parent_ask, wait on the returned request_id
  if needed, then produce frontend handling pseudocode using the exact received fields.
  A must not guess the contract or ask the Host.

After creating both, you may subagent_wait on their actual returned task_ids. If a
new child question interrupts a wait, process it; the children are not finished.
When A's question qA arrives, ask B via subagent_ask with B's actual task_id and
message requesting the complete contract. Use related_request_id=qA. Its returned
request_id qB is a NEW independent request. Use agent_wait_reply(qB) if necessary.
Read B's reply, then agent_reply(request_id=qA, message=<the complete exact contract
received from B>). Do not answer A early from guesses or substitute qA for qB.
New incoming messages appear with kind/from_task_id/request_id in your context.
For any incoming question, answer it before resuming your own pending wait.

After forwarding the actual contract to A, collect both child outputs with
subagent_wait and subagent_result for EACH task_id. Inspect their work and write
your own concise memo. Finally call task_complete alone with JSON text containing
contract (the exact contract object), frontend_review and backend_review.
Do not claim completion from spawn alone. Tool IDs and request IDs must be copied
from actual tool results/messages, never invented. Keep responses concise.
"""
FRONTEND_PROMPT = """You are Agent A, responsible for frontend login handling.
You have no backend reader or contract. Ask your actual parent with parent_ask for
the complete login contract, including its runtime-specific fields and source_ref.
Use the returned request_id with agent_wait_reply when needed. If that wait releases
for another incoming question, answer it and resume waiting for your original ID.
Once you receive the actual contract, continue your original task: write frontend
pseudocode that reads all response fields, including the unpredictable ui_hint_*
field by its exact name, and display its exact synthetic value. Write a concise
task-local memo. Complete with task_complete alone, output JSON text with role='A',
contract=<exact received contract object>, frontend_pseudocode=<your implementation>.
Never guess unknown fields. Do not call unavailable tools or use host_ask.
"""
BACKEND_PROMPT = """You are Agent B, responsible for backend contract investigation.
Immediately call read_backend_contract to obtain the real fixture for this run.
The tool may wait until a parent question arrives. Do not skip the read.
When a parent question is in your context, use agent_reply with that exact received
request_id and message containing the COMPLETE contract JSON returned by the tool.
Answering is NOT task completion. After the accepted reply, continue the original
backend task: create a field/type table and validation checklist from the actual
contract, and write a concise local memo. Then call task_complete alone, with output
JSON containing role='B', contract=<exact tool contract>, field_types and validation_checklist.
If the read finishes before a question appears, continue inspecting the contract;
do not invent a request ID or complete without answering a received parent question.
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def save(path, value):
    # Each primary artifact is written once in a new, exclusive run directory.
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)


def get_api(endpoint):
    with urlopen(HOST + "/api/" + endpoint, timeout=10) as response:
        return json.load(response)


def source_hashes():
    return {p.as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path("src").rglob("*.py"))}


def raw_tools(trace, name):
    result = []
    for event in trace.events:
        if event.type == "tool_finished" and event.payload["name"] == name and event.payload["status"] == "succeeded":
            detail = trace.artifact(event.payload["result_ref"]["artifact_id"])
            if detail.data.get("raw_result_artifact_id"):
                result.append((event, trace.artifact(detail.data["raw_result_artifact_id"]).data["output"]))
    return result


def parsed_output(text):
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def communication_in(messages):
    result = []
    for message in messages:
        if message.get("content", "").startswith("Agent communication"):
            try:
                value = json.loads(message["content"].split("\n", 1)[1])
                result.append((message["role"], value))
            except (ValueError, IndexError):
                pass
    return result


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


class Fixture:
    def __init__(self, gate_timeout):
        nonce = uuid4().hex
        self.field = "ui_hint_" + nonce[:10]
        self.value = "synthetic_" + uuid4().hex[:18]
        self.contract = {
            "source_ref": "fixture:backend-contract:" + nonce,
            "schema_revision": "rev_" + nonce[10:22],
            "endpoint": "/api/login", "method": "POST", "success_status": 200,
            "response_example": {"access_token": "SYNTHETIC_ONLY_NOT_VALID",
                "user": {"id": 17, "display_name": "fixture-user"}, self.field: self.value},
            "field_types": {"access_token": "string", "user": "object", self.field: "string"},
            "frontend_required_fields": ["access_token", "user", self.field],
        }
        self.anchors = [self.field, self.value, self.contract["source_ref"], self.contract["schema_revision"]]
        self.private_markers = {role: "local_note_" + role + "_" + uuid4().hex[:12] for role in ("Root", "A", "B")}
        self.gate_timeout = gate_timeout
        self.release = asyncio.Event()
        self.operations = []
        self.question_received = None
        self.gate_observations = []

    def contains_contract(self, text):
        return all(anchor in (text or "") for anchor in self.anchors)

    async def read_backend_contract(self):
        record = {"entered_at": now(), "entered_monotonic": perf_counter(), "completed": False,
                  "cancelled": False, "release_was_open": self.release.is_set()}
        self.operations.append(record)
        try:
            await asyncio.wait_for(self.release.wait(), self.gate_timeout)
            record["output"] = copy.deepcopy(self.contract)
            record["completed"] = True
            return copy.deepcopy(self.contract)
        except TimeoutError:
            record["error"] = "test gate timeout: no parent question received by B"
            raise RuntimeError(record["error"])
        except asyncio.CancelledError:
            record["cancelled"] = True
            raise
        finally:
            record["returned_at"] = now()
            record["returned_monotonic"] = perf_counter()

    def registry(self):
        registry = ToolRegistry()
        registry.register(Tool(ToolSpec("read_backend_contract",
            "Read this run's synthetic login API contract. Only backend B has this tool. "
            "The test read can wait until a parent question arrives; its result is the authoritative fixture.",
            {"type": "object", "properties": {}, "additionalProperties": False}), self.read_backend_contract))
        return registry


def audit(tree, fixture, roles, live, states, observation_errors, timed_out):
    checks = {}
    def check(name, passed, detail="", *, optional=False):
        checks[name] = {"status": "pass" if passed else "not_observed" if optional else "fail", "detail": detail}
    root_id = tree.root_task_id
    root = tree.traces[root_id]
    aid, bid = roles.get("A"), roles.get("B")
    a, b = tree.traces.get(aid), tree.traces.get(bid)
    check("root_A_B_completed", len(tree.traces) == 3 and bool(a and b) and all(
        t.state().task.status.value == "completed" for t in tree.traces.values()))
    check("no_watchdog_timeout", not timed_out)
    check("tree_complete", tree.complete)
    check("tree_details_complete", tree.details_complete)
    try:
        restored = TaskTreeSnapshot.from_json(tree.to_json())
        check("json_roundtrip_and_cross_task_refs", tree.to_json() == restored.to_json())
    except Exception as exc:
        check("json_roundtrip_and_cross_task_refs", False, f"{type(exc).__name__}: {exc}")
    check("live_events_match_final", all([e.to_dict() for e in t.events] == live.get(key) for key, t in tree.traces.items()))
    check("live_artifacts_resolve", not observation_errors, observation_errors)
    check("state_cursors_replay", all(tree.traces[key].state(at_sequence=s.last_event_sequence) == s
        for key, values in states.items() for s in values))
    check("actual_parent_child_state_relationships", bool(a and b) and all(t.state().task.parent_task_id == root_id
        and t.state().task.root_task_id == root_id and t.state().task.depth == 1 for t in (a, b)))

    accepted = {e.payload["message_id"] for t in tree.traces.values() for e in t.events
                if e.type == "agent_message_accepted"}
    timeline = []
    for task_id, trace in tree.traces.items():
        for event in trace.events:
            if event.type == "agent_message_requested":
                artifact = trace.artifact(event.artifact_ids[0])
                data = artifact.data
                timeline.append({"timestamp": event.timestamp, "task_id": task_id, "sequence": event.sequence,
                    "artifact_id": artifact.artifact_id, "accepted": data["message_id"] in accepted, **data})
    timeline.sort(key=lambda item: (item["timestamp"], item["task_id"], item["sequence"]))
    delivered = [m for m in timeline if m["accepted"]]
    qa = [m for m in delivered if m["kind"] == "question" and m["from_task_id"] == aid and m["to_task_id"] == root_id]
    qb = [m for m in delivered if m["kind"] == "question" and m["from_task_id"] == root_id and m["to_task_id"] == bid]
    rb = [m for m in delivered if m["kind"] == "reply" and m["from_task_id"] == bid and m["to_task_id"] == root_id
          and m["request_id"] in {q["request_id"] for q in qb}]
    ra = [m for m in delivered if m["kind"] == "reply" and m["from_task_id"] == root_id and m["to_task_id"] == aid
          and m["request_id"] in {q["request_id"] for q in qa}]
    route = bool(qa and qb and rb and ra)
    check("A_Root_B_Root_A_actual_messages", route)
    check("distinct_real_request_ids", bool(qa and qb) and not ({q["request_id"] for q in qa} & {q["request_id"] for q in qb}))
    check("related_request_id_links_qB_to_qA", any(q.get("related_request_id") in {m["request_id"] for m in qa} for q in qb), optional=True)
    check("B_reply_grounded_in_contract", bool(rb) and all(fixture.contains_contract(m["content"]) for m in rb))
    check("parent_relay_grounded_in_B_reply", bool(rb and ra) and all(fixture.contains_contract(m["content"]) for m in ra)
          and all(any(x["timestamp"] < m["timestamp"] for x in rb) for m in ra))

    provenance = True
    for m in delivered:
        try:
            trace = tree.traces[m["from_task_id"]]
            source = m["source"]
            request = trace.artifact(source["request_artifact_id"]).data
            response = trace.artifact(request["response_artifact_id"]).data
            provenance &= any(c["id"] == source["tool_call_id"] and c["name"] == request["name"]
                and c["arguments"] == request["requested_arguments"] for c in response["tool_calls"])
            provenance &= request["requested_arguments"].get("message") == m["content"]
            provenance &= any(x.kind == "llm_adapter_response" and x.operation_id == source["llm_call_id"]
                              for x in trace.artifacts.values())
        except (KeyError, TypeError):
            provenance = False
    spawn_provenance = []
    for event in root.events:
        if event.type == "subagent_created":
            data = root.artifact(event.payload["artifact_id"]).data
            source = data["source"]
            request = root.artifact(source["request_artifact_id"]).data
            response = root.artifact(request["response_artifact_id"]).data
            spawn_provenance.append(any(c["id"] == source["tool_call_id"] and c["name"] == "subagent_spawn"
                and c["arguments"] == request["requested_arguments"] for c in response["tool_calls"]))
    check("real_LLM_tool_call_provenance", bool(delivered) and provenance and len(spawn_provenance) == 2 and all(spawn_provenance))

    contexts = []
    for m in delivered:
        target = tree.traces[m["to_task_id"]]
        built = [e for e in target.events if e.type == "agent_message_context_built" and e.payload["message_id"] == m["message_id"]]
        common_match, adapter_match = False, False
        for event in built:
            for artifact in target.artifacts.values():
                if artifact.operation_id != event.payload["llm_call_id"]:
                    continue
                if artifact.kind == "llm_common_request":
                    msgs = artifact.data["messages"]
                elif artifact.kind == "llm_adapter_request":
                    msgs = artifact.data["kwargs"]["messages"]
                else:
                    continue
                matched = any(role == "user" and value.get("direction") == "received" and all(
                    value.get(k) == m.get(k) for k in ("message_id", "request_id", "from_task_id", "to_task_id", "kind", "content"))
                    for role, value in communication_in(msgs))
                common_match |= matched and artifact.kind == "llm_common_request"
                adapter_match |= matched and artifact.kind == "llm_adapter_request"
        contexts.append({"message_id": m["message_id"], "kind": m["kind"], "common_request": common_match,
                         "adapter_request": adapter_match, "context_events": [e.sequence for e in built]})
    check("messages_in_actual_common_and_SDK_context", bool(contexts) and all(x["common_request"] and x["adapter_request"] for x in contexts), contexts)
    reads = raw_tools(b, "read_backend_contract") if b else []
    check("B_read_actual_contract", bool(reads) and all(value == fixture.contract for _, value in reads))
    offered = {key: [a.data["tools"] for a in trace.artifacts.values() if a.kind == "llm_common_request"]
               for key, trace in tree.traces.items()}
    check("backend_tool_only_offered_to_B", bool(b) and all(
        ("read_backend_contract" in {s["name"] for s in schemas}) == (key == bid)
        for key, requests in offered.items() for schemas in requests))
    first_contexts = [next((x.data for x in t.artifacts.values() if x.kind == "llm_common_request"), {})
                      for t in (root, a) if t is not None]
    check("Root_A_not_given_fixture_in_advance", len(first_contexts) == 2 and all(
        all(anchor not in json.dumps(x, ensure_ascii=False) for anchor in fixture.anchors) for x in first_contexts))
    originals = {key: next((x.data for x in t.artifacts.values() if x.kind == "final_context"), {})
                 for key, t in tree.traces.items()}
    check("tool_batches_contiguous", all(balanced(x.get("messages", [])) for x in originals.values()))
    check("memo_task_ownership", all(memo["task_id"] == key for key, x in originals.items() for memo in x.get("memos", [])))
    private_memos = {role: any(fixture.private_markers[role] in m["content"] for m in originals.get(key, {}).get("memos", []))
                     for role, key in roles.items()}
    check("each_agent_created_own_private_memo", set(private_memos) == {"Root", "A", "B"} and all(private_memos.values()), private_memos)
    check("child_conversations_not_shared", bool(a and b) and all(
        fixture.private_markers[other] not in json.dumps(originals.get(key, {}).get("messages", []))
        for role, key in (("A", aid), ("B", bid)) for other in ("Root", "B" if role == "A" else "A")))
    root_reads = raw_tools(root, "subagent_result")
    read_cutoff = min((event.sequence for event, _ in root_reads), default=len(root.events) + 1)
    root_before_reads = [x.data for x in root.artifacts.values() if x.kind == "llm_common_request" and
                        any(e.sequence < read_cutoff and x.artifact_id in e.artifact_ids for e in root.events)]
    check("child_private_memos_not_automatically_shared_before_result", all(
        fixture.private_markers[role] not in json.dumps(root_before_reads) for role in ("A", "B")))
    for role, trace in (("Root", root), ("A", a), ("B", b)):
        output = tree.result(trace.task_id).get("output", "") if trace else ""
        check(role + "_final_contract_exact", parsed_output(output).get("contract") == fixture.contract)
    a_output = parsed_output(tree.result(aid).get("output", "")) if a else {}
    check("A_uses_random_field_in_frontend_work", fixture.field in str(a_output.get("frontend_pseudocode", ""))
          and fixture.value in str(a_output.get("frontend_pseudocode", "")))
    check("parent_collected_both_results", bool(a and b) and {value["task_id"] for _, value in root_reads if value.get("available")} == {aid, bid})
    check("parent_final_decision_contains_both_results", any(
        len({json.loads(m["content"])["output"]["task_id"] for m in x.data["messages"] if m["role"] == "tool"
             and m.get("name") == "subagent_result" and json.loads(m["content"]).get("output", {}).get("available")}) == 2
        for x in root.artifacts.values() if x.kind == "llm_common_request"))

    inflight = []
    safe = []
    resumed = []
    if b:
        for q in qb:
            received = next((e for e in b.events if e.type == "agent_message_received" and e.payload["message_id"] == q["message_id"]), None)
            applied = next((e for e in b.events if e.type == "agent_message_applied" and e.payload["message_id"] == q["message_id"]), None)
            for read_event, _ in reads:
                started = next(e for e in b.events if e.type == "tool_started" and e.payload["tool_execution_id"] == read_event.payload["tool_execution_id"])
                if received and started.sequence < received.sequence < read_event.sequence:
                    inflight.append({"request_id": q["request_id"], "tool_started": started.sequence,
                                     "received": received.sequence, "tool_finished": read_event.sequence})
                    safe.append(bool(applied and read_event.sequence < applied.sequence))
        for reply in rb:
            accepted_event = next(e for e in b.events if e.type == "agent_message_accepted" and e.payload["message_id"] == reply["message_id"])
            resumed.append(any(e.type == "tool_started" and e.payload["name"] == "memo_write" and
                               e.sequence > accepted_event.sequence for e in b.events))
    check("question_received_during_B_tool", bool(inflight), inflight, optional=True)
    check("B_tool_not_cancelled_by_question", bool(inflight) and all(op["completed"] and not op["cancelled"] for op in fixture.operations), optional=not bool(inflight))
    check("question_applied_after_B_tool_batch", bool(safe) and all(safe), optional=not bool(inflight))
    check("B_continues_original_work_after_reply", bool(resumed) and all(resumed))

    waits = [{"task_id": key, "event_sequence": e.sequence, "kind": e.type, **trace.artifact(e.artifact_ids[0]).data}
             for key, trace in tree.traces.items() for e in trace.events if e.type in {"subagent_wait_finished", "agent_wait_finished"}]
    root_interrupts = [w for w in waits if w["task_id"] == root_id and w.get("reason") == "incoming_request"]
    check("parent_wait_handles_queued_question", bool(root_interrupts), root_interrupts, optional=True)
    # A queued question can predate wait entry. Only count an in-flight
    # interruption when receipt falls strictly inside the actual wait interval.
    inflight_waits = []
    preserved = []
    for wait in root_interrupts:
        start_kind = wait["kind"].replace("finished", "started")
        start = next((e for e in reversed(root.events) if e.type == start_kind
                      and e.sequence < wait["event_sequence"]), None)
        if start is None:
            continue
        receipts = [e for e in root.events if e.type == "agent_message_received"
                    and e.payload["kind"] == "question"
                    and start.sequence < e.sequence < wait["event_sequence"]]
        if receipts:
            inflight_waits.append({"start": start.sequence, "finish": wait["event_sequence"],
                                   "received": [e.sequence for e in receipts]})
        before = root.state(at_sequence=start.sequence).agent
        after = root.state(at_sequence=wait["event_sequence"]).agent
        preserved.append(set(before.incoming_request_ids) <= set(after.incoming_request_ids)
                         and set(before.outgoing_request_ids) <= set(after.outgoing_request_ids))
    check("parent_wait_interrupted_by_question_received_during_wait", bool(inflight_waits), inflight_waits, optional=True)
    check("parent_existing_requests_preserved_on_wait_release", bool(preserved) and all(preserved), preserved,
          optional=not bool(root_interrupts))
    reply_interrupts = [w for w in waits if w["kind"] == "agent_wait_finished" and w.get("reason") == "incoming_request"]
    resumed_ids = [w["request_id"] for w in reply_interrupts if w.get("pending") and any(
        later["task_id"] == w["task_id"] and later.get("request_id") == w["request_id"] and
        later["event_sequence"] > w["event_sequence"] and later.get("reason") == "answered" for later in waits)]
    check("reply_wait_yields_to_new_question_and_resumes_original", bool(resumed_ids), resumed_ids, optional=True)
    unanswered = [{"request_id": q["request_id"], "from_task_id": q["from_task_id"], "to_task_id": q["to_task_id"]}
                  for q in delivered if q["kind"] == "question" and not any(
                      m["kind"] == "reply" and m["request_id"] == q["request_id"] for m in delivered)]
    check("all_questions_answered", not unanswered, unanswered)
    tool_errors = [{"task_id": key, "sequence": e.sequence, **e.payload} for key, t in tree.traces.items()
                   for e in t.events if e.type == "tool_finished" and e.payload["status"] != "succeeded"]
    check("no_tool_errors", not tool_errors, tool_errors)
    llm_calls = [e for t in tree.traces.values() for e in t.events if e.type == "llm_call_finished"]
    check("all_five_LLM_boundaries", bool(llm_calls) and all(set(e.payload["artifacts"]) == {
        "common_request", "adapter_request", "adapter_response", "common_response", "normalized_response"}
        and e.payload["validation_succeeded"] for e in llm_calls))
    return {"checks": checks, "timeline": timeline, "waits": waits, "unanswered_requests": unanswered,
            "tool_errors": tool_errors, "accepted": all(v["status"] != "fail" for v in checks.values()),
            "core_flow_success": route and bool(rb and ra) and all(fixture.contains_contract(m["content"]) for m in rb + ra)
                and a_output.get("contract") == fixture.contract}


async def verify(model, folder, args):
    fixture = Fixture(args.gate_timeout)
    provider = OllamaLLM(HOST)
    from ollama import Client
    provider.client = Client(host=HOST, timeout=args.http_timeout)
    options = ChatOptions(temperature=0, max_tokens=4096, extra={"think": True, "options": {"num_ctx": 16384}})
    child_limits = AgentLimits(max_steps=18, max_tool_calls=32, host_response_timeout=None, context_compact_threshold_chars=None)
    root_limits = AgentLimits(max_steps=28, max_tool_calls=60, host_response_timeout=None,
                             max_total_subagents=2, max_subagent_depth=1, context_compact_threshold_chars=None)
    def prompt(role, body):
        return TaskRequest.__dataclass_fields__["system_prompt"].default + "\n" + body + (
            "\nWrite a local memo containing your own marker " + fixture.private_markers[role] +
            ". Never put that marker in questions, replies, delegated context/instructions, or final output.")
    frontend_registry, backend_registry = ToolRegistry(), fixture.registry()
    configs = {name: SubagentConfig(provider=provider, model=model, options=options, limits=child_limits,
        registry=registry, executor=ToolExecutor(registry), system_prompt=prompt(role, body))
        for name, role, body, registry in (("frontend", "A", FRONTEND_PROMPT, frontend_registry),
                                          ("backend", "B", BACKEND_PROMPT, backend_registry))}
    stem = model.replace(":", "_").replace(".", "_")
    save(folder / f"{stem}.setup.json", {"model": model, "started_at": now(), "fixture": fixture.contract,
        "private_markers": fixture.private_markers, "root_instruction": ROOT_PROMPT,
        "system_prompts": {"Root": prompt("Root", ""), "A": configs["frontend"].system_prompt, "B": configs["backend"].system_prompt},
        "options": asdict(options), "root_limits": asdict(root_limits), "child_limits": asdict(child_limits),
        "external_schemas": {"Root": [], "A": [], "B": backend_registry.schemas()}})
    started = perf_counter()
    root = await ProblemSolver().start(TaskRequest(ROOT_PROMPT, system_prompt=prompt("Root", "")),
        provider=provider, model=model, options=options, limits=root_limits, subagents=configs)
    roles, live, states, errors, watchers = {"Root": root.task_id}, {}, {}, [], []
    sessions = {root.task_id: root}
    stream = (folder / f"{stem}.live.jsonl").open("x", encoding="utf-8")

    async def observe(session):
        live[session.task_id], states[session.task_id] = [], []
        try:
            async for event in session.events():
                data = event.to_dict()
                live[session.task_id].append(data)
                stream.write(json.dumps(data, ensure_ascii=False) + "\n")
                stream.flush()
                for artifact_id in event.artifact_ids:
                    artifact = await session.artifact(artifact_id)
                    if artifact.task_id != session.task_id:
                        errors.append("wrong task for live artifact " + artifact_id)
                if event.type == "subagent_created":
                    creation = (await session.artifact(event.payload["artifact_id"])).data
                    role = {"frontend": "A", "backend": "B"}[creation["config_id"]]
                    child = root.subagents.child(creation["task_id"])
                    roles[role] = child.task_id
                    sessions[child.task_id] = child
                    watchers.append(asyncio.create_task(observe(child)))
                if event.type in {"llm_call_started", "tool_started", "agent_message_received", "agent_message_applied",
                                  "agent_wait_started", "agent_wait_finished", "subagent_wait_started", "finished"}:
                    states[session.task_id].append(await session.state())
                if session.task_id == roles.get("B") and event.type == "agent_message_received" and event.payload["kind"] == "question":
                    state = await session.state()
                    fixture.question_received = event.to_dict()
                    fixture.gate_observations.append({"observed_at": now(), "event_sequence": event.sequence,
                        "state": state.to_dict(), "active_read": bool(fixture.operations and not fixture.operations[-1]["completed"]
                            and "returned_at" not in fixture.operations[-1]), "release_was_closed": not fixture.release.is_set()})
                    # This only returns the real fixture tool, without fabricating a message/decision.
                    fixture.release.set()
                if event.type in {"subagent_created", "tool_finished", "agent_message_received", "subagent_wait_finished", "agent_wait_finished", "finished"}:
                    print(json.dumps({"model": model, "role": next((r for r, key in roles.items() if key == session.task_id), "?"),
                        "event": event.type, "sequence": event.sequence, "name": event.payload.get("name"),
                        "status": event.payload.get("status"), "request_id": event.payload.get("request_id"),
                        "reason": event.payload.get("reason"), "error": event.payload.get("result", {}).get("error")}), flush=True)
        except Exception as exc:
            errors.append(f"observe {session.task_id}: {type(exc).__name__}: {exc}")

    root_watcher = asyncio.create_task(observe(root))
    result_task = asyncio.create_task(root.result())
    timed_out = False
    result = None
    execution_error = None
    try:
        try:
            result = await asyncio.wait_for(asyncio.shield(result_task), args.run_timeout)
        except TimeoutError:
            timed_out = True
            print(json.dumps({"model": model, "watchdog": "cooperative cancellation requested"}), flush=True)
            fixture.release.set()
            if not root.done:
                await root.cancel()
            result = await asyncio.wait_for(asyncio.shield(result_task), args.http_timeout + 30)
    except Exception as exc:
        execution_error = f"{type(exc).__name__}: {exc}"
        errors.append(execution_error)
    finally:
        fixture.release.set()
        if root.done:
            await root_watcher
            await asyncio.gather(*watchers, return_exceptions=True)
        else:
            for task in (root_watcher, *watchers):
                task.cancel()
            await asyncio.gather(root_watcher, *watchers, return_exceptions=True)
        stream.close()

    # Preserve each original trace before building/auditing the complete tree.
    traces = {key: await session.trace() for key, session in sessions.items()}
    raw_tree = {"schema_version": 1, "scope": "root_and_direct_children", "root_task_id": root.task_id,
                "complete": all(t.complete for t in traces.values()),
                "details_complete": all(t.details_complete for t in traces.values()),
                "traces": {key: trace.to_dict() for key, trace in traces.items()}}
    save(folder / f"{stem}.trace.json", raw_tree)
    task_results = {}
    for key, session in sessions.items():
        if session.done:
            try:
                value = await session.result()
                task_results[key] = {k: v for k, v in asdict(value).items() if k not in {"tracer", "tree"}}
            except Exception as exc:
                task_results[key] = {"result_error": f"{type(exc).__name__}: {exc}", "state": traces[key].state().to_dict()}
        else:
            task_results[key] = {"available": False, "state": traces[key].state().to_dict()}
    save(folder / f"{stem}.result.json", {"root_task_id": root.task_id, "roles": roles, "tasks": task_results,
        "expected_contract": fixture.contract, "fixture_operations": fixture.operations,
        "gate_observations": fixture.gate_observations, "execution_error": execution_error, "watchdog_timeout": timed_out})
    try:
        tree = TaskTreeSnapshot.from_dict(raw_tree)
        assessment = audit(tree, fixture, roles, live, states, errors, timed_out)
        assessment["checks"]["TaskResult_tree_equals_session_traces"] = {
            "status": "pass" if result is not None and result.tree.to_json() == tree.to_json() else "fail", "detail": ""}
        assessment["accepted"] &= assessment["checks"]["TaskResult_tree_equals_session_traces"]["status"] == "pass"
    except Exception as exc:
        assessment = {"accepted": False, "core_flow_success": False, "checks": {
            "audit_completed": {"status": "fail", "detail": f"{type(exc).__name__}: {exc}"}}}
    stats = {}
    for role, key in roles.items():
        trace = traces[key]
        stats[role] = {"task_id": key, "status": trace.state().task.status.value,
            "steps": trace.state().agent.steps, "llm_calls": sum(e.type == "llm_call_started" for e in trace.events),
            "tool_calls": trace.state().agent.tool_calls, "tool_errors": trace.state().agent.tool_errors,
            "model_tool_calls": sum(len(a.data["tool_calls"]) for a in trace.artifacts.values()
                                    if a.kind == "llm_normalized_response"),
            "tool_started": sum(e.type == "tool_started" for e in trace.events),
            "tool_finished": sum(e.type == "tool_finished" for e in trace.events),
            "events": len(trace.events), "artifacts": len(trace.artifacts),
            "pending_incoming": list(trace.state().agent.incoming_request_ids),
            "pending_outgoing": list(trace.state().agent.outgoing_request_ids),
            "thinking_chars": sum(len(a.data["fields"].get("thinking") or "") for a in trace.artifacts.values() if a.kind == "llm_common_response")}
    summary = {"model": model, "attempts": 1, "finished_at": now(), "elapsed_seconds": round(perf_counter() - started, 3),
        "all_sessions_terminated": all(session.done for session in sessions.values()),
        "roles": roles, "task_stats": stats, "watchdog_timeout": timed_out, "execution_error": execution_error,
        "observation_errors": errors, "live_states": {key: [s.to_dict() for s in values] for key, values in states.items()}, **assessment}
    save(folder / f"{stem}.summary.json", summary)
    print(json.dumps({"model": model, "accepted": summary["accepted"], "core_flow_success": summary["core_flow_success"],
        "elapsed_seconds": summary["elapsed_seconds"], "checks": {k: v["status"] for k, v in summary["checks"].items()}}), flush=True)
    return summary


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", choices=MODELS, action="append")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--gate-timeout", type=float, default=180)
    parser.add_argument("--run-timeout", type=float, default=600)
    parser.add_argument("--http-timeout", type=float, default=180)
    args = parser.parse_args()
    models = list(dict.fromkeys(args.model or MODELS))
    folder = args.output_dir or Path("build/ollama_communication") / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
    folder.mkdir(parents=True, exist_ok=False)
    before = source_hashes()
    metadata = {"started_at": now(), "host": HOST, "python": platform.python_version(),
        "models_requested": models, "gate_timeout": args.gate_timeout, "run_timeout": args.run_timeout,
        "http_timeout": args.http_timeout, "source_sha256_before": before,
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "protocol": "Real OllamaLLM chooses all tool calls and arguments; no Host spawn/ask/reply/instruction injection. "
                    "Gate only controls B's fixture-tool return. Sequential independent roots, one attempt per model.",
        "inference_parallelism": "Not measured; overlapping client calls would not prove concurrent inference."}
    try:
        metadata.update(server=get_api("version"), ollama_sdk=version("ollama"))
        available = get_api("tags")["models"]
        metadata["installed_models"] = [m for m in available if m["name"] in models]
    except Exception as exc:
        metadata["environment_error"] = f"{type(exc).__name__}: {exc}"
        available = []
    save(folder / "metadata.json", metadata)
    print(json.dumps({"output_dir": str(folder.resolve()), "models": models}), flush=True)
    summaries = []
    for model in models:
        if any(s.get("all_sessions_terminated") is False for s in summaries):
            summary = {"model": model, "attempts": 0, "accepted": False, "status": "not_verified",
                       "reason": "previous model task did not terminate after watchdog grace; no overlapping model run started"}
            save(folder / (model.replace(":", "_").replace(".", "_") + ".summary.json"), summary)
        elif model not in {m["name"] for m in available}:
            summary = {"model": model, "attempts": 0, "accepted": False, "status": "not_verified",
                       "reason": metadata.get("environment_error", "required model not installed; no pull or substitution")}
            save(folder / (model.replace(":", "_").replace(".", "_") + ".summary.json"), summary)
        else:
            try:
                summary = await verify(model, folder, args)
            except Exception as exc:
                summary = {"model": model, "attempts": 1, "accepted": False, "harness_error": f"{type(exc).__name__}: {exc}"}
                save(folder / (model.replace(":", "_").replace(".", "_") + ".harness_error.json"), summary)
        summaries.append(summary)
    unchanged = source_hashes() == before
    save(folder / "summary.json", {"models": summaries, "source_unchanged": unchanged, "finished_at": now()})
    return 0 if unchanged and all(s["accepted"] for s in summaries) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
