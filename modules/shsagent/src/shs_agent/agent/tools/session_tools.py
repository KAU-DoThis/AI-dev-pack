
"""Session tool view: external Registry plus task-specific tools.

External tools run through the existing ToolExecutor.
The host's Registry is not modified.
"""

from __future__ import annotations

import copy
from typing import Any, Awaitable, Callable

from ..core import ToolExecutorLike, ToolRegistryLike, invoke
from .schemas import (
    HOST_ASK_SCHEMA,
    MEMORY_WRITE_SCHEMA,
    CONVERSATION_SEARCH_SCHEMA,
    CONVERSATION_READ_SCHEMA,
    TASK_COMPLETE_SCHEMA,
)


class SessionTools:
    def __init__(
        self,
        registry: ToolRegistryLike,
        executor: ToolExecutorLike,
        host_ask: Callable[..., Awaitable[Any]],
        memory_write: Callable[..., Awaitable[Any]],
        conversation_search: Callable[..., Awaitable[Any]],
        conversation_read: Callable[..., Awaitable[Any]],
        task_complete: Callable[..., Awaitable[Any]],
    ):
        self.registry = registry
        self.executor = executor

        self._local = {
            "host_ask": (HOST_ASK_SCHEMA, host_ask),
            "memory_write": (MEMORY_WRITE_SCHEMA, memory_write),
            "conversation_search": (
                CONVERSATION_SEARCH_SCHEMA,
                conversation_search,
            ),
            "conversation_read": (
                CONVERSATION_READ_SCHEMA,
                conversation_read,
            ),
            "task_complete": (
                TASK_COMPLETE_SCHEMA, 
                task_complete
            ),
        }

        # Detect reserved-name conflicts before execution.
        self.schemas()

    @property
    def version(self) -> int | None:
        return getattr(self.registry, "version", None)

    def schemas(self) -> list[dict[str, Any]]:
        schemas = copy.deepcopy(self.registry.schemas())
        names: set[str] = set()

        for schema in schemas:
            name = schema["name"]

            if name in names or name in self._local:
                raise ValueError(
                    f"duplicate or reserved tool name: {name}"
                )

            names.add(name)

        return schemas + [
            copy.deepcopy(item[0])
            for item in self._local.values()
        ]

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> Any:
        if name in self._local:
            return await self._local[name][1](**arguments)

        return await invoke(
            self.executor.execute,
            name=name,
            arguments=copy.deepcopy(arguments),
        )
