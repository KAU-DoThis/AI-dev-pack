"""One provider decision, isolated inputs and validated common response."""
from __future__ import annotations

import copy
import json
from typing import Any

from shs_agent.llm import LLMResponse, ToolCall
from shs_agent.llm.observation import Observer, common_response, observation_scope, observe
from shs_agent.llm.errors import ContextOverflow

from ._async import invoke
from .types import LLMProvider, nonempty


class AgentCore:
    def __init__(self, provider: LLMProvider):
        if not callable(getattr(provider, "chat", None)):
            raise TypeError("provider must provide chat()")
        self.provider = provider

    async def step(self, *, model: str, messages: list[dict[str, Any]],
                   tools: list[dict[str, Any]], options: Any = None,
                   observer: Observer | None = None) -> LLMResponse:
        nonempty(model, "model")
        request = {"model": model, "messages": copy.deepcopy(messages),
                   "tools": copy.deepcopy(tools), "options": copy.deepcopy(options)}
        
        with observation_scope(observer):
            observe("common_request", request)
            response = await invoke(self.provider.chat, **request)
            observe("common_response", lambda: common_response(response))

        normalized = self._normalize(response)

        if observer is not None:
            observer("normalized_response", normalized)
        if normalized.finish_reason in {"context_length_exceeded", "context_window_exceeded"}:
            raise ContextOverflow("Provider returned " + normalized.finish_reason)

        return normalized

    def _normalize(self, response: Any) -> LLMResponse:
        """
        Validate and normalize a provider response to a common LLMResponse.
        """
        if not hasattr(response, "content") or not hasattr(response, "tool_calls"):
            raise TypeError("provider must return an LLMResponse-like object")

        for name in ("content", "finish_reason", "thinking"):
            value = getattr(response, name, None)
            if value is not None and not isinstance(value, str):
                raise TypeError(f"response.{name} must be str or None")

        if not isinstance(response.tool_calls, (list, tuple)):
            raise TypeError("response.tool_calls must be a sequence")

        calls = []
        ids: set[str] = set()
        for call in response.tool_calls:
            nonempty(call.id, "tool call id")
            nonempty(call.name, "tool name")
            if call.id in ids:
                raise ValueError("tool call ids must be unique in a response")
            ids.add(call.id)
            if not isinstance(call.arguments, dict) or not all(
                isinstance(key, str) for key in call.arguments
            ):
                raise TypeError("tool arguments must be an object with string keys")
            json.dumps(call.arguments, allow_nan=False)
            calls.append(ToolCall(call.name, copy.deepcopy(call.arguments), call.id))

        return LLMResponse(
            content=response.content, tool_calls=calls,
            finish_reason=getattr(response, "finish_reason", None),
            thinking=getattr(response, "thinking", None),
            usage=copy.deepcopy(getattr(response, "usage", None)),
        )
