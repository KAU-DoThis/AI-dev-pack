"""Public task contracts and dependency protocols; no runtime state lives here."""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from .subagent.models import TaskTreeSnapshot

from .tracer.models import AgentEvent, TracerSnapshot
from .planning import PlanSnapshot
from .prompts import DEFAULT_SYSTEM


class LLMProvider(Protocol):
    """A sync or async adapter returning shs_agent.llm.LLMResponse."""

    def chat(self, model: str, messages: list[dict[str, Any]],
             tools: list[dict[str, Any]] | None = None, options: Any = None) -> Any: ...


class ToolRegistryLike(Protocol):
    def schemas(self) -> list[dict[str, Any]]: ...


class ToolExecutorLike(Protocol):
    def execute(self, name: str, arguments: dict[str, Any]) -> Any: ...


class TaskStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_FOR_HOST = "waiting_for_host"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    LIMIT_REACHED = "limit_reached"
    INCOMPLETE = "incomplete"

    @property
    def terminal(self) -> bool:
        return self in {self.COMPLETED, self.FAILED, self.CANCELLED,
                        self.LIMIT_REACHED, self.INCOMPLETE}


@dataclass(frozen=True)
class TaskRequest:
    instruction: str
    context: tuple[str, ...] = ()
    system_prompt: str = DEFAULT_SYSTEM
    role: str = ""
    result_requirements: str = ""

    def __post_init__(self) -> None:
        nonempty(self.instruction, "instruction")
        if isinstance(self.context, str):
            raise TypeError("context must be a sequence of strings")
        context = tuple(self.context)
        if not all(isinstance(item, str) for item in context):
            raise TypeError("context must be a sequence of strings")
        object.__setattr__(self, "context", context)
        if not isinstance(self.system_prompt, str):
            raise TypeError("system_prompt must be a string")
        if not isinstance(self.role, str) or not isinstance(self.result_requirements, str):
            raise TypeError("role and result_requirements must be strings")


@dataclass(frozen=True)
class AgentLimits:
    max_steps: int = 20
    max_tool_calls: int = 50
    max_tool_output_chars: int = 12000
    host_response_timeout: float | None = 300.0
    # Compatibility: an explicitly set threshold opts into legacy proactive compaction.
    # Default recovery is driven by a confirmed Provider context overflow.
    context_compact_threshold_chars: int | None = None
    context_window_tokens: int | None = None
    context_window_source: str = "host"
    context_keep_recent_messages: int = 12
    context_summary_max_chars: int = 12000
    max_memo_item_chars: int = 4000
    max_task_memo_chars: int = 24000
    conversation_search_max_results: int = 20
    conversation_search_snippet_chars: int = 600
    conversation_read_max_chars: int = 6000
    max_total_subagents: int | None = None
    max_subagent_depth: int | None = None

    def __post_init__(self) -> None:
        minimums = {
            "max_steps": 1, "max_tool_calls": 0, "max_tool_output_chars": 256,
            "context_keep_recent_messages": 1, "context_summary_max_chars": 128,
            "max_memo_item_chars": 64, "max_task_memo_chars": 128,
            "conversation_search_max_results": 1, "conversation_search_snippet_chars": 64,
            "conversation_read_max_chars": 256,
        }
        for name, minimum in minimums.items():
            integer(getattr(self, name), name, minimum)
        for name in ("max_total_subagents", "max_subagent_depth"):
            if getattr(self, name) is not None:
                integer(getattr(self, name), name)
        if self.context_compact_threshold_chars is not None:
            integer(self.context_compact_threshold_chars, "context_compact_threshold_chars", 256)
        if self.context_window_tokens is not None:
            integer(self.context_window_tokens, "context_window_tokens", 1)
        if self.max_memo_item_chars > self.max_task_memo_chars:
            raise ValueError("max_memo_item_chars cannot exceed max_task_memo_chars")
        if self.conversation_search_max_results > 20:
            raise ValueError("conversation_search_max_results cannot exceed 20")
        if self.conversation_read_max_chars > 6000:
            raise ValueError("conversation_read_max_chars cannot exceed 6000")
        timeout = self.host_response_timeout
        if timeout is not None and (isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("host_response_timeout must be positive or None")


@dataclass(frozen=True)
class TaskMemo:
    memo_id: str
    task_id: str
    content: str
    kind: str
    source_refs: tuple[str, ...] = ()
    created_step: int = 0


@dataclass(frozen=True)
class HostCommand:
    message_id: str
    type: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class TaskResult:
    task_id: str
    status: TaskStatus
    output: str
    stop_reason: str
    steps: int
    tool_calls: int
    tool_errors: int
    memos: tuple[TaskMemo, ...] = ()
    messages: tuple[dict[str, Any], ...] = ()
    context_summary: str = ""
    context_compactions: int = 0
    warnings: tuple[str, ...] = ()
    tracer: TracerSnapshot | None = None
    plan: PlanSnapshot | None = None
    workspace: dict[str, Any] | None = None
    tree: TaskTreeSnapshot | None = None


def nonempty(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")


def integer(value: int, name: str, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
