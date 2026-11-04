"""One root's registered sessions, execution handles and cumulative budget."""
from __future__ import annotations

import asyncio
import copy
from dataclasses import asdict, replace
from typing import Any, Callable
from uuid import uuid4

from ..communication import AgentMessage, AgentRequest, ParentConnection, CommunicationError
from ..session.channel import TaskChannel
from ..session.handle import TaskSession
from ..state import AgentActivity
from ..types import AgentLimits, TaskRequest, TaskResult, TaskStatus, nonempty
from .models import SubagentConfig, SubagentError, TaskTreeSnapshot


class SubagentManager:
    """Single-loop root scope. State is always read from each child's Channel.

    Assembly is injected by Solver; this module never imports Solver or starts a
    second agent loop. Registered sessions and observer tasks stay strongly held.
    """

    def __init__(self, *, channel: TaskChannel, limits: AgentLimits,
                 configs: dict[str, SubagentConfig],
                 resolve: Callable[[TaskChannel, SubagentConfig], SubagentConfig],
                 create: Callable[[TaskRequest, SubagentConfig, TaskChannel], TaskSession]):
        if limits.max_total_subagents is None or limits.max_subagent_depth is None:
            raise ValueError("enabling subagents requires both root subagent limits")
        self._channel = channel
        self._limits = limits
        self._configs = dict(configs)
        self._resolve = resolve
        self._create = create
        self._sessions: dict[str, TaskSession] = {}
        self._channels = {channel.task_id: channel}
        self._observers: dict[str, asyncio.Task[TaskResult]] = {}
        self._results: dict[str, TaskResult] = {}
        self._wait_interrupts: dict[str, asyncio.Future[str]] = {}
        self._allowed_configs = {channel.task_id: tuple(configs)}
        self._finishing: set[str] = set()
        self._approved_schemas = {}
        self._environments = {}
        self._plans = {}
        for key, config in self._configs.items():
            nonempty(key, "config_id")
            if not isinstance(config, SubagentConfig):
                raise TypeError("subagents values must be SubagentConfig")
            if any(child not in configs for child in config.allowed_subagent_config_ids):
                raise ValueError("allowed_subagent_config_ids must reference host-approved profiles")
            self._approved_schemas[key] = copy.deepcopy(config.registry.schemas())

    @property
    def root_task_id(self) -> str:
        return self._channel.task_id

    @property
    def total_created(self) -> int:
        return len(self._sessions)

    @property
    def config_ids(self) -> tuple[str, ...]:
        return tuple(self._configs)

    def _caller(self, caller: str) -> TaskChannel:
        self._channel.check_loop()
        if caller not in self._channels:
            raise SubagentError("unknown caller in this root")
        return self._channels[caller]

    def bind_environment(self, task_id, tools, planning):
        self._environments[task_id] = tools
        self._plans[task_id] = planning

    def plan(self, task_id, revision=None):
        return self._plans[task_id].snapshot(revision)

    def _child_ids(self, caller: str) -> tuple[str, ...]:
        return tuple(key for key in self._sessions if self._channels[key].task.parent_task_id == caller)

    def _direct_child(self, caller: str, task_id: str) -> TaskSession:
        self._caller(caller)
        session = self.child(task_id)
        if self._channels[task_id].task.parent_task_id != caller:
            raise SubagentError("only this task's direct children are addressable")
        return session

    def bind(self, channel: TaskChannel, config_ids: tuple[str, ...]):
        """Assembly-only task scope, without the Host's whole-tree capabilities."""
        from .scope import SubagentScope
        enabled = channel.task.depth < self._limits.max_subagent_depth and bool(config_ids)
        return SubagentScope(self, channel.task_id, config_ids if enabled else ())

    def child(self, task_id: str) -> TaskSession:
        """Host access to any descendant's existing Session interface."""
        self._channel.check_loop()
        nonempty(task_id, "task_id")
        if task_id not in self._sessions:
            raise SubagentError("unknown descendant of this root")
        return self._sessions[task_id]

    def _record(self, kind: str, data: dict[str, Any], tool_ref: dict[str, Any] | None = None,
                *, references: tuple[str, ...] = (), caller: str | None = None) -> str:
        channel = self._caller(caller or self.root_task_id)
        tool_ref = tool_ref or {}
        refs = tuple(dict.fromkeys((*references, *((tool_ref["request_artifact_id"],)
                    if tool_ref.get("request_artifact_id") else ()))))
        artifact_id = channel.add_artifact(kind, {**data, "source": {
            "task_id": channel.task_id, **tool_ref,
        }}, operation_id=tool_ref.get("tool_execution_id"), references=refs)
        channel.emit(kind, {"artifact_id": artifact_id,
                                  **{key: data[key] for key in ("task_id", "task_ids", "reason") if key in data}},
                           artifact_ids=(artifact_id,))
        return artifact_id

    async def spawn(self, caller: str, *, instruction: str, context: list[str], config_id: str,
                    plan_ref: dict[str, Any] | None = None,
                    plan_artifact_id: str | None = None,
                     tool_ref: dict[str, Any] | None = None, role: str = "", result_requirements: str = "",
                     workspace_paths: list[str] | None = None, tool_ids: list[str] | None = None,
                     redelegate: bool = False) -> dict[str, Any]:
        parent = self._caller(caller)
        request_id = self._record("subagent_spawn_requested", {
            "instruction": instruction, "context": context, "config_id": config_id,
            "plan_ref": plan_ref,
        }, tool_ref, caller=caller)
        try:
            ancestor = parent
            while True:
                if ancestor.done or ancestor.task.cancel_requested or ancestor.task_id in self._finishing:
                    raise SubagentError("parent or ancestor is closed, cancelling or finishing")
                if ancestor.task.parent_task_id is None:
                    break
                ancestor = self._channels[ancestor.task.parent_task_id]
            depth = parent.task.depth + 1
            if depth > self._limits.max_subagent_depth:
                raise SubagentError("max_subagent_depth reached")
            if self.total_created >= self._limits.max_total_subagents:
                raise SubagentError("max_total_subagents reached")
            nonempty(config_id, "config_id")
            if config_id not in self._allowed_configs[caller]:
                raise SubagentError("config_id was not delegated to this task by the host")
            if not isinstance(context, list) or not all(isinstance(item, str) for item in context):
                raise SubagentError("context must be an array of strings")
            config = self._resolve(parent, self._configs[config_id])
            if config.registry.schemas() != self._approved_schemas[config_id]:
                raise SubagentError("approved child tool registry changed; create a new root configuration")
            environment = self._environments[caller]
            if type(redelegate) is not bool:
                raise SubagentError("redelegate must be a boolean")
            if workspace_paths is not None:
                if environment.workspace is None:
                    raise SubagentError("parent has no workspace to delegate")
                config = replace(config, workspace=environment.workspace.delegate(workspace_paths, redelegate=redelegate))
            if tool_ids is not None:
                from shs_agent.tool import ToolRegistry, ToolExecutor, Tool
                if not isinstance(tool_ids, list) or not all(isinstance(name, str) for name in tool_ids):
                    raise SubagentError("tool_ids must be an array of strings")
                if not isinstance(config.registry, ToolRegistry) or not isinstance(config.executor, ToolExecutor):
                    raise SubagentError("dynamic spawn tools require the standard Registry/Executor")
                registry = ToolRegistry()
                for tool in config.registry.all():
                    registry.register(tool)
                for name in tool_ids:
                    tool = environment.tool(name)
                    if name not in environment.delegable_tool_ids or tool is None:
                        raise SubagentError(f"tool {name!r} is unavailable or not delegable; "
                                            f"delegable extra tools: {list(environment.delegable_tool_ids)}. "
                                            "workspace_paths provides file tools automatically; omit them from tool_ids.")
                    registry.register(Tool(copy.deepcopy(tool.spec), tool.handler))
                config = replace(config, registry=registry, executor=ToolExecutor(registry),
                                 delegable_tool_ids=tuple(dict.fromkeys((*config.delegable_tool_ids,
                                                                       *(tool_ids if redelegate else ())))))
            request = TaskRequest(instruction, tuple(context), config.system_prompt, role, result_requirements)
            channel = TaskChannel(request, model=config.model,
                                  provider=f"{type(config.provider).__module__}.{type(config.provider).__qualname__}",
                                  parent_task_id=caller, root_task_id=self.root_task_id, depth=depth)
            session = self._create(request, config, channel)
            applied_id = channel.add_artifact("subagent_configuration", {
                "config_id": config_id, "provider": channel.agent.provider, "model": config.model,
                "options": config.options, "limits": asdict(config.limits),
                "root_limits": {"max_total_subagents": self._limits.max_total_subagents,
                                "max_subagent_depth": self._limits.max_subagent_depth},
                "tools": config.registry.schemas(),
                "allowed_subagent_config_ids": config.allowed_subagent_config_ids,
                "allowed_tool_ids": config.allowed_tool_ids,
                "delegable_tool_ids": config.delegable_tool_ids,
                "workspace": config.workspace.snapshot() if config.workspace else None,
                "role": role, "result_requirements": result_requirements,
            }, credentials=True)
            child_id = session.task_id
            # Validate all local references before the registration/count boundary.
            if plan_artifact_id is not None:
                parent.artifact(plan_artifact_id)
            creation_id = self._record("subagent_created", {
                "task_id": child_id, "parent_task_id": caller, "root_task_id": self.root_task_id,
                "depth": depth, "config_id": config_id, "plan_ref": plan_ref,
                "total_created": self.total_created + 1,
                "task_refs": [{"task_id": child_id, "artifact_id": channel.task.input_artifact_id},
                              {"task_id": child_id, "artifact_id": applied_id}],
            }, tool_ref, references=(request_id, *((plan_artifact_id,) if plan_artifact_id else ())), caller=caller)
            origin_id = channel.add_artifact("subagent_origin", {"task_refs": [
                {"task_id": caller, "artifact_id": creation_id},
            ]})
            channel.emit("subagent_bound", {"parent_task_id": caller, "root_task_id": self.root_task_id,
                                           "depth": depth}, artifact_ids=(origin_id, applied_id))
            # No await between budget check, assembly, relationship events and count commit.
            self._sessions[child_id] = session
            self._channels[child_id] = channel
            self._allowed_configs[child_id] = config.allowed_subagent_config_ids
            self._channel.journal.attach(channel)
        except Exception as exc:
            self._record("subagent_spawn_rejected", {
                "reason": str(exc), "error_type": type(exc).__name__, "total_created": self.total_created,
            }, tool_ref, references=(request_id,), caller=caller)
            raise
        try:
            await session.start()
        except Exception as exc:
            # Registration is permanent even if scheduling failed. No leaked runner.
            result = TaskResult(child_id, TaskStatus.FAILED, "", "subagent_schedule_failed", 0, 0, 0,
                                warnings=(f"{type(exc).__name__}: {exc}",))
            context_id = channel.add_artifact("final_context", {
                "messages": [], "memos": [], "context_summary": "", "context_compactions": 0,
            })
            channel.close(result, context_artifact_id=context_id)
            result = replace(result, tracer=channel.tracer_snapshot())
            session.set_start_failure(result)
        observer = asyncio.create_task(self._observe(session), name=f"observe-child-{child_id}")
        self._observers[child_id] = observer
        # Strong ownership plus eager exception retrieval even without a result consumer.
        observer.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        return await self._status_one(child_id)

    async def _observe(self, session: TaskSession) -> TaskResult:
        result = await session.result()
        self._results[session.task_id] = result
        parent_id = self._channels[session.task_id].task.parent_task_id
        if not self._channels[parent_id].done:
            self._record("subagent_terminated", {
                "task_id": session.task_id, "status": result.status.value,
                "stop_reason": result.stop_reason, "task_refs": self._result_refs(result),
            }, caller=parent_id)
        return result

    @staticmethod
    def _result_refs(result: TaskResult) -> list[dict[str, str]]:
        return [{"task_id": result.task_id, "artifact_id": artifact.artifact_id}
                for artifact in result.tracer.artifacts.values()
                if artifact.kind == "final_context" or
                artifact.artifact_id == result.tracer.state().task.plan_artifact_id]

    async def _status_one(self, task_id: str) -> dict[str, Any]:
        session = self.child(task_id)
        state = await session.state()
        return {"task_id": task_id, "state": state.to_dict(), "done": session.done,
                "result_available": task_id in self._results,
                "trace_ref": {"task_id": task_id}}

    async def children(self, parent_task_id: str | None = None) -> list[dict[str, Any]]:
        """Small live snapshots; each cursor belongs only to its named Task."""
        parent_task_id = parent_task_id or self.root_task_id
        self._caller(parent_task_id)
        return [await self._status_one(task_id) for task_id in self._child_ids(parent_task_id)]

    async def descendants(self) -> list[dict[str, Any]]:
        """Host live view of every registered descendant, including terminal tasks."""
        self._channel.check_loop()
        return [await self._status_one(task_id) for task_id in tuple(self._sessions)]

    async def status(self, caller: str, task_id: str | None = None) -> dict[str, Any]:
        self._caller(caller)
        if task_id is not None:
            self._direct_child(caller, task_id)
        return {"children": [await self._status_one(task_id)] if task_id is not None else await self.children(caller)}

    async def result(self, caller: str, task_id: str, *, tool_ref=None) -> dict[str, Any]:
        self._direct_child(caller, task_id)
        status = await self._status_one(task_id)
        # A closed Channel guarantees runtime has built its result. Joining only the
        # observer here cannot secretly wait for a running child's completion.
        if status["done"]:
            await asyncio.shield(self._observers[task_id])
        result = self._results.get(task_id)
        payload = {**status, "available": result is not None, "result_available": result is not None}
        if result is not None:
            payload.update(output=result.output, status=result.status.value, stop_reason=result.stop_reason,
                           memos=[asdict(memo) for memo in result.memos], task_refs=self._result_refs(result))
        self._record("subagent_result_delivered" if result is not None else "subagent_result_pending", {
            "task_id": task_id, "available": result is not None,
            "task_refs": self._result_refs(result) if result else [],
        }, tool_ref, caller=caller)
        return payload

    async def wait(self, caller: str, task_ids: list[str], *, tool_ref=None) -> dict[str, Any]:
        channel = self._caller(caller)
        if (not isinstance(task_ids, list) or not task_ids or
                not all(isinstance(item, str) and item.strip() for item in task_ids) or
                len(set(task_ids)) != len(task_ids)):
            raise SubagentError("task_ids must be a nonempty unique array of child IDs")
        for task_id in task_ids:
            self._direct_child(caller, task_id)
        if caller in self._wait_interrupts:
            raise SubagentError("a parent wait is already active")
        self._record("subagent_wait_started", {"task_ids": task_ids}, tool_ref, caller=caller)
        interrupt = asyncio.get_running_loop().create_future()
        self._wait_interrupts[caller] = interrupt
        channel.update_state(agent={"activity": AgentActivity.WAITING_FOR_SUBAGENTS,
                                          "waiting_for": "subagents", "waiting_task_ids": tuple(task_ids)})
        # Cancelling this aggregate cancels only shield wrappers, not observers/runners.
        joined = asyncio.gather(*(asyncio.shield(self._observers[key]) for key in task_ids))
        reason = "terminated"
        incoming = None
        wake = None
        try:
            while True:
                incoming = channel.communication_interrupt()
                if interrupt.done():
                    reason = interrupt.result()
                    break
                if incoming is not None:
                    reason = incoming["reason"]
                    break
                if joined.done():
                    await joined
                    break
                channel.prepare_communication_wait()
                wake = asyncio.create_task(channel.wait_communication())
                await asyncio.wait({joined, interrupt, wake}, return_when=asyncio.FIRST_COMPLETED)
                wake.cancel()
                await asyncio.gather(wake, return_exceptions=True)
                wake = None
            return {**(incoming or {"request_ids": [], "message_ids": []}), "reason": reason,
                    "children": [await self._status_one(key) for key in task_ids]}
        except asyncio.CancelledError:
            reason = "wait_cancelled"
            raise
        finally:
            if wake is not None:
                wake.cancel()
                await asyncio.gather(wake, return_exceptions=True)
            if not joined.done():
                joined.cancel()
            await asyncio.gather(joined, return_exceptions=True)
            if not interrupt.done():
                interrupt.cancel()
            del self._wait_interrupts[caller]
            channel.update_state(agent={"activity": AgentActivity.TOOL_EXECUTION, "waiting_for": None,
                                              "waiting_task_ids": ()})
            self._record("subagent_wait_finished", {"task_ids": task_ids, **(incoming or {}),
                                                    "reason": reason}, tool_ref, caller=caller)

    def communication(self, channel: TaskChannel, *, can_manage: bool = False):
        """Bind identity at assembly; intermediate tasks can serve both roles."""
        channel.bind_communication(lambda: self._close_requests(channel))
        def send(**kwargs):
            if "task_id" not in kwargs and channel.task.parent_task_id is None:
                return channel.send_parent_request(timeout=self._limits.host_response_timeout, **kwargs)
            if "task_id" in kwargs:
                target_channel = self._channels.get(kwargs["task_id"])
                if target_channel is None or target_channel.task.parent_task_id != channel.task_id:
                    raise CommunicationError("subagent tools may address only this task's direct children")
            target = kwargs.pop("task_id", channel.task.parent_task_id)
            return self.send_message(channel.task_id, target, **kwargs)
        def reply(**kwargs):
            if channel.request(kwargs["request_id"]).message.from_task_id is None:
                return channel.reply_to_host(**kwargs)
            return self.reply_message(channel.task_id, **kwargs)
        return ParentConnection(send, reply, channel.wait_reply)

    def _communication_channel(self, caller: str) -> TaskChannel:
        self._channel.check_loop()
        if caller not in self._channels:
            raise CommunicationError("unknown caller in this root")
        source = self._channels[caller]
        if source.done or source.task.cancel_requested:
            raise CommunicationError("sender is closed or cancelling")
        return source

    def _peer(self, source: TaskChannel, target_id: str) -> TaskChannel:
        nonempty(target_id, "task_id")
        target = self._channels.get(target_id)
        if target is None or not (
            target.task.parent_task_id == source.task_id or source.task.parent_task_id == target.task_id
        ):
            raise CommunicationError("only this task's direct parent or child is addressable")
        return target

    def send_message(self, caller: str, task_id: str, *, kind: str, message: str,
                     related_request_id: str | None = None, tool_ref=None) -> dict[str, Any]:
        source = self._communication_channel(caller)
        nonempty(message, "message")
        if kind not in {"question", "notify", "instruction"}:
            raise CommunicationError("invalid message kind")
        value = AgentMessage(uuid4().hex, uuid4().hex if kind == "question" else None,
                             caller, task_id, kind, message, related_request_id)
        requested = source.trace_communication("agent_message_requested", value.to_dict(), tool_ref=tool_ref)
        reference = {"task_id": caller, "artifact_id": requested}
        refs = [reference]
        try:
            target = self._peer(source, task_id)
            if kind == "instruction" and target.task.parent_task_id != source.task_id:
                raise CommunicationError("children may notify or ask their parent, not instruct it")
            if related_request_id is not None:
                if kind != "question":
                    raise CommunicationError("only questions accept related_request_id")
                refs.append(source.request(related_request_id).reference)
            if target.done or target.task.cancel_requested:
                result = {"accepted": False, "reason": "target_terminated" if target.done else "target_cancelling",
                          "message_id": value.message_id, "request_id": value.request_id}
                source.trace_communication("agent_message_rejected", {**result, "task_refs": refs}, tool_ref=tool_ref)
                return result
        except (CommunicationError, ValueError, TypeError) as exc:
            source.trace_communication("agent_message_rejected", {"message_id": value.message_id,
                "reason": str(exc), "task_refs": [reference]}, tool_ref=tool_ref)
            raise
        if kind == "question":
            request = AgentRequest(value, requested)
            source.put_request(request)
            target.put_request(request)
        source.record_sent(value)
        source.trace_communication("agent_message_accepted", {"message_id": value.message_id,
            "request_id": value.request_id, "to_task_id": task_id, "task_refs": refs}, tool_ref=tool_ref)
        target.trace_communication("agent_message_received", {"message_id": value.message_id,
            "request_id": value.request_id, "from_task_id": caller, "kind": kind, "task_refs": [reference]})
        target.receive_agent(value, requested)
        return {"accepted": True, "reason": "queued", "message_id": value.message_id,
                "request_id": value.request_id, "task_refs": [reference]}

    def reply_message(self, caller: str, *, request_id: str, message: str, tool_ref=None) -> dict[str, Any]:
        source = self._communication_channel(caller)
        nonempty(message, "message")
        try:
            request = source.request(request_id)
            if request.message.to_task_id != caller:
                raise CommunicationError("only the recorded recipient may answer this request")
            target = self._peer(source, request.message.from_task_id)
        except (CommunicationError, ValueError, TypeError) as exc:
            source.trace_communication("agent_reply_rejected", {"request_id": request_id, "reason": str(exc)},
                                       tool_ref=tool_ref)
            raise
        if request.status != "pending" or target.done or target.task.cancel_requested:
            result = {"accepted": False, "reason": "request_closed" if request.status != "pending" else
                      "target_terminated" if target.done else "target_cancelling",
                      "request_id": request_id, "request_status": request.status}
            source.trace_communication("agent_reply_rejected", {**result, "task_refs": [request.reference]},
                                       tool_ref=tool_ref)
            return result
        value = AgentMessage(uuid4().hex, request_id, caller, target.task_id, "reply", message,
                             request.message.related_request_id)
        artifact_id = source.trace_communication("agent_message_requested", {
            **value.to_dict(), "task_refs": [request.reference]}, tool_ref=tool_ref)
        updated = replace(request, status="answered", reply=value, reply_artifact_id=artifact_id)
        source.put_request(updated)
        target.put_request(updated)
        reference = {"task_id": caller, "artifact_id": artifact_id}
        source.record_sent(value)
        for channel, event in ((source, "agent_message_accepted"), (target, "agent_message_received")):
            channel.trace_communication(event, {"message_id": value.message_id, "request_id": request_id,
                "kind": "reply", "from_task_id": caller, "to_task_id": target.task_id,
                "task_refs": [reference, request.reference]}, tool_ref=tool_ref if channel is source else None)
        target.receive_agent(value, artifact_id)
        return {"accepted": True, "reason": "answered", "message_id": value.message_id,
                "request_id": request_id, "task_refs": [reference]}

    def _close_requests(self, channel: TaskChannel) -> None:
        for request in channel.requests():
            if request.status != "pending":
                continue
            value = request.message
            status = "target_terminated" if value.to_task_id == channel.task_id else "requester_terminated"
            for task_id in (value.from_task_id, value.to_task_id):
                if task_id is None:
                    continue
                peer = self._channels[task_id]
                if not peer.done:
                    peer.put_request(replace(request, status=status))
                    peer.trace_communication("agent_request_closed", {"request_id": value.request_id,
                        "reason": status, "terminated_task_id": channel.task_id, "task_refs": [request.reference]})

    def interrupt_wait(self, reason: str, *, caller: str | None = None) -> None:
        interrupt = self._wait_interrupts.get(caller or self.root_task_id)
        if interrupt is not None and not interrupt.done():
            interrupt.set_result(reason)

    async def cancel(self, caller: str, task_id: str, *, tool_ref=None) -> dict[str, Any]:
        session = self._direct_child(caller, task_id)
        receipt = None if session.done else await session.cancel()
        self._record("subagent_cancel_requested", {"task_id": task_id, "message_id": receipt,
                                                    "accepted": receipt is not None}, tool_ref, caller=caller)
        return {"accepted": receipt is not None, "message_id": receipt, **await self._status_one(task_id)}

    def ensure_can_complete(self, caller: str | None = None) -> None:
        caller = caller or self.root_task_id
        self._caller(caller)
        if any(not self._sessions[key].done for key in self._child_ids(caller)):
            raise SubagentError("running children remain; explicitly wait/cancel and collect their results before completion")

    async def finish_parent(self, status: TaskStatus, *, caller: str | None = None) -> None:
        """Approved policy: abnormal parent exit requests cooperative child cancel, then joins."""
        caller = caller or self.root_task_id
        channel = self._caller(caller)
        self._finishing.add(caller)
        children = self._child_ids(caller)
        if status != TaskStatus.COMPLETED:
            for task_id in children:
                session = self._sessions[task_id]
                if not session.done:
                    await self.cancel(caller, task_id)
        if children:
            channel.update_state(agent={"activity": AgentActivity.WAITING_FOR_SUBAGENTS,
                                        "waiting_for": "subagent_cleanup", "waiting_task_ids": children})
            await asyncio.gather(*(asyncio.shield(self._observers[key]) for key in children))

    async def snapshot(self) -> TaskTreeSnapshot:
        """Nonblocking live/finished export; complete is independent of root log close."""
        self._channel.check_loop()
        # No await while copying: registrations and message links cannot straddle this cut.
        traces = {key: channel.tracer_snapshot() for key, channel in self._channels.items()}
        return TaskTreeSnapshot(self.root_task_id, traces, tuple(self._channel.journal.order))

    async def collect(self) -> TaskTreeSnapshot:
        """Join currently registered descendants (and their cleanup), then export.

        A running root may still spawn after this call; await root.result() for a
        final archive. This API never marks a running task complete.
        """
        self._channel.check_loop()
        await asyncio.gather(*(asyncio.shield(task) for task in self._observers.values()))
        return await self.snapshot()
