"""Explicit task memos and their result snapshots; no long-term Memory storage."""
from __future__ import annotations

from uuid import uuid4

from ..types import AgentLimits, TaskMemo, nonempty


class Memo:
    def __init__(self, task_id: str, limits: AgentLimits):
        self.task_id = task_id
        self.limits = limits
        self._memos: list[TaskMemo] = []

    def write(self, content: str, kind: str, source_refs: list[str] | None,
              *, step: int) -> TaskMemo:
        nonempty(content, "content")
        nonempty(kind, "kind")
        if source_refs is not None and (not isinstance(source_refs, list)
                or not all(isinstance(ref, str) for ref in source_refs)):
            raise TypeError("source_refs must be a list of strings")
        if len(content) > self.limits.max_memo_item_chars:
            raise ValueError("memo content exceeds max_memo_item_chars")
        if sum(len(memo.content) for memo in self._memos) + len(content) > self.limits.max_task_memo_chars:
            raise ValueError("task memos exceed max_task_memo_chars")
        memo = TaskMemo(uuid4().hex, self.task_id, content, kind, tuple(source_refs or ()), step)
        self._memos.append(memo)
        return memo

    def snapshot(self) -> tuple[TaskMemo, ...]:
        # TaskMemo contains only immutable values, including tuple source references.
        return tuple(self._memos)
