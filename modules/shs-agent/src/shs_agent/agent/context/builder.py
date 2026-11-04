"""Pure composition of one LLM input from detached task state."""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict
from typing import Any

from ..types import AgentLimits
from .conversation import bounded_messages, serialize
from .state import ContextSnapshot
from .. import prompts


class ContextBuilder:
    def __init__(self, limits: AgentLimits):
        self._max_tool_output_chars = limits.max_tool_output_chars

    def build(self, state: ContextSnapshot) -> list[dict[str, Any]]:
        conversation, summary = state.conversation, state.summary
        result = copy.deepcopy(list(conversation.messages[:conversation.initial_count]))
        if state.prompt_state is not None:
            blocks = self.blocks(state)
            result[0] = {"role": "system", "content": "\n\n".join(b["content"] for b in blocks)}
        # Generated data stays at assistant priority, never inside the system prompt.
        if summary.content:
            result.append({"role": "assistant", "content": prompts.SUMMARY_DATA.format(
                upto=summary.summarized_upto, content=summary.content)})
        if state.memos:
            result.append({"role": "assistant", "content": prompts.MEMO_DATA.format(
                content=serialize([asdict(memo) for memo in state.memos]))})
        if state.plan is not None:
            result.append({"role": "assistant", "content": prompts.PLAN_DATA.format(content=serialize(state.plan.to_dict()))})
        if state.tool_state is not None:
            result.append({"role": "assistant", "content": prompts.TOOL_DATA.format(content=serialize(state.tool_state))})
        # Reinsert folded Host instructions once; recent ones retain their recorded order.
        result.extend(copy.deepcopy([
            conversation.messages[index] for index in sorted(set(
                (*conversation.instruction_indices, *conversation.communication_indices)))
            if index < summary.summarized_upto
        ]))
        result.extend(bounded_messages(conversation.messages[summary.summarized_upto:],
                                       self._max_tool_output_chars))
        # Several chat templates interpret a final assistant data block as a
        # prefill. Explicitly request the next turn without changing raw history.
        if state.prompt_state is not None and result[-1]["role"] == "assistant":
            result.append({"role": "user", "content": prompts.CONTINUE_TASK})
        return result

    def blocks(self, state: ContextSnapshot) -> list[dict[str, Any]]:
        values = state.prompt_state or {}
        blocks = [("base", state.conversation.messages[0]["content"])]
        if values.get("role") or values.get("result_requirements"):
            blocks.append(("role", prompts.ROLE_BLOCK.format(**values)))
        blocks.append(("parent", prompts.RELATION_BLOCK.format(**values)))
        schemas = values.get("tools", [])
        blocks.append(("tools", prompts.TOOLS_BLOCK.format(
            tools="\n".join(f"- {s['name']}: {s['description']}" for s in schemas))))
        if any(s["name"] == "plan_read" for s in schemas):
            blocks.append(("planning", prompts.PLANNING_BLOCK))
        if values.get("workspace") is not None:
            blocks.append(("workspace", prompts.WORKSPACE_BLOCK.format(
                workspace=json.dumps(values["workspace"], ensure_ascii=False))))
        return [{"id": name, "content": content,
                 "content_id": hashlib.sha256(content.encode()).hexdigest()} for name, content in blocks]
