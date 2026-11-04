"""Public handle; it has no dependency on a concrete Runtime or Context."""
from __future__ import annotations

import asyncio
import copy
from typing import TYPE_CHECKING, AsyncIterator, Awaitable, Callable

if TYPE_CHECKING:
    from ..subagent.manager import SubagentManager

from ..types import AgentEvent, TaskResult, TaskStatus, nonempty
from ..state import SessionState
from ..tracer import TraceArtifact, TracerSnapshot
from .channel import TaskChannel


class TaskSession:
    """Created by ProblemSolver, used from a single asyncio event loop.

    Command return values acknowledge receipt. Observe command_applied or
    command_rejected for application. Events can be replayed for this object's
    lifetime; each result/event consumer receives its own snapshot.
    """

    def __init__(self, channel: TaskChannel, run: Callable[[], Awaitable[TaskResult]], *,
                 subagents: SubagentManager | None = None, tool_access=None, tools=None):
        self._channel = channel
        self._run = run
        self._task: asyncio.Task[TaskResult] | None = None
        self._start_failure: TaskResult | None = None
        self._subagents = subagents
        self._tool_access = tool_access
        self._tools = tools

    @property
    def subagents(self) -> SubagentManager | None:
        """Host's whole-tree manager on an enabled root; use child(id) for descendants."""
        return self._subagents

    @property
    def task_id(self) -> str:
        return self._channel.task_id

    @property
    def status(self) -> TaskStatus:
        return self._channel.status

    @property
    def done(self) -> bool:
        return self._channel.done

    async def start(self) -> None:
        """Start once; repeated calls never restart a completed task."""
        self._channel.check_loop()
        if self._task is None and self._start_failure is None:
            coroutine = self._run()
            try:
                self._task = asyncio.create_task(coroutine, name=f"agent-{self.task_id}")
            except BaseException:
                coroutine.close()
                raise

    def set_start_failure(self, result: TaskResult) -> None:
        """Assembly-owner recovery after registration but before execution scheduling."""
        if self._task is not None or not self.done or result.task_id != self.task_id:
            raise RuntimeError("start failure requires this closed, unscheduled session")
        self._start_failure = copy.deepcopy(result)

    async def result(self) -> TaskResult:
        self._channel.check_loop()
        if self._start_failure is not None:
            return copy.deepcopy(self._start_failure)
        if self._task is None:
            raise RuntimeError("call await session.start() before result()")
        return copy.deepcopy(await asyncio.shield(self._task))

    async def events(self, *, after_sequence: int = 0, scope: str = "task") -> AsyncIterator:
        """Task events or tree envelopes with a separate monotonic tree_sequence."""
        self._channel.check_loop()
        if scope not in {"task", "tree"}:
            raise ValueError("scope must be task or tree")
        source = self._channel.events(after_sequence) if scope == "task" else self._channel.journal.events(after_sequence)
        async for event in source:
            yield event

    async def state(self) -> SessionState:
        """Atomically read current small state and the cursor for subsequent events."""
        self._channel.check_loop()
        return self._channel.state()

    async def artifact(self, artifact_id: str) -> TraceArtifact:
        """Read an independent detail copy, including while an operation is in flight."""
        self._channel.check_loop()
        return self._channel.artifact(artifact_id)

    async def trace(self) -> TracerSnapshot:
        """Detached trace at this instant; complete only when this task's log closed."""
        self._channel.check_loop()
        return self._channel.tracer_snapshot()

    async def requests(self):
        """Detached parent/child communication requests, including resolved ones."""
        self._channel.check_loop()
        return copy.deepcopy(self._channel.requests())

    async def ask(self, message: str) -> str:
        """Host question; reply is observable via requests() and parent_reply events."""
        return self._channel.ask_from_host(message)

    async def tool_state(self):
        """Read applied tools, last offered revision, and requests known to this task."""
        return self._tool_access.state()

    async def resource_state(self):
        return self._tool_access.state()

    async def grant_workspace(self, *, request_id, workspace):
        return self._tool_access.grant_workspace(request_id, workspace)

    async def supply_tool(self, *, request_id, tool, redelegate=False):
        """Host supplies a new Tool; executable only after the next batch boundary."""
        return self._tool_access.supply_tool(request_id, tool, redelegate=redelegate)

    async def deny_resource(self, *, request_id, reason):
        return self._tool_access.deny(request_id, reason)

    async def workspace(self):
        self._channel.check_loop()
        return self._tools.workspace.snapshot() if self._tools and self._tools.workspace else None

    async def grant_tool(self, *, request_id: str, tool_id: str):
        """Host decision only; returns approved, not applied. Never waits for a batch."""
        if self._tool_access is None:
            raise ValueError("tool integration was not enabled with host_tools")
        return self._tool_access.grant(request_id, tool_id)

    async def deny_tool(self, *, request_id: str, reason: str):
        if self._tool_access is None:
            raise ValueError("tool integration was not enabled with host_tools")
        return self._tool_access.deny(request_id, reason)

    async def send_instruction(self, content: str, *, message_id: str | None = None) -> str:
        nonempty(content, "content")
        return self._channel.send("instruction", {"content": content}, message_id)

    async def answer(self, question_id: str | None = None, text: str | None = None, *,
                     request_id: str | None = None, content: str | None = None,
                     message_id: str | None = None) -> str:
        """Answer one question. request_id/content are prototype keyword aliases."""
        if question_id is not None and request_id is not None:
            raise TypeError("use question_id or request_id, not both")
        if text is not None and content is not None:
            raise TypeError("use text or content, not both")
        question_id = question_id if question_id is not None else request_id
        text = text if text is not None else content
        nonempty(question_id, "question_id")
        nonempty(text, "text")
        return self._channel.send("answer", {"question_id": question_id, "text": text}, message_id)

    async def cancel(self, *, message_id: str | None = None) -> str:
        """Cooperative cancellation; an in-flight external operation is awaited."""
        return self._channel.send("cancel", {}, message_id)
