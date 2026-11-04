"""Compaction boundaries and requests, independent of provider execution."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Sequence

from shs_agent.llm import LLMResponse

from ..types import AgentLimits
from .conversation import ConversationSnapshot, bounded_messages
from ..prompts import SUMMARY_SYSTEM, SUMMARY_TRUNCATED


SUMMARY_PROMPT = SUMMARY_SYSTEM


@dataclass(frozen=True)
class CompactionPlan:
    start: int
    upto: int
    before_chars: int
    messages: list[dict[str, Any]]

    def event_payload(self) -> dict[str, int]:
        return {"from_message": self.start, "to_message": self.upto,
                "folded_messages": self.upto - self.start,
                "context_chars_before": self.before_chars}


@dataclass(frozen=True)
class Summary:
    content: str
    summarized_upto: int


class Compaction:
    """Own Summary state and boundaries; Runtime performs the requested inference."""

    def __init__(self, initial_count: int, limits: AgentLimits):
        self._limits = limits
        self._summary = Summary("", initial_count)
        self._count = 0

    @property
    def summary(self) -> Summary:
        return self._summary

    @property
    def count(self) -> int:
        return self._count

    def plan(self, conversation: ConversationSnapshot, *, context_chars: int) -> CompactionPlan | None:
        threshold = self._limits.context_compact_threshold_chars
        if threshold is None or context_chars <= threshold:
            return None
        start = self._summary.summarized_upto
        upto = choose_compaction_upto(
            conversation.messages, start=start,
            keep_recent_messages=self._limits.context_keep_recent_messages,
        )
        if upto <= start:
            return None
        return CompactionPlan(start, upto, context_chars, summary_messages(
            self._summary.content, bounded_messages(conversation.messages[start:upto],
                                                     self._limits.max_tool_output_chars),
        ))

    def apply(self, plan: CompactionPlan, response: LLMResponse) -> dict[str, Any]:
        if plan.start != self._summary.summarized_upto:
            raise ValueError("stale context compaction plan")
        if response.finish_reason in {"length", "max_tokens", "content_filter"}:
            raise ValueError(f"context summary incomplete: {response.finish_reason}")
        if response.tool_calls or not response.content or not response.content.strip():
            raise ValueError("context summarizer must return nonempty text without tool calls")
        text = response.content.strip()
        limit = self._limits.context_summary_max_chars
        truncated = len(text) > limit
        if truncated:
            suffix = SUMMARY_TRUNCATED
            text = text[:limit - len(suffix)] + suffix
        self._summary = Summary(text, plan.upto)
        self._count += 1
        return {**plan.event_payload(), "summarized_upto": plan.upto,
                "summary_chars": len(text), "summary_truncated": truncated}

    def recovery_plan(self, conversation: ConversationSnapshot, *, context_chars: int,
                      budget_chars: int) -> CompactionPlan | None:
        """One bounded chunk of complete groups; raw records remain untouched.

        A confirmed overflow may require folding even the most recent completed
        group. Mandatory inputs/unanswered messages are separately reinserted by
        the Builder. A group that cannot fit is an explicit recovery failure.
        """
        start = self._summary.summarized_upto
        best = None
        for end in range(start + 1, len(conversation.messages) + 1):
            upto = choose_compaction_upto(conversation.messages[:end], start=start, keep_recent_messages=0)
            if upto != end:
                continue
            messages = summary_messages(self._summary.content, bounded_messages(
                conversation.messages[start:upto],
                max(256, min(self._limits.max_tool_output_chars, budget_chars // 4)),
            ))
            if estimate_context_chars(messages, []) > budget_chars:
                break
            best = CompactionPlan(start, upto, context_chars, messages)
        return best


def estimate_context_chars(messages: Sequence[dict[str, Any]],
                           tools: Sequence[dict[str, Any]]) -> int:
    """A provider-independent heuristic, not a tokenizer or a hard context limit."""
    return len(json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False))


def choose_compaction_upto(messages: Sequence[dict[str, Any]], *, start: int,
                           keep_recent_messages: int) -> int:
    """Never divide an assistant tool batch and its corresponding result messages."""
    boundaries = [start]
    index = start
    while index < len(messages):
        calls = messages[index].get("tool_calls", ())
        end = index + 1
        if calls:
            expected = {call["id"] for call in calls}
            seen: set[str] = set()
            while end < len(messages) and messages[end].get("role") == "tool":
                seen.add(messages[end].get("tool_call_id"))
                end += 1
            if expected != seen:
                break  # incomplete or malformed groups always remain verbatim
        boundaries.append(end)
        index = end
    target = len(messages) - keep_recent_messages
    return max((boundary for boundary in boundaries if boundary <= target), default=start)


def summary_messages(previous_summary: str,
                     messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": SUMMARY_PROMPT},
        {"role": "user", "content": json.dumps({
            "previous_summary": previous_summary or None, "messages_to_fold_in": messages,
        }, ensure_ascii=False)},
    ]
