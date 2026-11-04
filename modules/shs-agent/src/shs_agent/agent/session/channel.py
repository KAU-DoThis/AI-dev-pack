"""In-process command inbox, replayable event journal and correlated questions.

Runtime uses these methods, never Session's queues or Futures. All access is
confined to one asyncio loop. This module does not decide execution policy.
"""
from __future__ import annotations

import asyncio
import copy
from collections import deque
from dataclasses import replace
from typing import Any, AsyncIterator
from uuid import uuid4

from ..state import AgentActivity, AgentState, SessionState, TaskState
from ..communication.models import AgentMessage, AgentRequest, CommunicationError
from ..planning import PlanError, PlanSnapshot
from ..tracer import TraceArtifact, Tracer, TracerSnapshot
from ..tracer.models import STATE_EVENT_TYPES, utc_timestamp
from ..types import AgentEvent, HostCommand, TaskRequest, TaskResult, TaskStatus, integer, nonempty
from .journal import TreeJournal


class QuestionInterrupted(Exception):
    """Runtime invalidated a pending question at a control boundary."""


class HostResponseTimeout(Exception):
    pass


class TaskChannel:
    def __init__(self, request: TaskRequest | None = None, *, model: str | None = None,
                 provider: str | None = None, task_id: str | None = None,
                 parent_task_id: str | None = None, root_task_id: str | None = None,
                 depth: int = 0) -> None:
        task_id = task_id or uuid4().hex
        integer(depth, "depth")
        if depth == 0 and (parent_task_id is not None or root_task_id not in (None, task_id)):
            raise ValueError("a root has no parent and is its own root_task_id")
        if depth == 1 and (not parent_task_id or parent_task_id != root_task_id or task_id == root_task_id):
            raise ValueError("a direct child's parent must be its distinct root")
        if depth > 1 and (not parent_task_id or not root_task_id or
                          len({task_id, parent_task_id, root_task_id}) != 3):
            raise ValueError("a descendant needs distinct task, parent and root IDs")
        self._commands: deque[HostCommand] = deque()
        self._accepted: dict[str, HostCommand] = {}
        self._inbox_ready = asyncio.Event()
        self._tracer = Tracer(task_id)
        input_id = self._tracer.add_artifact("task_input", request)
        self._task_state = TaskState(task_id, request.instruction if request else "", TaskStatus.CREATED,
                                     utc_timestamp(), input_id, parent_task_id=parent_task_id,
                                     root_task_id=root_task_id or task_id, depth=depth)
        self._agent_state = AgentState(model=model, provider=provider)
        self._events_ready = asyncio.Event()
        self._question: tuple[str, asyncio.Future[str]] | None = None
        self._host_detached = False
        self._host_question_args = None
        self._host_question_ref = None
        self._host_timer = None
        self._host_answers: deque[tuple[str, str]] = deque()
        self._tool_updates = deque()
        self.host_response_expired = False
        self._agent_inbox: deque[tuple[AgentMessage, str]] = deque()
        self._agent_sent: deque[AgentMessage] = deque()
        self._agent_requests: dict[str, AgentRequest] = {}
        self._parent_timers = {}
        self._communication_ready = asyncio.Event()
        self._communication_interrupt: str | None = None
        self._on_close = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.journal = TreeJournal()
        self.journal.attach(self)
        self.emit("created", {"state": self.state().to_dict()}, artifact_ids=(input_id,))

    @property
    def task_id(self) -> str:
        return self._task_state.task_id

    @property
    def status(self) -> TaskStatus:
        return self._task_state.status

    @property
    def done(self) -> bool:
        return self._tracer.closed

    @property
    def task(self) -> TaskState:
        return self._task_state

    @property
    def agent(self) -> AgentState:
        return self._agent_state

    def state(self) -> SessionState:
        # No await, full journal copy, or Conversation traversal. Values are immutable.
        return SessionState(self._task_state, self._agent_state, len(self._tracer))

    def update_state(self, *, task: dict[str, Any] | None = None,
                     agent: dict[str, Any] | None = None,
                     kind: str = "state_changed", payload: dict[str, Any] | None = None,
                     artifact_ids: tuple[str, ...] = ()) -> None:
        if self.done:
            raise RuntimeError("cannot update a closed task")
        next_task = replace(self._task_state, **(task or {}))
        next_agent = replace(self._agent_state, **(agent or {}))
        if next_task == self._task_state and next_agent == self._agent_state and kind == "state_changed":
            return
        self._task_state, self._agent_state = next_task, next_agent
        # The state mutation and its event are one non-yielding loop operation.
        self.emit(kind, {**(payload or {}), "state": self.state().to_dict()}, artifact_ids=artifact_ids)

    def add_artifact(self, kind: str, data: Any, **kwargs: Any) -> str:
        return self._tracer.add_artifact(kind, data, **kwargs)

    def artifact(self, artifact_id: str) -> TraceArtifact:
        return self._tracer.artifact(artifact_id)

    def publish_plan(self, plan: PlanSnapshot, previous: PlanSnapshot | None,
                     tool_ref: dict[str, Any]) -> None:
        """Publish a validated revision and its small state in one non-yielding boundary.

        Planning remains the plan owner; Channel stores only identity and a trace ref.
        Resolve explicit artifact evidence before registering anything or changing state.
        """
        if self.done:
            raise RuntimeError("cannot change a closed task's plan")
        if (plan.task_id != self.task_id or
                (previous.plan_id if previous else None) != self.task.plan_id or
                (previous.revision if previous else None) != self.task.plan_revision):
            raise PlanError("plan does not match this task's current revision")
        before_id = self.task.plan_artifact_id
        request_id = tool_ref.get("request_artifact_id")
        evidence = tuple(dict.fromkeys(
            ref[len("artifact:"):] for ref in (*plan.source_refs,
                *(ref for item in plan.items for ref in item.source_refs))
            if ref.startswith("artifact:")
        ))
        references = tuple(dict.fromkeys(ref for ref in (before_id, request_id, *evidence) if ref is not None))
        for ref in references:
            try:
                self.artifact(ref)
            except KeyError as exc:
                raise PlanError(f"unknown artifact reference: {ref}") from exc
        after_id = self.add_artifact("plan_revision", plan.to_dict(),
                                     operation_id=tool_ref.get("tool_execution_id"),
                                     references=references)
        self.update_state(task={"plan_id": plan.plan_id, "plan_revision": plan.revision,
                                "plan_artifact_id": after_id}, kind=f"plan_{plan.change_kind}", payload={
            "task_id": plan.task_id, "plan_id": plan.plan_id,
            "previous_revision": previous.revision if previous else None,
            "revision": plan.revision, "change_kind": plan.change_kind,
            "reason": plan.reason, "source_refs": list(plan.source_refs),
            "before_artifact_id": before_id, "after_artifact_id": after_id,
            "llm_call_id": tool_ref.get("llm_call_id"),
            "tool_execution_id": tool_ref.get("tool_execution_id"),
            "tool_call_id": tool_ref.get("tool_call_id"), "request_artifact_id": request_id,
        }, artifact_ids=(*references, after_id))

    def check_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("a session must be used from one asyncio event loop")

    def send(self, kind: str, payload: dict[str, Any], message_id: str | None) -> str:
        self.check_loop()
        message_id = uuid4().hex if message_id is None else message_id
        nonempty(message_id, "message_id")
        command = HostCommand(message_id, kind, copy.deepcopy(payload))
        if message_id in self._accepted:
            if self._accepted[message_id] != command:
                raise ValueError("message_id was already used for different content")
            return message_id
        if self.done:
            raise RuntimeError("task is finished; create a new session")
        self._accepted[message_id] = command
        self._commands.append(command)
        if kind == "cancel":
            self.update_state(task={"cancel_requested": True})
        self.emit("command_received", {
            "message_id": message_id, "command_type": kind, "command": command.payload,
        })
        self._inbox_ready.set()
        return message_id

    def take_commands(self) -> tuple[HostCommand, ...]:
        commands = tuple(self._commands)
        self._commands.clear()
        self._inbox_ready.clear()
        return commands

    async def wait_commands(self) -> None:
        await self._inbox_ready.wait()

    def acknowledge(self, command: HostCommand, *, reason: str | None = None) -> None:
        payload = {"message_id": command.message_id, "command_type": command.type}
        if reason is not None:
            payload["reason"] = reason
        self.emit("command_rejected" if reason else "command_applied", payload)

    def emit(self, kind: str, payload: dict[str, Any], *, artifact_ids: tuple[str, ...] = ()) -> None:
        if self.done:
            raise RuntimeError("cannot publish after the task has closed")
        if kind in STATE_EVENT_TYPES and "state" in payload:
            payload = {**payload, "state": {**payload["state"], "last_event_sequence": len(self._tracer) + 1}}
            artifact_ids = tuple(dict.fromkeys((*artifact_ids, self.task.input_artifact_id,
                                                *((self.task.plan_artifact_id,) if self.task.plan_artifact_id else ()),
                                                *((self.agent.operation_artifact_id,) if self.agent.operation_artifact_id else ()))))
        self._tracer.record(kind, payload, step=self.agent.steps, artifact_ids=artifact_ids)
        self.journal.record(self.task_id, len(self._tracer))
        self._events_ready.set()

    def event_at(self, sequence):
        return self._tracer.read(sequence - 1)

    def tracer_snapshot(self) -> TracerSnapshot:
        return self._tracer.snapshot()

    def set_status(self, status: TaskStatus) -> None:
        if self.status != status:
            previous = self.status
            changes: dict[str, Any] = {"status": status}
            if status == TaskStatus.RUNNING and self.task.started_at is None:
                changes["started_at"] = utc_timestamp()
            if status == TaskStatus.CANCELLING:
                changes["cancel_requested"] = True
            self.update_state(task=changes, kind="status_changed",
                              payload={"status": status.value, "previous_status": previous.value})

    def close(self, result: TaskResult, *, context_artifact_id: str | None = None) -> None:
        if not result.status.terminal:
            raise ValueError("a final result must have a terminal status")
        if self._on_close is not None:
            self._on_close()
        if self._question is not None:
            self._close_host_question("task_terminated")
        for timer in self._parent_timers.values():
            timer.cancel()
        self._parent_timers.clear()
        for request in tuple(self._agent_requests.values()):
            if request.status == "pending" and None in (request.message.from_task_id, request.message.to_task_id):
                self.put_request(replace(request, status="closed"))
                self.emit("agent_request_closed", {"request_id": request.message.request_id,
                                                  "reason": "task_terminated"})
        self.set_status(result.status)
        self.update_state(task={"finished_at": utc_timestamp(), "stop_reason": result.stop_reason},
                          agent={"activity": AgentActivity.FINISHED, "operation_id": None,
                                 "operation_kind": None, "operation_artifact_id": None,
                                 "waiting_for": None, "question_id": None,
                                 "waiting_request_id": None, "waiting_task_ids": ()}, kind="finished", payload={
            "status": result.status.value, "stop_reason": result.stop_reason,
            "output": result.output, "steps": result.steps,
            "tool_calls": result.tool_calls, "tool_errors": result.tool_errors,
            "memo_items": len(result.memos), "context_compactions": result.context_compactions,
            "warnings": list(result.warnings),
            "context_artifact_id": context_artifact_id,
        }, artifact_ids=(context_artifact_id,) if context_artifact_id else ())
        self._tracer.close()
        self._events_ready.set()
        self.journal.ready.set()

    async def events(self, after_sequence: int) -> AsyncIterator[AgentEvent]:
        self.check_loop()
        integer(after_sequence, "after_sequence")
        if after_sequence > len(self._tracer):
            raise ValueError("after_sequence is beyond the current event log")
        index = after_sequence
        while True:
            while index < len(self._tracer):
                event = self._tracer.read(index)
                index += 1
                yield event
            if self.done:
                return
            # No await between inspecting the journal and clearing this signal.
            # Each subscriber owns its cursor, so subscribers never steal events.
            self._events_ready.clear()
            await self._events_ready.wait()

    async def ask(self, question: str, context: str, timeout: float | None, *,
                  tool_ref: dict[str, Any] | None = None) -> str | dict[str, Any]:
        nonempty(question, "question")
        if not isinstance(context, str):
            raise TypeError("context must be a string")
        if self._question is not None:
            if not self._host_detached or self._host_question_args != (question, context):
                raise RuntimeError("a Host question is already pending; repeat the same question/context to resume its wait")
            question_id, future = self._question
        else:
            question_id = uuid4().hex
            future = asyncio.get_running_loop().create_future()
            self._question = question_id, future
            self._host_question_args = question, context
            self._host_question_ref = tool_ref
            if timeout is not None:
                def expire():
                    if not future.done():
                        future.set_exception(HostResponseTimeout())
                self._host_timer = asyncio.get_running_loop().call_later(timeout, expire)
            def finished(_):
                if self._question is not None and self._question[1] is future and self._host_detached:
                    self._commands.append(HostCommand(question_id, "host_question_ready", {}))
                    self._inbox_ready.set()
            future.add_done_callback(finished)
            self.emit("question", {"question_id": question_id, "request_id": question_id,
                                   "question": question, "context": context, "tool_ref": tool_ref})
        self._host_detached = False
        self.update_state(agent={"activity": AgentActivity.WAITING_FOR_HOST,
                                 "waiting_for": "host", "question_id": question_id})
        reason = "answered"
        wake = None
        try:
            while not future.done():
                incoming = self.communication_interrupt()
                if incoming is not None and incoming["reason"] in {"incoming_request", "incoming_message", "tool_request_updated"}:
                    self._host_detached = True
                    self.emit("host_wait_released", {"question_id": question_id, **incoming,
                                                      "tool_ref": tool_ref})
                    return {"question_id": question_id, "pending": True, **incoming}
                self._communication_ready.clear()
                wake = asyncio.create_task(self.wait_communication())
                await asyncio.wait({future, wake}, return_when=asyncio.FIRST_COMPLETED)
                wake.cancel()
                await asyncio.gather(wake, return_exceptions=True)
                wake = None
            return future.result()
        except HostResponseTimeout:
            reason = "host_response_timeout"
            raise
        except QuestionInterrupted as exc:
            reason = str(exc)
            raise
        except asyncio.CancelledError:
            reason = "runtime_cancelled"
            raise
        finally:
            if wake is not None:
                wake.cancel()
                await asyncio.gather(wake, return_exceptions=True)
            if not self._host_detached:
                self._close_host_question(reason)
            self.update_state(agent={"activity": AgentActivity.TOOL_EXECUTION,
                                     "waiting_for": None})

    def _close_host_question(self, reason: str) -> None:
        question_id, future = self._question
        if self._host_timer is not None:
            self._host_timer.cancel()
        if not future.done():
            future.cancel()
        elif not future.cancelled():
            future.exception()
        self._question = None
        self._host_detached = False
        self.update_state(agent={"question_id": None})
        self.emit("question_closed", {"question_id": question_id, "request_id": question_id,
                                       "reason": reason, "tool_ref": self._host_question_ref})

    def collect_host_answer(self) -> None:
        if self._question is None or not self._host_detached or not self._question[1].done():
            return
        question_id, future = self._question
        reason = "answered"
        try:
            self._host_answers.append((question_id, future.result()))
            self.interrupt_communication_wait("host_answer")
        except QuestionInterrupted as exc:
            reason = str(exc)
        except HostResponseTimeout:
            reason = "host_response_timeout"
            self.host_response_expired = True
            self.interrupt_communication_wait(reason)
        finally:
            self._close_host_question(reason)

    def take_host_answers(self):
        answers = tuple(self._host_answers)
        self._host_answers.clear()
        return answers

    def accept_answer(self, question_id: str, text: str) -> bool:
        request = self._agent_requests.get(question_id)
        if request is not None and request.message.to_task_id is None:
            if request.status != "pending":
                return False
            message = AgentMessage(uuid4().hex, question_id, None, self.task_id, "reply", text)
            artifact = self.trace_communication("agent_message_received", message.to_dict())
            self.put_request(replace(request, status="answered", reply=message, reply_artifact_id=artifact))
            timer = self._parent_timers.pop(question_id, None)
            if timer:
                timer.cancel()
            self.receive_agent(message, artifact)
            return True
        if self._question is None:
            return False
        pending_id, future = self._question
        if pending_id != question_id or future.done():
            return False
        future.set_result(text)
        return True

    def send_parent_request(self, *, kind, message, timeout=None, related_request_id=None, tool_ref=None):
        self.check_loop()
        if self.done or self.task.cancel_requested:
            raise CommunicationError("sender is closed or cancelling")
        nonempty(message, "message")
        if related_request_id is not None:
            self.request(related_request_id)
        value = AgentMessage(uuid4().hex, uuid4().hex if kind == "question" else None,
                             self.task_id, None, kind, message, related_request_id)
        artifact = self.trace_communication("agent_message_requested", value.to_dict(), tool_ref=tool_ref)
        self.record_sent(value)
        if kind == "question":
            self.put_request(AgentRequest(value, artifact))
            self.emit("question", {"question_id": value.request_id, "request_id": value.request_id,
                                  "question": message, "context": "", "transport": "parent"})
            if timeout is not None:
                def expired():
                    request = self.request(value.request_id)
                    if request.status == "pending" and not self.done:
                        self.put_request(replace(request, status="timed_out"))
                        self.host_response_expired = True
                        self.interrupt_communication_wait("host_response_timeout")
                self._parent_timers[value.request_id] = asyncio.get_running_loop().call_later(timeout, expired)
        else:
            self.emit("parent_notification", value.to_dict())
        return {"accepted": True, "reason": "queued", "message_id": value.message_id,
                "request_id": value.request_id, "task_refs": [{"task_id": self.task_id, "artifact_id": artifact}]}

    def ask_from_host(self, message):
        self.check_loop()
        if self.done:
            raise CommunicationError("target is closed")
        nonempty(message, "message")
        value = AgentMessage(uuid4().hex, uuid4().hex, None, self.task_id, "question", message)
        artifact = self.trace_communication("agent_message_received", value.to_dict())
        self.put_request(AgentRequest(value, artifact))
        self.receive_agent(value, artifact)
        return value.request_id

    def reply_to_host(self, request_id, message, *, tool_ref=None):
        nonempty(message, "message")
        request = self.request(request_id)
        if request.message.from_task_id is not None or request.message.to_task_id != self.task_id:
            raise CommunicationError("only the recipient may answer a Host request")
        if request.status != "pending":
            return {"accepted": False, "reason": "request_closed", "request_id": request_id}
        value = AgentMessage(uuid4().hex, request_id, self.task_id, None, "reply", message)
        artifact = self.trace_communication("agent_message_requested", value.to_dict(), tool_ref=tool_ref)
        self.put_request(replace(request, status="answered", reply=value, reply_artifact_id=artifact))
        self.record_sent(value)
        self.emit("parent_reply", value.to_dict())
        return {"accepted": True, "reason": "answered", "request_id": request_id}

    def interrupt_question(self, reason: str) -> None:
        if self._question is not None and not self._question[1].done():
            self._question[1].set_exception(QuestionInterrupted(reason))

    def bind_communication(self, on_close) -> None:
        self._on_close = on_close

    def request(self, request_id: str) -> AgentRequest:
        nonempty(request_id, "request_id")
        if request_id not in self._agent_requests:
            raise CommunicationError("unknown request for this task")
        return self._agent_requests[request_id]

    def requests(self) -> tuple[AgentRequest, ...]:
        return tuple(self._agent_requests.values())

    def put_request(self, request: AgentRequest) -> None:
        self._agent_requests[request.message.request_id] = request
        self._communication_state()
        self._communication_ready.set()

    def _communication_state(self) -> None:
        pending = [r.message for r in self._agent_requests.values() if r.status == "pending"]
        self.update_state(agent={
            "outgoing_request_ids": tuple(m.request_id for m in pending if m.from_task_id == self.task_id),
            "incoming_request_ids": tuple(m.request_id for m in pending if m.to_task_id == self.task_id),
            "queued_message_ids": tuple(m.message_id for m, _ in self._agent_inbox),
        })

    def record_sent(self, message: AgentMessage) -> None:
        self._agent_sent.append(message)

    def receive_agent(self, message: AgentMessage, artifact_id: str) -> None:
        self._agent_inbox.append((message, artifact_id))
        self._communication_state()
        self._communication_ready.set()
        if message.kind == "instruction":
            self._commands.append(HostCommand(message.message_id, "agent_instruction", {}))
            self._inbox_ready.set()

    def take_agent_messages(self):
        received, sent = tuple(self._agent_inbox), tuple(self._agent_sent)
        self._agent_inbox.clear()
        self._agent_sent.clear()
        self._communication_state()
        return received, sent

    def has_agent_messages(self) -> bool:
        return bool(self._agent_inbox)

    def has_pending_input(self) -> bool:
        return bool(self._agent_inbox or self._host_answers or self._tool_updates or self.host_response_expired)

    def receive_tool_update(self, data, artifact_id):
        self._tool_updates.append((copy.deepcopy(data), artifact_id))
        self._communication_ready.set()

    def take_tool_updates(self):
        updates = tuple(self._tool_updates)
        self._tool_updates.clear()
        return updates

    def communication_interrupt(self) -> dict[str, Any] | None:
        messages = [m for m, _ in self._agent_inbox if m.kind != "reply"]
        related = {"request_ids": [m.request_id for m in messages if m.request_id is not None],
                   "message_ids": [m.message_id for m in messages]}
        if self._communication_interrupt is not None:
            return {"reason": self._communication_interrupt, **related}
        if self._tool_updates:
            return {"reason": "tool_request_updated", **related}
        if messages:
            reason = ("superseded_by_parent_instruction" if any(m.kind == "instruction" for m in messages) else
                      "incoming_request" if any(m.kind == "question" for m in messages) else "incoming_message")
            return {"reason": reason, **related}
        return None

    def interrupt_communication_wait(self, reason: str) -> None:
        self._communication_interrupt = reason
        self._communication_ready.set()

    def clear_communication_interrupt(self) -> None:
        self._communication_interrupt = None

    async def wait_communication(self) -> None:
        await self._communication_ready.wait()

    def prepare_communication_wait(self) -> None:
        self._communication_ready.clear()

    def trace_communication(self, kind: str, data: dict[str, Any], *, tool_ref=None) -> str:
        tool_ref = tool_ref or {}
        refs = (tool_ref["request_artifact_id"],) if tool_ref.get("request_artifact_id") else ()
        artifact_id = self.add_artifact(kind, {**data, "source": {"task_id": self.task_id, **tool_ref}},
                                        operation_id=tool_ref.get("tool_execution_id"), references=refs)
        self.emit(kind, {key: value for key, value in data.items() if key != "content"},
                  artifact_ids=(artifact_id,))
        return artifact_id

    async def wait_reply(self, request_id: str, *, tool_ref=None) -> dict[str, Any]:
        request = self.request(request_id)
        if request.message.from_task_id != self.task_id:
            raise CommunicationError("only the requester may wait for this reply")
        target = request.message.to_task_id
        parent_reply = target == self.task.parent_task_id
        self.update_state(agent={"activity": AgentActivity.WAITING_FOR_PARENT_REPLY if parent_reply else
                                             AgentActivity.WAITING_FOR_CHILD_REPLY,
                                 "waiting_for": "parent_reply" if parent_reply else "child_reply",
                                 "waiting_request_id": request_id, "waiting_task_ids": (target,) if target else ()})
        self.trace_communication("agent_wait_started", {"request_id": request_id,
            "task_refs": [request.reference]}, tool_ref=tool_ref)
        outcome = {"reason": "wait_cancelled", "request_id": request_id}
        try:
            while True:
                request = self.request(request_id)
                if request.status != "pending":
                    outcome = {"reason": request.status, "request_id": request_id,
                               "request_ids": [request_id], "reply": request.reply.to_dict() if request.reply else None}
                    return outcome
                incoming = self.communication_interrupt()
                if incoming is not None:
                    outcome = {**incoming, "request_id": request_id, "pending": True}
                    return outcome
                self.prepare_communication_wait()
                await self.wait_communication()
        finally:
            self.update_state(agent={"activity": AgentActivity.TOOL_EXECUTION, "waiting_for": None,
                                     "waiting_request_id": None, "waiting_task_ids": ()})
            self.trace_communication("agent_wait_finished", {
                **{k: v for k, v in outcome.items() if k != "reply"}, "task_refs": [request.reference]},
                tool_ref=tool_ref)
