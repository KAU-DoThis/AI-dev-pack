"""Task-local tool bindings using the existing Registry and Executor contracts."""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

from shs_agent.tool import Tool, ToolExecutor, ToolRegistry, ToolSpec

from .._async import invoke
from ..context import TaskContext
from ..communication import CommunicationError
from ..planning import Planning
from ..subagent import SubagentError
from ..subagent.scope import SubagentScope
from ..session.channel import TaskChannel
from ..types import AgentLimits, ToolExecutorLike, ToolRegistryLike, integer, nonempty
from .schemas import (CONVERSATION_READ_SCHEMA, CONVERSATION_SEARCH_SCHEMA,
                      HOST_ASK_SCHEMA, MEMO_WRITE_SCHEMA, TASK_COMPLETE_SCHEMA,
                      PLAN_CREATE_SCHEMA, PLAN_READ_SCHEMA, PLAN_UPDATE_SCHEMA, PLAN_REVISE_SCHEMA,
                      SUBAGENT_SCHEMAS, COMMUNICATION_SCHEMAS, TOOL_INTEGRATION_SCHEMAS, RESOURCE_SCHEMAS)


@dataclass(frozen=True)
class ToolSnapshot:
    schemas: list[dict[str, Any]]
    version: int | None
    revision: int = 0


