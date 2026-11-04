"""Offline fixture and deterministic Provider decisions for the Host example.

Only chat() responses are scripted. All state, tools, communication and results
are produced by shs-agent. The Provider sees data only in actual tool responses.
"""
from __future__ import annotations

import copy
import asyncio
import json

from shs_agent import AgentLimits, SubagentConfig
from shs_agent.llm import LLMResponse, ToolCall
from shs_agent.tool import Tool, ToolExecutor, ToolRegistry, ToolSpec


ROWS = (
    {"id": "T1", "amount": 1200, "status": "settled"},
    {"id": "T2", "amount": 800, "status": "settled"},
    {"id": "T3", "amount": 900, "status": "pending"},
    {"id": "T4", "amount": 500, "status": "settled"},
)
POLICY = {"included_status": "settled", "currency": "KRW"}
ROOT_TASK = "Root"
NARROW_INSTRUCTION = "Only include T1."


class OperationGate:
    """Example/test synchronization for an in-flight handler, never a timeout."""
    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()


def call(name, **arguments):
    return LLMResponse(None, [ToolCall(name, arguments)])


def outputs(request, name):
    values = [json.loads(m["content"]) for m in request["messages"]
              if m["role"] == "tool" and m.get("name") == name]
    if not all(v["ok"] for v in values):
        raise AssertionError(f"unexpected tool error: {name}: {values}")
    return [v["output"] for v in values]


def incoming(request, kind):
    return [json.loads(m["content"].split("\n", 1)[1]) for m in request["messages"]
            if m["role"] == "user" and m.get("content", "").startswith("Agent communication")
            and json.loads(m["content"].split("\n", 1)[1])["kind"] == kind]


def context_data(request, prefix):
    return next((json.loads(m["content"].split("\n", 1)[1]) for m in request["messages"]
                 if m.get("content", "").startswith(prefix)), None)


