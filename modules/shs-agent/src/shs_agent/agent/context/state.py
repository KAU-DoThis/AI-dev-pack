"""Task-owned composition state; LLM input construction belongs to the Builder."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..types import AgentLimits, TaskMemo, TaskRequest
from ..planning import PlanSnapshot
from .compaction import Compaction, Summary
from .conversation import Conversation, ConversationSnapshot
from .memo import Memo


@dataclass(frozen=True)
class ContextSnapshot:
    """Detached read input for ContextBuilder, with no live services or mutators."""

    conversation: ConversationSnapshot
    memos: tuple[TaskMemo, ...]
    summary: Summary
    plan: PlanSnapshot | None = None
    tool_state: dict[str, Any] | None = None
    prompt_state: dict[str, Any] | None = None


class TaskContext:
    def __init__(self, task_id: str, request: TaskRequest, limits: AgentLimits):
        self.task_id = task_id
        self.request = request
        self.conversation = Conversation(request, limits)
        self.memo = Memo(task_id, limits)
        self.compaction = Compaction(self.conversation.initial_count, limits)

    def snapshot(self, *, plan: PlanSnapshot | None = None) -> ContextSnapshot:
        return ContextSnapshot(self.conversation.snapshot(), self.memo.snapshot(),
                               self.compaction.summary, plan)

    def result_fields(self) -> dict[str, Any]:
        return {"messages": self.conversation.snapshot().messages, "memos": self.memo.snapshot(),
                "context_summary": self.compaction.summary.content,
                "context_compactions": self.compaction.count}