class ToolUnavailable(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


class SessionTools:
    def __init__(self, registry: ToolRegistryLike, executor: ToolExecutorLike,
                 context: TaskContext, channel: TaskChannel, limits: AgentLimits, *,
                 planning: Planning | None = None, subagents: SubagentScope | None = None,
                 communication=None, integration=None, allowed_tool_ids=(), workspace=None,
                 delegable_tool_ids=(), parent_plan=None):
        self._registry = registry
        self._executor = executor
        self._base_snapshot = (copy.deepcopy(registry.schemas()), getattr(registry, "version", None)) if integration else None
        self._base_handlers = {t.spec.name: t.handler for t in registry.all()} if integration and isinstance(registry, ToolRegistry) else None
        self._context = context
        self._channel = channel
        self._limits = limits
        self._planning = planning
        self._subagents = subagents
        self._communication = communication
        self.workspace = workspace
        self.delegable_tool_ids = tuple(delegable_tool_ids)
        self._parent_plan = parent_plan
        self._files = ToolRegistry()
        if workspace is not None:
            from shs_agent.workspace.tools import registry as file_registry
            self._files = file_registry(workspace)
        self._file_executor = ToolExecutor(self._files)
        self._workspace_ref = channel.add_artifact("workspace_configuration", workspace.snapshot()) if workspace else None
        if workspace:
            channel.emit("workspace_applied", {"workspace": workspace.snapshot(), "artifact_id": self._workspace_ref},
                         artifact_ids=(self._workspace_ref,))
        self._step = 0
        self._invocation_index: int | None = None
        self._tool_ref: dict[str, Any] | None = None
        self._local = ToolRegistry()
        self._added = ToolRegistry()
        self._added_executor = ToolExecutor(self._added)
        self._tool_revision = 0
        self._tool_set_id = None
        self._last_offered_revision = None
        self.integration = None
        for schema, handler in (
            (HOST_ASK_SCHEMA, self._ask), (MEMO_WRITE_SCHEMA, self._write),
            (CONVERSATION_SEARCH_SCHEMA, self._search),
            (CONVERSATION_READ_SCHEMA, self._read), (TASK_COMPLETE_SCHEMA, self._complete),
        ):
            if schema["name"] == "host_ask" and channel.task.depth != 0:
                continue
            self._local.register(Tool(ToolSpec(**copy.deepcopy(schema)), handler))
        if planning is not None:
            for schema, handler in (
                (PLAN_CREATE_SCHEMA, self._plan_create), (PLAN_READ_SCHEMA, self._plan_read),
                (PLAN_UPDATE_SCHEMA, self._plan_update), (PLAN_REVISE_SCHEMA, self._plan_revise),
            ):
                self._local.register(Tool(ToolSpec(**copy.deepcopy(schema)), handler))
        if subagents is not None and subagents.can_spawn:
            for schema in SUBAGENT_SCHEMAS:
                schema = copy.deepcopy(schema)
                if schema["name"] == "subagent_spawn":
                    schema["parameters"]["properties"]["config_id"]["enum"] = list(subagents.config_ids)
                self._local.register(Tool(ToolSpec(**schema), getattr(self, "_" + schema["name"])))
        if communication is not None:
            allowed = {"agent_reply", "agent_wait_reply", "parent_ask", "parent_notify"}
            if subagents is not None and subagents.can_spawn:
                allowed |= {"subagent_ask", "subagent_send"}
            for schema in COMMUNICATION_SCHEMAS:
                if schema["name"] in allowed:
                    self._local.register(Tool(ToolSpec(**copy.deepcopy(schema)), getattr(self, "_" + schema["name"])))
        self._local_executor = ToolExecutor(self._local)
        if integration is not None:
            for schema in (*RESOURCE_SCHEMAS, *TOOL_INTEGRATION_SCHEMAS):
                self._local.register(Tool(ToolSpec(**copy.deepcopy(schema)), getattr(self, "_" + schema["name"])))
            self.integration = integration.bind(channel, allowed_tool_ids, communication, self)
        self.snapshot()  # fail reserved-name collisions before starting a task
        if self.integration is not None:
            self._record_tool_set()

    def snapshot(self) -> ToolSnapshot:
        if self._base_snapshot is not None:
            if (self._registry.schemas(), getattr(self._registry, "version", None)) != self._base_snapshot:
                raise ToolUnavailable("tool_changed", "base registry changed outside Host grant API")
            if self._base_handlers is not None and any(self._registry.get(name).handler is not handler
                                                       for name, handler in self._base_handlers.items()):
                raise ToolUnavailable("tool_changed", "base handler changed outside Host grant API")
        external = copy.deepcopy(self._registry.schemas() + self._added.schemas() + self._files.schemas())
        names: set[str] = set()
        for schema in external:
            name = schema["name"]
            nonempty(name, "tool name")
            if (name in names or self._local.get(name) is not None or name == "host_ask" or
                    name in {item["name"] for item in (*SUBAGENT_SCHEMAS, *COMMUNICATION_SCHEMAS,
                                                      *TOOL_INTEGRATION_SCHEMAS, *RESOURCE_SCHEMAS)}):
                raise ValueError(f"duplicate or reserved tool name: {name}")
            names.add(name)
        return ToolSnapshot(external + copy.deepcopy(self._local.schemas()),
                            getattr(self._registry, "version", None), self._tool_revision)

    def _record_tool_set(self, definition=None):
        snapshot = self.snapshot()
        schemas = snapshot.schemas
        identity = hashlib.sha256(json.dumps(schemas, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        previous = self._tool_set_id
        self._tool_set_id = self._channel.add_artifact("tool_set", {
            "revision": self._tool_revision, "schema_id": identity, "schemas": schemas,
            "previous_artifact_id": previous, "definition_artifact_id": definition,
        }, references=tuple(r for r in (previous, definition) if r))
        self._channel.emit("tool_set_changed", self.tool_state(), artifact_ids=(self._tool_set_id,))

    def tool_state(self):
        if self._tool_set_id is None:
            return {"revision": 0, "tools": [s["name"] for s in self.snapshot().schemas]}
        data = self._channel.artifact(self._tool_set_id).data
        return {"revision": self._tool_revision, "schema_id": data["schema_id"],
                "artifact_id": self._tool_set_id, "tools": [s["name"] for s in data["schemas"]],
                "last_offered_revision": self._last_offered_revision}

    def validate_addition(self, tool):
        snapshot = self.snapshot()
        if tool.spec.name in {s["name"] for s in snapshot.schemas}:
            raise ValueError("tool name is already registered or reserved")
        reserved = {s["name"] for s in (*SUBAGENT_SCHEMAS, *COMMUNICATION_SCHEMAS, *TOOL_INTEGRATION_SCHEMAS, *RESOURCE_SCHEMAS)}
        if tool.spec.name == "host_ask" or tool.spec.name in reserved:
            raise ValueError("reserved tool name")
        # JSON schema must be exportable before permission is accepted.
        json.dumps(asdict(tool.spec), allow_nan=False)
        return ToolSnapshot(copy.deepcopy(self._registry.schemas()), getattr(self._registry, "version", None))

    def apply_addition(self, tool, before, definition):
        if (self._registry.schemas() != before.schemas or
                getattr(self._registry, "version", None) != before.version):
            raise ValueError("base registry changed after Host approval")
        self.validate_addition(tool)
        # Publish the new executable view atomically; a failed publication must
        # not leave an unreported executable handler behind.
        previous, previous_id = self._added, self._tool_set_id
        candidate = ToolRegistry()
        for existing in previous.all():
            candidate.register(existing)
        candidate.register(tool)
        self._added = candidate
        self._tool_revision += 1
        try:
            self._record_tool_set(definition)
        except Exception:
            self._added, self._tool_set_id = previous, previous_id
            self._tool_revision -= 1
            raise
        self._added_executor = ToolExecutor(candidate)

    def apply_pending(self):
        if self.integration is not None:
            self.integration.apply_pending()

    def apply_workspace(self, workspace):
        from shs_agent.workspace.tools import registry as file_registry
        revision = self.workspace.revision + 1 if self.workspace else 1
        workspace = workspace.revised(revision)
        files = file_registry(workspace)
        occupied = {s["name"] for s in self._registry.schemas() + self._added.schemas() + self._local.schemas()}
        if occupied & {s["name"] for s in files.schemas()}:
            raise ValueError("workspace file tool conflicts with an existing tool")
        ref = self._channel.add_artifact("workspace_configuration", workspace.snapshot())
        previous = (self.workspace, self._files, self._file_executor, self._workspace_ref, self._tool_set_id)
        self.workspace, self._files, self._file_executor = workspace, files, ToolExecutor(files)
        self._workspace_ref = ref
        self._tool_revision += 1
        try:
            self._record_tool_set()
        except Exception:
            self.workspace, self._files, self._file_executor, self._workspace_ref, self._tool_set_id = previous
            self._tool_revision -= 1
            raise
        self._channel.emit("workspace_applied", {"workspace": workspace.snapshot(), "artifact_id": ref},
                           artifact_ids=(ref,))

    def tool(self, name):
        """Resolve an actual Tool for explicit delegation; custom executors are not copied."""
        # File handlers are bound to this task's paths. Rebind them through a
        # delegated Workspace instead of copying a handler with the parent's cwd.
        if self._files.get(name) is not None:
            return None
        sources = [self._added]
        if type(self._executor) is ToolExecutor:
            sources.append(self._registry)
        for registry in sources:
            if isinstance(registry, ToolRegistry):
                tool = registry.get(name)
                if tool is not None:
                    return tool
        return None

    def offered(self, llm_call_id, artifact_id):
        if self.integration is not None:
            self._last_offered_revision = self._tool_revision
            self._channel.emit("tool_set_offered", {**self.tool_state(), "llm_call_id": llm_call_id,
                               "request_artifact_id": artifact_id}, artifact_ids=(self._tool_set_id, artifact_id))

    def close(self):
        if self.integration is not None:
            self.integration.close()

    def check_available(self, name: str, offered: ToolSnapshot) -> None:
        previous = {schema["name"]: schema for schema in offered.schemas}
        if name not in previous:
            raise ToolUnavailable("unknown_tool", f"Tool was not offered: {name}")
        current = self.snapshot()
        schemas = {schema["name"]: schema for schema in current.schemas}
        if schemas.get(name) != previous[name] or offered.revision != current.revision or (
            offered.version is not None and offered.version != current.version
        ):
            raise ToolUnavailable("tool_changed", f"Tool registry changed after planning: {name}")

    async def execute(self, name: str, arguments: dict[str, Any], *,
                      step: int, invocation_index: int,
                      tool_ref: dict[str, Any] | None = None) -> Any:
        self._step, self._invocation_index = step, invocation_index
        self._tool_ref = tool_ref
        try:
            if self._local.get(name) is not None:
                # Start local async handlers on this loop immediately, so a question
                # cannot open after its cancellation was consumed.
                return await self._local_executor.execute(name, copy.deepcopy(arguments))
            if self._added.get(name) is not None:
                return await invoke(self._added_executor.execute, name=name, arguments=copy.deepcopy(arguments))
            if self._files.get(name) is not None:
                return await invoke(self._file_executor.execute, name=name, arguments=copy.deepcopy(arguments))
            return await invoke(self._executor.execute, name=name, arguments=copy.deepcopy(arguments))
        finally:
            self._invocation_index = None
            self._tool_ref = None

    async def _ask(self, question: str, context: str = "") -> str:
        return await self._channel.ask(question, context, self._limits.host_response_timeout,
                                       tool_ref=self._tool_ref)

    async def _tool_request(self, requested_tool: str, reason: str, intended_use: str):
        return self.integration.request(requested_tool, reason, intended_use, tool_ref=self._tool_ref)

    async def _tool_forward(self, request_id: str):
        return self.integration.forward(request_id, tool_ref=self._tool_ref)

    async def _tool_wait(self, request_id: str):
        return await self.integration.wait(request_id, tool_ref=self._tool_ref)

    async def _tool_status(self):
        return self.integration.state()

    async def _workspace_request(self, paths, reason):
        return self.integration.workspace_request(paths, reason, tool_ref=self._tool_ref)

    async def _resource_forward(self, request_id):
        return self.integration.forward(request_id, tool_ref=self._tool_ref)

    async def _resource_grant(self, request_id, workspace_paths=None, redelegate=False):
        return self.integration.provide(request_id, workspace_paths=workspace_paths,
                                        redelegate=redelegate, tool_ref=self._tool_ref)

    async def _resource_deny(self, request_id, reason):
        return self.integration.reject(request_id, reason, tool_ref=self._tool_ref)

    async def _resource_wait(self, request_id):
        return await self.integration.wait(request_id, tool_ref=self._tool_ref)

    async def _resource_status(self):
        return self.integration.state()

    async def _write(self, content: str, kind: str,
                     source_refs: list[str] | None = None) -> dict[str, Any]:
        memo = self._context.memo.write(content, kind, source_refs, step=self._step)
        self._channel.emit("task_memo_written", asdict(memo))
        return {"memo_id": memo.memo_id, "available_in_context": True}

    async def _search(self, query: str, limit: int = 8) -> dict[str, Any]:
        return self._context.conversation.search(query, limit, exclude=self._invocation_index)

    async def _read(self, message_index: int, char_offset: int = 0,
                    max_chars: int | None = None) -> dict[str, Any]:
        return self._context.conversation.read(message_index, char_offset, max_chars,
                                               exclude=self._invocation_index)

    async def _complete(self, output: str) -> str:
        nonempty(output, "output")
        if self.integration is not None and any(r["requesting_task_id"] == self._channel.task_id and
                r["status"] == "approved" for r in self.integration.state()["requests"]):
            raise CommunicationError("approved tools must reach the next decision before completion")
        if self._channel.has_pending_input():
            raise CommunicationError("incoming messages must reach the next decision before completing")
        if self._channel.agent.question_id is not None:
            raise CommunicationError("a Host question is still pending; resume host_ask or process its answer before completing")
        if self._subagents is not None:
            self._subagents.ensure_can_complete()
        return output.strip()

    async def _parent_ask(self, message: str, related_request_id: str | None = None):
        return self._communication.ask_parent(message, related_request_id=related_request_id, tool_ref=self._tool_ref)

    async def _subagent_ask(self, task_id: str, message: str, related_request_id: str | None = None):
        return self._communication.ask_child(task_id, message, related_request_id=related_request_id,
                                              tool_ref=self._tool_ref)

    async def _agent_reply(self, request_id: str, message: str):
        return self._communication.reply(request_id, message, tool_ref=self._tool_ref)

    async def _agent_wait_reply(self, request_id: str):
        return await self._communication.wait_reply(request_id, tool_ref=self._tool_ref)

    async def _parent_notify(self, message: str):
        return self._communication.notify_parent(message, tool_ref=self._tool_ref)

    async def _subagent_send(self, task_id: str, message: str, kind: str = "notify"):
        return self._communication.send_child(task_id, message, kind=kind, tool_ref=self._tool_ref)

    async def _subagent_spawn(self, instruction: str, context: list[str], config_id: str,
                              plan_ref: dict[str, Any] | None = None, role: str = "",
                              result_requirements: str = "", workspace_paths: list[str] | None = None,
                              tool_ids: list[str] | None = None, redelegate: bool = False) -> dict[str, Any]:
        plan_artifact_id = None
        if plan_ref is not None:
            if not isinstance(plan_ref, dict) or set(plan_ref) != {"plan_id", "revision", "item_id"}:
                raise SubagentError("plan_ref requires exactly plan_id, revision and item_id")
            integer(plan_ref["revision"], "plan_ref.revision", 1)
            nonempty(plan_ref["plan_id"], "plan_ref.plan_id")
            nonempty(plan_ref["item_id"], "plan_ref.item_id")
            plan = self._planning.snapshot(plan_ref["revision"])
            if (plan is None or plan.plan_id != plan_ref["plan_id"] or
                    plan.revision != self._channel.task.plan_revision or
                    not any(item.item_id == plan_ref["item_id"] for item in plan.items)):
                raise SubagentError("plan_ref must identify an item in the current parent plan")
            plan_artifact_id = self._channel.task.plan_artifact_id
        return await self._subagents.spawn(instruction=instruction, context=context,
            config_id=config_id, plan_ref=plan_ref, plan_artifact_id=plan_artifact_id, tool_ref=self._tool_ref,
            role=role, result_requirements=result_requirements, workspace_paths=workspace_paths,
            tool_ids=tool_ids, redelegate=redelegate)

    async def _subagent_status(self, task_id: str | None = None) -> dict[str, Any]:
        return await self._subagents.status(task_id)

    async def _subagent_result(self, task_id: str) -> dict[str, Any]:
        return await self._subagents.result(task_id, tool_ref=self._tool_ref)

    async def _subagent_wait(self, task_ids: list[str]) -> dict[str, Any]:
        return await self._subagents.wait(task_ids, tool_ref=self._tool_ref)

    async def _subagent_cancel(self, task_id: str) -> dict[str, Any]:
        return await self._subagents.cancel(task_id, tool_ref=self._tool_ref)

    def _change_plan(self, method: str, **arguments: Any) -> dict[str, Any]:
        previous = self._planning.snapshot()
        plan = getattr(self._planning, method)(**arguments, tool_ref=self._tool_ref)
        return {"changed": plan is not previous, "plan_id": plan.plan_id,
                "revision": plan.revision, "artifact_id": self._channel.task.plan_artifact_id}

    async def _plan_create(self, items: list[dict[str, Any]], reason: str,
                           source_refs: list[str] | None = None) -> dict[str, Any]:
        return self._change_plan("create", items=items, reason=reason, source_refs=source_refs)

    async def _plan_read(self, revision: int | None = None, scope: str = "self") -> dict[str, Any]:
        if scope not in {"self", "parent"}:
            raise ValueError("plan scope must be self or parent")
        plan = (self._parent_plan(revision) if self._parent_plan else None) if scope == "parent" else self._planning.snapshot(revision)
        if scope == "parent":
            self._channel.trace_communication("parent_plan_read", {"plan": plan.to_dict() if plan else None},
                                              tool_ref=self._tool_ref)
        return {"plan": plan.to_dict() if plan is not None else None}

    async def _plan_update(self, updates: list[dict[str, Any]], expected_revision: int, reason: str,
                           source_refs: list[str] | None = None) -> dict[str, Any]:
        return self._change_plan("update", updates=updates, expected_revision=expected_revision,
                                 reason=reason, source_refs=source_refs)

    async def _plan_revise(self, items: list[dict[str, Any]], expected_revision: int, reason: str,
                           source_refs: list[str] | None = None) -> dict[str, Any]:
        return self._change_plan("revise", items=items, expected_revision=expected_revision,
                                 reason=reason, source_refs=source_refs)
