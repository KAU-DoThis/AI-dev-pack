"""Session API used by the host. Use it from a single asyncio event loop."""
from __future__ import annotations

import asyncio
import copy
from collections import deque
from typing import TYPE_CHECKING, Any, AsyncIterator
from uuid import uuid4

from .types import AgentEvent, HostCommand, TaskResult, TaskStatus

if TYPE_CHECKING:
    from .runtime import TaskRuntime


class TaskSession:
    def __init__(self) -> None:
        self.task_id = uuid4().hex
        self._status = TaskStatus.CREATED
        self._commands: deque[HostCommand] = deque()
        self._inbox_ready = asyncio.Event()
        self._events: list[AgentEvent] = []
        self._events_ready = asyncio.Event()
        self._accepted: dict[str, HostCommand] = {}
        self._runtime: TaskRuntime | None = None
        self._task: asyncio.Task[TaskResult] | None = None
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._emit("created", {})

    @property
    def status(self) -> TaskStatus:
        return self._status

    @property
    def done(self) -> bool:
        return self._closed

    def _check_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("a session must be used from one asyncio event loop")

    async def start(self) -> None:
        """Start execution and return. Use result() to wait for completion."""
        self._check_loop()
        if self._task is not None:
            return
        if self._runtime is None:
            raise RuntimeError("create this session through ProblemSolver.create_task()")
        self._task = asyncio.create_task(self._runtime.run(), name=f"agent-{self.task_id}")

    async def result(self) -> TaskResult:
        self._check_loop()
        if self._task is None:
            raise RuntimeError("call await session.start() before result()")
        # Prevent a result waiter's timeout or cancellation from cancelling the running Runtime.
        return copy.deepcopy(await asyncio.shield(self._task))

    async def send_instruction(self, content: str, *, message_id: str | None = None) -> str:
        self._nonempty(content, "content")
        return self._send("instruction", {"content": content}, message_id)

    async def answer(self, request_id: str, content: str, *,
                     message_id: str | None = None) -> str:
        self._nonempty(request_id, "request_id")
        self._nonempty(content, "content")
        return self._send("answer", {"request_id": request_id, "content": content}, message_id)

    async def cancel(self, *, message_id: str | None = None) -> str:
        """Request cooperative cancellation. Wait for running sync functions and network calls to finish."""
        return self._send("cancel", {}, message_id)

    def _send(self, kind: str, payload: dict[str, Any], message_id: str | None) -> str:
        self._check_loop()
        if message_id is None:
            message_id = uuid4().hex
        self._nonempty(message_id, "message_id")
        command = HostCommand(message_id, kind, copy.deepcopy(payload))
        previous = self._accepted.get(message_id)
        if previous is not None:
            if previous != command:
                raise ValueError("message_id was already used for different content")
            return message_id  # Repeated delivery of the same message is idempotent.
        if self._closed:
            raise RuntimeError("task is finished; create a new session")
        self._accepted[message_id] = command
        self._commands.append(command)
        self._emit("command_received", {"message_id": message_id, "command_type": kind})
        self._inbox_ready.set()
        return message_id

    async def events(self, *, after_sequence: int = 0) -> AsyncIterator[AgentEvent]:
        """Events can be replayed for the session lifetime; each consumer has an independent read position."""
        self._check_loop()
        if type(after_sequence) is not int or after_sequence < 0:
            raise ValueError("after_sequence must be a nonnegative integer")
        if after_sequence > len(self._events):
            raise ValueError("after_sequence is beyond the current event log")
        index = after_sequence
        while True:
            while index < len(self._events):
                event = copy.deepcopy(self._events[index])
                index += 1
                yield event
            if self._closed:
                return
            self._events_ready.clear()
            await self._events_ready.wait()

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        self._events.append(AgentEvent(self.task_id, len(self._events) + 1,
                                       kind, copy.deepcopy(payload)))
        self._events_ready.set()

    def _set_status(self, status: TaskStatus) -> None:
        if self._status != status:
            self._status = status
            self._emit("status_changed", {"status": status.value})

    @staticmethod
    def _nonempty(value: str, name: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a nonempty string")