class WorkflowProvider:
    """A small protocol-driving Fake, not a second Task runtime or evaluator."""

    async def chat(self, **request):
        role = request["messages"][1]["content"]
        response = self.decide(role, request)
        offered = {tool["name"] for tool in request["tools"]}
        assert all(c.name in offered for c in response.tool_calls), (role, response)
        return response

    def decide(self, role, request):
        result = lambda name: outputs(request, name)
        plan = context_data(request, "Current task plan")
        access = context_data(request, "Current tool availability")
        questions, replies = incoming(request, "question"), incoming(request, "reply")
        answered = {r["request_id"] for r in result("agent_reply") if r["accepted"]}
        # Forward only real tool-request hops; ordinary agent questions are separate.
        for message in questions:
            known = next((r for r in access["requests"]
                          if any(h["request_id"] == message["request_id"] for h in r["hops"])), None)
            if known and known["status"] == "requested" and known["hops"][-1]["request_id"] == message["request_id"]:
                return call("tool_forward", request_id=message["request_id"])
        ordinary = [q for q in questions if not q["content"].startswith('{"kind": "tool_request"')]

        if role == "A-1":
            return self.leaf(request, plan, access, replies)
        if role == "B":
            if not result("policy_read"):
                return call("policy_read")
            for question in ordinary:
                if question["request_id"] not in answered:
                    return call("agent_reply", request_id=question["request_id"],
                                message=json.dumps(result("policy_read")[0]))
            if not result("parent_ask"):
                return call("parent_ask", message="policy_ready")
            own = result("parent_ask")[0]["request_id"]
            if not any(r["request_id"] == own for r in replies):
                return call("agent_wait_reply", request_id=own)
            return call("task_complete", output=json.dumps(result("policy_read")[0]))

        if plan is None:
            return call("plan_create", items=[{"item_id": "investigate", "description": "Collect delegated evidence"}],
                        reason="Separate evidence gathering and policy confirmation")
        children = result("subagent_spawn")
        if not children:
            names = ("A", "B") if role == "Root" else ("A-1",)
            return LLMResponse(None, [ToolCall("subagent_spawn", {
                "instruction": name, "context": [], "config_id": name}) for name in names])

        if role == "A":
            for question in ordinary:
                if question["request_id"] in answered:
                    continue
                asked = result("parent_ask")
                if not asked:
                    return call("parent_ask", message="Which status counts?",
                                related_request_id=question["request_id"])
                reply = next((r for r in replies if r["request_id"] == asked[0]["request_id"]), None)
                if reply:
                    return call("agent_reply", request_id=question["request_id"], message=reply["content"])
                return call("agent_wait_reply", request_id=asked[0]["request_id"])
        else:
            for question in ordinary:
                if question["content"] == "policy_ready" or question["request_id"] in answered:
                    continue
                asked = result("subagent_ask")
                if not asked:
                    return call("subagent_ask", task_id=children[1]["task_id"], message="Which status counts?",
                                related_request_id=question["request_id"])
                reply = next((r for r in replies if r["request_id"] == asked[0]["request_id"]), None)
                if reply:
                    return call("agent_reply", request_id=question["request_id"], message=reply["content"])
                return call("agent_wait_reply", request_id=asked[0]["request_id"])
            if result("subagent_ask") and replies:
                for question in ordinary:
                    if question["content"] == "policy_ready" and question["request_id"] not in answered:
                        return call("agent_reply", request_id=question["request_id"], message="Policy received; finish.")
            host_answers = result("host_ask")
            detached_answer = context_data(request, "Host answer:")
            if not host_answers or (isinstance(host_answers[-1], dict) and host_answers[-1].get("pending")
                                    and detached_answer is None):
                return call("host_ask", question="Confirm reporting currency KRW?")

        collected = {r["task_id"]: r for r in result("subagent_result") if r["available"]}
        pending = [child["task_id"] for child in children if child["task_id"] not in collected]
        waits = result("subagent_wait")
        if pending:
            if waits and waits[-1]["reason"] == "terminated":
                return call("subagent_result", task_id=pending[0])
            return call("subagent_wait", task_ids=pending)
        child_result = collected[children[0]["task_id"]]
        output = child_result["output"] if child_result["status"] == "completed" else json.dumps({
            "incomplete_child": child_result["task_id"], "status": child_result["status"]})
        if not result("memo_write"):
            return call("memo_write", content=output, kind="verified_by_child",
                        source_refs=[f"task:{children[0]['task_id']}"])
        if plan["items"][0]["status"] != "completed":
            return call("plan_update", updates=[{"item_id": "investigate", "status": "completed"}],
                        expected_revision=plan["revision"], reason="Collected and reviewed child results")
        return call("task_complete", output=output)

    def leaf(self, request, plan, access, replies):
        result = lambda name: outputs(request, name)
        if plan is None:
            return call("plan_create", items=[{"item_id": "contacts", "description": "Investigate with customer data"}],
                        reason="Initial investigation approach")
        if not result("parent_ask"):
            return call("parent_ask", message="Which status counts?")
        own = result("parent_ask")[0]["request_id"]
        reply = next((r for r in replies if r["request_id"] == own), None)
        if reply is None:
            return call("agent_wait_reply", request_id=own)
        requests = {r["requested_tool"]: r for r in access["requests"]}
        if "customer_lookup" not in requests:
            return call("tool_request", requested_tool="customer_lookup", reason="Investigate customer context",
                        intended_use="Read customer details")
        denied = requests["customer_lookup"]
        if denied["status"] != "denied":
            return call("tool_wait", request_id=denied["request_id"])
        if plan["items"][0]["item_id"] == "contacts":
            return call("plan_revise", items=[{"item_id": "anonymous", "description": "Use anonymous transaction rows"}],
                        expected_revision=plan["revision"], reason="Host denied customer access; use anonymous ledger")
        if "ledger_read" not in requests:
            return call("tool_request", requested_tool="ledger_read", reason="Need anonymous transaction evidence",
                        intended_use="Calculate the count and total")
        approved = requests["ledger_read"]
        if approved["status"] != "applied":
            return call("tool_wait", request_id=approved["request_id"])
        if not result("ledger_read"):
            return call("ledger_read")
        policy = json.loads(reply["content"])
        rows = [r for r in result("ledger_read")[0] if r["status"] == policy["included_status"]]
        if any(m["role"] == "user" and m.get("content") == NARROW_INSTRUCTION for m in request["messages"]):
            if plan["items"][0]["description"] != NARROW_INSTRUCTION:
                return call("plan_revise", items=[{"item_id": "anonymous", "description": NARROW_INSTRUCTION}],
                            expected_revision=plan["revision"], reason="Apply Host instruction to this leaf only")
            rows = [r for r in rows if r["id"] == "T1"]
        answer = {"count": len(rows), "total": sum(r["amount"] for r in rows),
                  "ids": [r["id"] for r in rows], "currency": policy["currency"]}
        text = json.dumps(answer)
        if not result("memo_write"):
            tool_id = next(m["tool_call_id"] for m in request["messages"] if m.get("name") == "ledger_read")
            return call("memo_write", content=text, kind="calculated", source_refs=[f"tool:{tool_id}"])
        if plan["items"][0]["status"] != "completed":
            return call("plan_update", updates=[{"item_id": "anonymous", "status": "completed"}],
                        expected_revision=plan["revision"], reason="Calculated from actual ledger tool result")
        if not result("parent_notify"):
            return call("parent_notify", message=text)
        return call("task_complete", output=text)


def configuration(executions, *, ledger_gate=None):
    """Host owns handlers, their fixture, profiles and permissions."""
    async def ledger_read():
        executions.append("ledger_read")
        if ledger_gate is not None:
            ledger_gate.entered.set()
            await ledger_gate.release.wait()
        return copy.deepcopy(list(ROWS))

    def policy_read():
        executions.append("policy_read")
        return dict(POLICY)

    def customer_lookup():
        executions.append("customer_lookup")
        raise AssertionError("the Host must never grant customer access in this scenario")

    def registry(**handlers):
        value = ToolRegistry()
        for name, handler in handlers.items():
            value.register(Tool(ToolSpec(name, name, {
                "type": "object", "properties": {}, "additionalProperties": False}), handler))
        return value

    empty, policy = registry(), registry(policy_read=policy_read)
    return dict(
        host_tools=registry(ledger_read=ledger_read, customer_lookup=customer_lookup),
        limits=AgentLimits(max_steps=60, max_tool_calls=90, max_total_subagents=3,
                           max_subagent_depth=2, context_compact_threshold_chars=None,
                           host_response_timeout=None),
        subagents={
            "A": SubagentConfig(registry=empty, executor=ToolExecutor(empty),
                                allowed_subagent_config_ids=("A-1",)),
            "A-1": SubagentConfig(registry=empty, executor=ToolExecutor(empty),
                                  allowed_tool_ids=("ledger_read", "customer_lookup")),
            "B": SubagentConfig(registry=policy, executor=ToolExecutor(policy)),
        },
    )
