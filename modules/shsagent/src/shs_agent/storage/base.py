
from __future__ import annotations

from typing import Protocol

from .model import StorageRecord, TaskRecord


class Storage(Protocol):

    async def initialize(self) -> None: ...

    async def create_task(
        self,
        task: TaskRecord,
    ) -> None: ...

    async def get_task(
        self,
        namespace: str,
        task_id: str,
    ) -> TaskRecord | None: ...

    async def update_task_status(
        self,
        namespace: str,
        task_id: str,
        status: str,
    ) -> None: ...

    async def append(
        self,
        record: StorageRecord,
    ) -> None: ...

    async def get(
        self,
        namespace: str,
        task_id: str,
        record_id: str,
    ) -> StorageRecord | None: ...

    async def read_messages(
        self,
        namespace: str,
        task_id: str,
        start: int = 0,
        limit: int = 100,
    ) -> list[StorageRecord]: ...

    async def search(
        self,
        namespace: str,
        task_id: str,
        query: str,
        limit: int = 20,
    ) -> list[StorageRecord]: ...

    async def close(self) -> None: ...
