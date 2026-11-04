"""Host-owned catalog, correlated requests and task-local application receipts.

All mutations run on the task's event loop. No handlers or credentials are put
in messages/trace records; only the copied ToolSpec crosses that boundary.
"""
from __future__ import annotations

import copy
import inspect
import json
from dataclasses import asdict, dataclass, field
from uuid import uuid4

from shs_agent.tool import Tool, ToolRegistry
from shs_agent.workspace import Workspace

from ..types import nonempty
from ..state import AgentActivity


class ToolIntegrationError(ValueError):
    """A rejected tool request/Host decision; no capability was granted by it."""


def tool_ids(value):
    if not isinstance(value, (tuple, list)):
        raise TypeError("allowed_tool_ids must be a sequence")
    for name in value:
        nonempty(name, "allowed_tool_id")
    if len(set(value)) != len(value):
        raise ValueError("allowed_tool_ids must be unique")
    return tuple(value)


@dataclass
class ToolRequest:
    request_id: str
    requesting_task_id: str
    requested_tool: str
    reason: str
    intended_use: str
    status: str = "requested"
    host_request_id: str | None = None
    host_request_ref: dict | None = None
    decision_ref: dict | None = None
    decision_reason: str | None = None
    revision: int | None = None
    hops: list[dict] = field(default_factory=list)
    resource_kind: str = "tool"
    workspace_paths: list[str] | None = None
    granted_by: str | None = None
    redelegable: bool = False

    def to_dict(self):
        return asdict(self)


class ToolIntegration:
    """One root's request index; routing itself remains Communication's job."""

    def __init__(self, catalog: ToolRegistry | None, *, enable_tools=False):
        if catalog is not None and not isinstance(catalog, ToolRegistry):
            raise TypeError("host_tools must be a ToolRegistry of definitions and handlers")
        self.tools_enabled = catalog is not None or enable_tools
        self.catalog = catalog if catalog is not None else ToolRegistry()
        self.endpoints = {}
        self.requests = {}
        self.origins = {}
        self.pending = {}
        self.root = None

    def bind(self, channel, allowed, communication, tools):
        allowed = tool_ids(allowed)
        if any(self.catalog.get(name) is None for name in allowed):
            raise ValueError("allowed_tool_ids must identify Host catalog tools")
        endpoint = ToolAccess(self, channel, allowed, communication, tools)
        self.endpoints[channel.task_id] = endpoint
        if channel.task.depth == 0:
            self.root = endpoint
        return endpoint

    def lookup(self, endpoint, request_id):
        origin = self.origins.get(request_id)
        request = self.requests.get(origin)
        if request is None or not any(endpoint.channel.task_id in (hop["from_task_id"], hop["to_task_id"])
                                      for hop in request.hops):
            raise ToolIntegrationError("unknown tool request for this task")
        return request

    def record(self, endpoint, kind, request, *, refs=(), extra=None, tool_ref=None):
        data = {**request.to_dict(), **(extra or {}), "task_refs": list(refs)}
        artifact_id = endpoint.channel.trace_communication(kind, data, tool_ref=tool_ref)
        return {"task_id": endpoint.channel.task_id, "artifact_id": artifact_id}

    def notify(self, request, kind, source):
        # Host decisions target the original Session. This is local Host input,
        # not a new peer-to-peer permission channel or an Agent reply.
        target = self.endpoints[request.requesting_task_id]
        ref = self.record(target, kind, request, refs=(source,))
        target.channel.receive_tool_update(request.to_dict(), ref["artifact_id"])
        self.notify_observers(request, ref)

    def notify_observers(self, request, source):
        participants = {task_id for hop in request.hops for task_id in (hop["from_task_id"], hop["to_task_id"])}
        for task_id in participants - {request.requesting_task_id}:
            endpoint = self.endpoints[task_id]
            if not endpoint.channel.done:
                ref = self.record(endpoint, "tool_access_observed", request, refs=(source,))
                endpoint.channel.receive_tool_update(request.to_dict(), ref["artifact_id"])

    def resolve_hops(self, request):
        """A structured terminal decision also answers its transport questions."""
        for hop in reversed(request.hops):
            endpoint = self.endpoints.get(hop["to_task_id"])
            if endpoint is None or endpoint.channel.done or hop["from_task_id"] == hop["to_task_id"]:
                continue
            received = endpoint.channel.request(hop["request_id"])
            if received.status == "pending":
                endpoint.communication.reply(hop["request_id"], json.dumps({
                    "resource_request_id": request.request_id, "status": request.status,
                    "reason": request.decision_reason, "revision": request.revision}))


