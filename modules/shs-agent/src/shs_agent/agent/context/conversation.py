"""Original task records, stable indices and bounded copies for consumers."""
from __future__ import annotations

from .. import prompts

import copy
import json
from dataclasses import dataclass
from typing import Any, Sequence

from shs_agent.llm import LLMResponse, ToolCall

from ..types import AgentLimits, TaskRequest, integer, nonempty


def tool_error(kind: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"type": kind, "message": message}}


def serialize(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, default=repr)


def bounded_tool_content(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    envelope = {"ok": json.loads(text).get("ok", False), "truncated": True,
                "original_chars": len(text), "preview": text[:limit - 128]}
    encoded = serialize(envelope)
    while len(encoded) > limit:
        envelope["preview"] = envelope["preview"][:-max(1, len(encoded) - limit)]
        encoded = serialize(envelope)
    return encoded


def bounded_messages(messages: Sequence[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Copy records before bounding tool results; never truncate their originals."""
    result = copy.deepcopy(list(messages))
    for message in result:
        if message["role"] == "tool":
            message["content"] = bounded_tool_content(message["content"], limit)
    return result


@dataclass(frozen=True)
class ConversationSnapshot:
    """Detached records to consume read-only, including original index metadata.

    Message dictionaries are copies, not references into the owning Conversation.
    """

    messages: tuple[dict[str, Any], ...]
    initial_count: int
    instruction_indices: tuple[int, ...]
    communication_indices: tuple[int, ...] = ()


class Conversation:
    def __init__(self, request: TaskRequest, limits: AgentLimits):
        self.limits = limits
        self._messages: list[dict[str, Any]] = [
            {"role": "system", "content": request.system_prompt},
            {"role": "user", "content": request.instruction},
            *({"role": "user", "content": prompts.BACKGROUND_DATA + value} for value in request.context),
        ]
        self._initial_count = len(self._messages)
        self._instruction_indices: list[int] = []
        self._communication_indices: list[int] = []
        self._unseen_agent_indices: list[int] = []

    @property
    def initial_count(self) -> int:
        return self._initial_count

    def __len__(self) -> int:
        return len(self._messages)

    def snapshot(self) -> ConversationSnapshot:
        return ConversationSnapshot(tuple(copy.deepcopy(self._messages)), self._initial_count,
                                    tuple(self._instruction_indices), tuple(sorted(set(
                                        (*self._communication_indices, *self._unseen_agent_indices)))))

    def record_agent_message(self, message, *, outgoing: bool = False) -> int:
        index = len(self._messages)
        data = message.to_dict()
        data["direction"] = "sent" if outgoing else "received"
        self._messages.append({"role": "assistant" if outgoing else "user",
                               "content": prompts.COMMUNICATION_DATA
                                          + serialize(data)})
        if message.kind == "question":
            self._communication_indices.append(index)
        if not outgoing:
            self._unseen_agent_indices.append(index)
        if not outgoing and message.kind == "instruction":
            self._instruction_indices.append(index)
        return index

    def mark_agent_messages_seen(self, indices) -> None:
        seen = set(indices)
        self._unseen_agent_indices = [i for i in self._unseen_agent_indices if i not in seen]

    def resolve_agent_request(self, request_id: str) -> None:
        self._communication_indices = [i for i in self._communication_indices
            if json.loads(self._messages[i]["content"].split("\n", 1)[1])["request_id"] != request_id]

    def record_tool_update(self, data: dict[str, Any]) -> int:
        index = len(self._messages)
        self._messages.append({"role": "user", "content": prompts.TOOL_RESULT_DATA + serialize(data)})
        return index

    def record_host_answer(self, question_id: str, text: str) -> int:
        index = len(self._messages)
        self._messages.append({"role": "user", "content": prompts.HOST_ANSWER_DATA + serialize({
            "question_id": question_id, "content": text})})
        return index

    def add_instruction(self, content: str) -> None:
        nonempty(content, "content")
        self._instruction_indices.append(len(self._messages))
        self._messages.append({"role": "user", "content": content})

    def record_response(self, response: LLMResponse) -> int:
        index = len(self._messages)
        self._messages.append(copy.deepcopy(response.to_message()))
        return index

    def record_tool(self, call: ToolCall, outcome: dict[str, Any]) -> dict[str, Any]:
        try:
            content = serialize(outcome)
        except Exception as exc:
            content = serialize(tool_error("serialization_error", str(exc)))
        self._messages.append({
            "role": "tool", "name": call.name, "tool_call_id": call.id, "content": content,
        })
        return json.loads(content)

    def tool_view(self, outcome: dict[str, Any]) -> dict[str, Any]:
        return json.loads(bounded_tool_content(serialize(outcome), self.limits.max_tool_output_chars))

    def search(self, query: str, limit: int = 8, *, exclude: int | None = None) -> dict[str, Any]:
        nonempty(query, "query")
        integer(limit, "limit", 1)
        if limit > 20:
            raise ValueError("limit must be between 1 and 20")
        needle = query.strip().casefold()
        terms = needle.split()
        matches = []
        for index, message in enumerate(self._messages):
            if index == exclude:
                continue
            text = serialize(message)
            folded = text.casefold()
            position = folded.find(needle)
            if position >= 0:
                score = 1000 + folded.count(needle)
            elif all(term in folded for term in terms):
                position = min(folded.find(term) for term in terms)
                score = 100 + sum(folded.count(term) for term in terms)
            else:
                continue
            width = self.limits.conversation_search_snippet_chars
            start = max(0, min(position - width // 2, len(text) - width))
            matches.append((score, index, {
                "message_index": index, "role": message["role"], "name": message.get("name"),
                "match_offset": position, "snippet": text[start:start + width],
            }))
        matches.sort(key=lambda match: (-match[0], -match[1]))
        # Keep the existing retrieval payload and index contract.
        return {"query": query, "matches": [item[2] for item in matches[
                    :min(limit, self.limits.conversation_search_max_results)]],
                "total_matches": len(matches), "transcript_messages": len(self._messages),
                "searched_messages": len(self._messages) - int(exclude is not None),
                "original_transcript_preserved": True,
                "hint": "Use conversation_read(message_index=...) for exact original content."}

    def read(self, message_index: int, char_offset: int = 0, max_chars: int | None = None,
             *, exclude: int | None = None) -> dict[str, Any]:
        integer(message_index, "message_index")
        integer(char_offset, "char_offset")
        max_chars = self.limits.conversation_read_max_chars if max_chars is None else max_chars
        integer(max_chars, "max_chars", 256)
        if max_chars > 6000:
            raise ValueError("max_chars must be between 256 and 6000")
        if message_index >= len(self._messages):
            raise IndexError("message_index is outside the conversation")
        if message_index == exclude:
            raise ValueError("cannot read the currently executing conversation tool invocation")
        message = self._messages[message_index]
        text = serialize(message)
        if char_offset > len(text):
            raise ValueError("char_offset exceeds message length")
        stop = min(len(text), char_offset + min(max_chars, self.limits.conversation_read_max_chars))
        return {"message_index": message_index, "role": message["role"], "name": message.get("name"),
                "char_offset": char_offset, "next_char_offset": stop if stop < len(text) else None,
                "total_chars": len(text), "eof": stop == len(text),
                "serialized_message_chunk": text[char_offset:stop]}
