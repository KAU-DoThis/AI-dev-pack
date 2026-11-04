"""Bridge existing sync providers/executors and asynchronous host adapters."""
from __future__ import annotations

import asyncio
import inspect
from typing import Any, Callable


async def invoke(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    # A sync adapter can itself return an awaitable (e.g. ToolExecutor).
    if inspect.iscoroutinefunction(function):
        result = function(*args, **kwargs)
    else:
        result = await asyncio.to_thread(function, *args, **kwargs)
    return await result if inspect.isawaitable(result) else result