class ToolAccess:
    """Bound task identity. Only TaskSession exposes grant/deny to the Host."""

    def __init__(self, owner, channel, allowed, communication, tools):
        self.owner, self.channel = owner, channel
        self.allowed, self.communication, self.tools = allowed, communication, tools

    def _live(self):
        self.channel.check_loop()
        endpoint = self
        while endpoint is not None:
            if endpoint.channel.done or endpoint.channel.task.cancel_requested:
                raise ToolIntegrationError("target task or ancestor is closed or cancelling")
            endpoint = self.owner.endpoints.get(endpoint.channel.task.parent_task_id)

    def request(self, requested_tool, reason, intended_use, *, tool_ref=None,
                resource_kind="tool", workspace_paths=None):
        self._live()
        for name, value in (("requested_tool", requested_tool), ("reason", reason), ("intended_use", intended_use)):
            nonempty(value, name)
        if resource_kind == "tool" and requested_tool in {s["name"] for s in self.tools.snapshot().schemas}:
            raise ToolIntegrationError("tool is already available")
        data = dict(requesting_task_id=self.channel.task_id, requested_tool=requested_tool,
                    reason=reason, intended_use=intended_use,
                    resource_kind=resource_kind, workspace_paths=workspace_paths)
        if self.channel.task.depth:
            receipt = self.communication.ask_parent(json.dumps({"kind": resource_kind + "_request", **data}), tool_ref=tool_ref)
            if not receipt["accepted"]:
                raise ToolIntegrationError(receipt["reason"])
            request_id = receipt["request_id"]
            refs = receipt["task_refs"]
        else:
            request_id, refs = uuid4().hex, []
        request = ToolRequest(request_id=request_id, **data)
        request.hops.append({"request_id": request_id, "from_task_id": self.channel.task_id,
                             "to_task_id": self.channel.task.parent_task_id or self.channel.task_id})
        self.owner.requests[request_id] = request
        self.owner.origins[request_id] = request_id
        ref = self.owner.record(self, "tool_access_requested", request, refs=refs, tool_ref=tool_ref)
        if not self.channel.task.depth:
            self._to_host(request, ref, tool_ref)
        return request.to_dict()

    def _to_host(self, request, ref, tool_ref):
        request.status = "awaiting_host"
        request.host_request_id = uuid4().hex
        request.host_request_ref = self.owner.record(self, "tool_host_approval_requested", request,
                                                     refs=(ref,), tool_ref=tool_ref)

    def forward(self, request_id, *, tool_ref=None):
        self._live()
        request = self.owner.lookup(self, request_id)
        hop = next((h for h in request.hops if h["request_id"] == request_id), None)
        if (request.status != "requested" or hop is None or hop["to_task_id"] != self.channel.task_id
                or hop["from_task_id"] == self.channel.task_id or hop is not request.hops[-1]):
            raise ToolIntegrationError("only the current direct recipient may forward a pending request")
        received = self.channel.request(request_id)
        if received.status != "pending":
            raise ToolIntegrationError("communication request is closed")
        if self.channel.task.depth:
            receipt = self.communication.ask_parent(json.dumps({"kind": request.resource_kind + "_request", **request.to_dict()}),
                                                   related_request_id=request_id, tool_ref=tool_ref)
            if not receipt["accepted"]:
                raise ToolIntegrationError(receipt["reason"])
            forwarded_id = receipt["request_id"]
            request.hops.append({"request_id": forwarded_id, "related_request_id": request_id,
                                 "from_task_id": self.channel.task_id,
                                 "to_task_id": self.channel.task.parent_task_id})
            self.owner.origins[forwarded_id] = request.request_id
            refs = receipt["task_refs"]
        else:
            refs = [received.reference]
        ref = self.owner.record(self, "tool_access_forwarded", request, refs=refs, tool_ref=tool_ref)
        if not self.channel.task.depth:
            self._to_host(request, ref, tool_ref)
        return request.to_dict()

    def state(self):
        self.channel.check_loop()
        return {**self.tools.tool_state(), "requests": [r.to_dict() for r in self.owner.requests.values()
                if any(self.channel.task_id in (h["from_task_id"], h["to_task_id"]) for h in r.hops)]}

    def _decision(self, request_id):
        self._live()
        request = self.owner.lookup(self, request_id)
        if request_id != request.request_id or request.requesting_task_id != self.channel.task_id:
            raise ToolIntegrationError("decision must target the original requesting task and request_id")
        if request.status != "awaiting_host":
            raise ToolIntegrationError("request is not awaiting a Host decision")
        self.owner.root._live()
        return request

    def grant(self, request_id, tool_id):
        request = self._decision(request_id)
        if request.resource_kind != "tool":
            raise ToolIntegrationError("this is not a tool request")
        try:
            if tool_id != request.requested_tool or tool_id not in self.allowed:
                raise ToolIntegrationError("tool does not match request or target's Host allowlist")
            supplied = self.owner.catalog.get(tool_id)
            if supplied is None or not callable(supplied.handler):
                raise ToolIntegrationError("Host catalog has no executable handler")
            tool = Tool(copy.deepcopy(supplied.spec), supplied.handler)
            if tool.spec.name != tool_id:
                raise ToolIntegrationError("catalog identity and definition disagree")
            params = tool.spec.parameters
            if not isinstance(params, dict) or params.get("type") != "object":
                raise ToolIntegrationError("tool parameters must be an object schema")
            properties, required = params.get("properties", {}), params.get("required", [])
            if not isinstance(properties, dict) or not isinstance(required, list) or not set(required) <= set(properties):
                raise ToolIntegrationError("invalid tool properties/required schema")
            # Fail closed for uninspectable handlers; Host can supply a small wrapper.
            signature = inspect.signature(tool.handler)
            signature.bind(**dict.fromkeys(required))
            signature.bind(**dict.fromkeys(properties))
            before = self.tools.validate_addition(tool)
        except Exception as exc:
            self.owner.record(self.owner.root, "tool_grant_rejected", request,
                              refs=(request.host_request_ref,),
                              extra={"error_type": type(exc).__name__, "reason": str(exc)})
            raise ToolIntegrationError(str(exc)) from exc
        request.status = "approved"
        definition = self.channel.add_artifact("tool_definition", asdict(tool.spec))
        ref = self.owner.record(self.owner.root, "tool_host_approved", request,
                               refs=(request.host_request_ref,
                                     {"task_id": self.channel.task_id, "artifact_id": definition}))
        request.decision_ref = ref
        self.owner.pending[request.request_id] = (tool, before, definition)
        self.owner.notify(request, "tool_approval_received", ref)
        return request.to_dict()

    def workspace_request(self, paths, reason, *, tool_ref=None):
        if not isinstance(paths, list) or not paths or not all(isinstance(p, str) and p for p in paths):
            raise ToolIntegrationError("paths must be a nonempty string array")
        return self.request("workspace", reason, reason, resource_kind="workspace",
                            workspace_paths=paths, tool_ref=tool_ref)

    def grant_workspace(self, request_id, workspace):
        request = self._decision(request_id)
        if request.resource_kind != "workspace" or not isinstance(workspace, Workspace):
            raise ToolIntegrationError("workspace grant requires a workspace request and Workspace")
        return self._approve_resource(request, workspace, self.owner.root)

    def supply_tool(self, request_id, tool, *, redelegate=False):
        """Explicit Host authority; unlike grant(), no preinstalled catalog required."""
        request = self._decision(request_id)
        if request.resource_kind != "tool" or not isinstance(tool, Tool) or tool.spec.name != request.requested_tool:
            raise ToolIntegrationError("supply the Tool requested by this task")
        if type(redelegate) is not bool:
            raise ToolIntegrationError("redelegate must be a boolean")
        if not callable(tool.handler):
            raise ToolIntegrationError("tool needs an executable handler")
        params = tool.spec.parameters
        if not isinstance(params, dict) or params.get("type") != "object":
            raise ToolIntegrationError("tool parameters must be an object schema")
        properties, required = params.get("properties", {}), params.get("required", [])
        if not isinstance(properties, dict) or not isinstance(required, list) or not set(required) <= set(properties):
            raise ToolIntegrationError("invalid tool properties/required schema")
        signature = inspect.signature(tool.handler)
        signature.bind(**dict.fromkeys(required))
        signature.bind(**dict.fromkeys(properties))
        return self._approve_resource(request, Tool(copy.deepcopy(tool.spec), tool.handler),
                                      self.owner.root, redelegate=redelegate)

    def provide(self, request_id, *, workspace_paths=None, redelegate=False, tool_ref=None):
        """Called by the actual recipient Agent, within its explicit delegation authority."""
        self._live()
        if type(redelegate) is not bool:
            raise ToolIntegrationError("redelegate must be a boolean")
        request = self.owner.lookup(self, request_id)
        hop = request.hops[-1]
        if request.status != "requested" or hop["to_task_id"] != self.channel.task_id or hop["from_task_id"] == self.channel.task_id:
            raise ToolIntegrationError("only the current parent recipient can provide this resource")
        if self.channel.request(hop["request_id"]).status != "pending":
            raise ToolIntegrationError("request already closed")
        if request.resource_kind == "workspace":
            if self.tools.workspace is None:
                raise ToolIntegrationError("parent has no workspace")
            resource = self.tools.workspace.delegate(workspace_paths or request.workspace_paths, redelegate=redelegate)
        else:
            if request.requested_tool not in self.tools.delegable_tool_ids:
                raise ToolIntegrationError("parent lacks tool delegation authority")
            resource = self.tools.tool(request.requested_tool)
            if resource is None:
                raise ToolIntegrationError("tool is not applied on parent")
            resource = Tool(copy.deepcopy(resource.spec), resource.handler)
        return self._approve_resource(request, resource, self, tool_ref=tool_ref, redelegate=redelegate)

    def _approve_resource(self, request, resource, approver, *, tool_ref=None, redelegate=False):
        target = self.owner.endpoints[request.requesting_task_id]
        target._live()
        before = None if isinstance(resource, Workspace) else target.tools.validate_addition(resource)
        definition = target.channel.add_artifact("workspace_definition" if isinstance(resource, Workspace) else "tool_definition",
                                                resource.snapshot() if isinstance(resource, Workspace) else asdict(resource.spec))
        request.redelegable = redelegate
        request.granted_by = approver.channel.task_id
        request.status = "approved"
        refs = [request.host_request_ref] if request.host_request_ref else [approver.channel.request(request.hops[-1]["request_id"]).reference]
        ref = self.owner.record(approver, "tool_resource_approved", request, refs=refs, tool_ref=tool_ref)
        request.decision_ref = ref
        self.owner.pending[request.request_id] = (resource, before, definition)
        self.owner.notify(request, "tool_approval_received", ref)
        return request.to_dict()

    def deny(self, request_id, reason):
        nonempty(reason, "reason")
        request = self._decision(request_id)
        request.status, request.decision_reason = "denied", reason
        ref = self.owner.record(self.owner.root, "tool_host_denied", request, refs=(request.host_request_ref,))
        request.decision_ref = ref
        self.owner.notify(request, "tool_access_denied", ref)
        self.owner.resolve_hops(request)
        return request.to_dict()

    def reject(self, request_id, reason, *, tool_ref=None):
        self._live()
        nonempty(reason, "reason")
        request = self.owner.lookup(self, request_id)
        hop = request.hops[-1]
        if request.status != "requested" or hop["to_task_id"] != self.channel.task_id or hop["from_task_id"] == self.channel.task_id:
            raise ToolIntegrationError("only the current parent recipient may deny")
        if self.channel.request(hop["request_id"]).status != "pending":
            raise ToolIntegrationError("request already closed")
        request.status, request.decision_reason = "denied", reason
        ref = self.owner.record(self, "tool_resource_denied", request,
                                refs=(self.channel.request(hop["request_id"]).reference,), tool_ref=tool_ref)
        request.decision_ref = ref
        self.owner.notify(request, "tool_access_denied", ref)
        self.owner.resolve_hops(request)
        return request.to_dict()

    def apply_pending(self):
        for request_id, (tool, before, definition) in tuple(self.owner.pending.items()):
            request = self.owner.requests[request_id]
            if request.requesting_task_id != self.channel.task_id:
                continue
            del self.owner.pending[request_id]
            try:
                self._live()
                if isinstance(tool, Workspace):
                    self.tools.apply_workspace(tool)
                    revision = self.tools.workspace.revision
                else:
                    self.tools.apply_addition(tool, before, definition)
                    if request.redelegable:
                        self.tools.delegable_tool_ids = tuple(dict.fromkeys((*self.tools.delegable_tool_ids, tool.spec.name)))
                    revision = self.tools.tool_state()["revision"]
                request.status, request.revision = "applied", revision
                kind = "tool_access_applied"
            except Exception as exc:
                request.status, request.decision_reason = "apply_failed", str(exc)
                kind = "tool_access_apply_failed"
            ref = self.owner.record(self, kind, request,
                                    refs=(request.decision_ref,
                                          {"task_id": self.channel.task_id, "artifact_id": definition}),
                                    extra={"tool_set": self.tools.tool_state()})
            self.channel.receive_tool_update(request.to_dict(), ref["artifact_id"])
            self.owner.notify_observers(request, ref)
            self.owner.resolve_hops(request)

    def close(self):
        for request in self.owner.requests.values():
            if request.requesting_task_id == self.channel.task_id and request.status in {"requested", "awaiting_host", "approved"}:
                request.status = "apply_failed" if request.status == "approved" else "closed"
                request.decision_reason = "task_terminated_before_application"
                self.owner.pending.pop(request.request_id, None)
                ref = self.owner.record(self, "tool_access_" + request.status, request,
                                        refs=(request.decision_ref,) if request.decision_ref else ())
                self.owner.notify_observers(request, ref)

    async def wait(self, request_id, *, tool_ref=None):
        self._live()
        request = self.owner.lookup(self, request_id)
        self.channel.update_state(agent={"activity": AgentActivity.WAITING_FOR_TOOL_APPROVAL,
                                         "waiting_for": "tool_approval", "waiting_request_id": request_id})
        self.owner.record(self, "tool_access_wait_started", request, tool_ref=tool_ref)
        try:
            while True:
                if request.status not in {"requested", "awaiting_host"}:
                    # Approval wakes the batch, so Runtime can reach the apply boundary.
                    return request.to_dict()
                incoming = self.channel.communication_interrupt()
                if incoming is not None:
                    return {**request.to_dict(), "interrupted": incoming}
                if self.channel.has_agent_messages():
                    # A parent's ordinary reply may suggest an alternative. It
                    # must reach the next decision without being a tool grant.
                    return {**request.to_dict(), "interrupted": {"reason": "incoming_reply"}}
                self.channel.prepare_communication_wait()
                await self.channel.wait_communication()
        finally:
            self.channel.update_state(agent={"activity": AgentActivity.TOOL_EXECUTION,
                                             "waiting_for": None, "waiting_request_id": None})
            self.owner.record(self, "tool_access_wait_finished", request, tool_ref=tool_ref)
