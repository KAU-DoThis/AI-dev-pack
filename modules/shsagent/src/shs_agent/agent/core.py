"""Use the existing LLM interface to make a single decision."""
from __future__ import annotations

import asyncio
import copy
import inspect
import json
from typing import Any, Callable, Protocol


class LLMProvider(Protocol):
    def chat(self, model: str, messages: list[dict[str, Any]],
             tools: list[dict[str, Any]] | None = None,
             options: Any = None) -> Any: ...

# for the session tools
class ToolRegistryLike(Protocol):
    def schemas(self) -> list[dict[str, Any]]: ...

class ToolExecutorLike(Protocol):
    def execute(self, name: str, arguments: dict[str, Any]) -> Any: ...


async def invoke(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run synchronous I/O in a thread; forced thread termination is not guaranteed.

    Await awaitables returned by async functions or sync functions on the event loop.
    Functions bound to a UI thread or uncooperative CPU work require a separate adapter.
    """
    if inspect.iscoroutinefunction(function):
        result = function(*args, **kwargs)
    else:
        result = await asyncio.to_thread(function, *args, **kwargs)
    return await result if inspect.isawaitable(result) else result


class AgentCore:
    def __init__(self, provider: LLMProvider):
        self.provider = provider

    async def step(self, *, model: str, messages: list[dict[str, Any]],
                   tools: list[dict[str, Any]], options: Any = None) -> Any:
        response = await invoke(
            self.provider.chat,
            model=model,
            messages=copy.deepcopy(messages),
            tools=copy.deepcopy(tools),
            options=copy.deepcopy(options),
        )
        # Require only a structure compatible with the existing LLMResponse/ToolCall.
        if not hasattr(response, "content") or not hasattr(response, "tool_calls"):
            raise TypeError("provider must return an LLMResponse-like object")
        if response.content is not None and not isinstance(response.content, str):
            raise TypeError("response.content must be str or None")
        if not isinstance(response.tool_calls, (list, tuple)):
            raise TypeError("response.tool_calls must be a sequence")
        ids: set[str] = set()
        for call in response.tool_calls:
            if not isinstance(call.id, str) or not call.id or call.id in ids:
                raise ValueError("tool call ids must be nonempty and unique in a response")
            ids.add(call.id)
            if not isinstance(call.name, str) or not call.name:
                raise ValueError("tool name must be a nonempty string")
            if not isinstance(call.arguments, dict):
                raise TypeError("tool arguments must be an object")
            if not all(isinstance(key, str) for key in call.arguments):
                raise TypeError("tool argument keys must be strings")
            json.dumps(call.arguments, allow_nan=False)
        return copy.deepcopy(response)
