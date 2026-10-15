
"""SQL Storage implementation independent of database type."""

from __future__ import annotations

import asyncio

from sqlalchemy import (
    Column,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Engine

from ..model import StorageRecord, TaskRecord


metadata = MetaData()

_tasks = Table(
    "agent_tasks",
    metadata,
    Column("namespace", String(128), primary_key=True),
    Column("task_id", String(64), primary_key=True),
    Column("status", String(32), nullable=False),
    Column("created_at", String(40), nullable=False),
)

_records = Table(
    "agent_records",
    metadata,
    Column("row_id", Integer, primary_key=True, autoincrement=True),
    Column("record_id", String(64), nullable=False, unique=True),
    Column("namespace", String(128), nullable=False),
    Column("task_id", String(64), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("message_index", Integer),
    Column("payload", JSON, nullable=False),
    Column("source_refs", JSON, nullable=False),
    Column("source_start", Integer),
    Column("source_end", Integer),
    Column("search_text", Text, nullable=False),
    Column("created_at", String(40), nullable=False),
    ForeignKeyConstraint(
        ["namespace", "task_id"],
        ["agent_tasks.namespace", "agent_tasks.task_id"],
    ),
    UniqueConstraint(
        "namespace", "task_id", "kind", "message_index",
        name="uq_agent_message_index",
    ),
)

Index(
    "ix_agent_records_task",
    _records.c.namespace,
    _records.c.task_id,
    _records.c.row_id,
)


def _record_from_row(row) -> StorageRecord:
    span = (
        (row["source_start"], row["source_end"])
        if row["source_start"] is not None
        else None
    )

    return StorageRecord(
        namespace=row["namespace"],
        task_id=row["task_id"],
        record_id=row["record_id"],
        kind=row["kind"],
        payload=row["payload"],
        message_index=row["message_index"],
        source_refs=tuple(row["source_refs"]),
        source_range=span,
        search_text=row["search_text"],
        created_at=row["created_at"],
    )


class SQLStorage:
    def __init__(self, engine: Engine):
        self.engine = engine

    async def initialize(self) -> None:
        def job():
            with self.engine.begin() as conn:
                metadata.create_all(conn)

        await asyncio.to_thread(job)

    async def create_task(self, task: TaskRecord) -> None:
        def job():
            with self.engine.begin() as conn:
                conn.execute(insert(_tasks).values(
                    namespace=task.namespace,
                    task_id=task.task_id,
                    status=task.status,
                    created_at=task.created_at,
                ))

        await asyncio.to_thread(job)

    async def get_task(
        self, namespace: str, task_id: str
    ) -> TaskRecord | None:
        def job():
            with self.engine.connect() as conn:
                row = conn.execute(
                    select(_tasks).where(
                        _tasks.c.namespace == namespace,
                        _tasks.c.task_id == task_id,
                    )
                ).mappings().first()

                return TaskRecord(**row) if row else None

        return await asyncio.to_thread(job)

    async def update_task_status(
        self, namespace: str, task_id: str, status: str
    ) -> None:
        def job():
            with self.engine.begin() as conn:
                result = conn.execute(
                    update(_tasks).where(
                        _tasks.c.namespace == namespace,
                        _tasks.c.task_id == task_id,
                    ).values(status=status)
                )
                if result.rowcount == 0:
                    raise KeyError(
                        f"unknown task: {namespace}/{task_id}"
                    )

        await asyncio.to_thread(job)

    async def append(self, record: StorageRecord) -> None:
        def job():
            span = record.source_range

            with self.engine.begin() as conn:
                conn.execute(insert(_records).values(
                    namespace=record.namespace,
                    task_id=record.task_id,
                    record_id=record.record_id,
                    kind=record.kind,
                    payload=record.payload,
                    message_index=record.message_index,
                    source_refs=list(record.source_refs),
                    source_start=span[0] if span else None,
                    source_end=span[1] if span else None,
                    search_text=record.search_text,
                    created_at=record.created_at,
                ))

        await asyncio.to_thread(job)

    async def get(
        self, namespace: str, task_id: str, record_id: str
    ) -> StorageRecord | None:
        def job():
            with self.engine.connect() as conn:
                row = conn.execute(
                    select(_records).where(
                        _records.c.namespace == namespace,
                        _records.c.task_id == task_id,
                        _records.c.record_id == record_id,
                    )
                ).mappings().first()

                return _record_from_row(row) if row else None

        return await asyncio.to_thread(job)

    async def read_messages(
        self,
        namespace: str,
        task_id: str,
        start: int = 0,
        limit: int = 100,
    ) -> list[StorageRecord]:

        if (
            type(start) is not int
            or start < 0
            or type(limit) is not int
            or not 1 <= limit <= 1000
        ):
            raise ValueError("invalid start or limit")

        def job():
            with self.engine.connect() as conn:
                rows = conn.execute(
                    select(_records).where(
                        _records.c.namespace == namespace,
                        _records.c.task_id == task_id,
                        _records.c.kind == "message",
                        _records.c.message_index >= start,
                    )
                    .order_by(_records.c.message_index)
                    .limit(limit)
                ).mappings().all()

                return [_record_from_row(row) for row in rows]

        return await asyncio.to_thread(job)

    async def search(
        self,
        namespace: str,
        task_id: str,
        query: str,
        limit: int = 20,
    ) -> list[StorageRecord]:

        if not isinstance(query, str) or not query.strip():
            raise ValueError("query is required")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be 1..100")

        def job():
            with self.engine.connect() as conn:
                rows = conn.execute(
                    select(_records).where(
                        _records.c.namespace == namespace,
                        _records.c.task_id == task_id,
                        _records.c.search_text.icontains(
                            query, autoescape=True
                        ),
                    )
                    .order_by(_records.c.row_id.desc())
                    .limit(limit)
                ).mappings().all()

                return [_record_from_row(row) for row in rows]

        return await asyncio.to_thread(job)

    async def close(self) -> None:
        await asyncio.to_thread(self.engine.dispose)
