from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _nonempty(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    status: str = "created"
    namespace: str = "default"
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        for name in ("task_id", "status", "namespace"):
            _nonempty(getattr(self, name), name)


@dataclass(frozen=True)
class StorageRecord:
    task_id: str
    kind: str
    payload: dict[str, Any]

    namespace: str = "default"
    record_id: str = field(default_factory=lambda: uuid4().hex)

    message_index: int | None = None

    # IDs of other stored records.
    source_refs: tuple[str, ...] = ()

    # Range in the original messages: [start, end).
    source_range: tuple[int, int] | None = None

    search_text: str | None = None
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        for name in ("task_id", "kind", "namespace", "record_id"):
            _nonempty(getattr(self, name), name)

        if not isinstance(self.payload, dict):
            raise TypeError("payload must be a dict")

        serialized = json.dumps(
            self.payload,
            ensure_ascii=False,
            allow_nan=False,
        )

        if self.message_index is not None and (
            type(self.message_index) is not int
            or self.message_index < 0
        ):
            raise ValueError("message_index must be >= 0")

        if self.kind == "message" and self.message_index is None:
            raise ValueError("message requires message_index")

        if self.source_range is not None:
            span = self.source_range
            if (
                len(span) != 2
                or not all(type(x) is int for x in span)
                or not 0 <= span[0] < span[1]
            ):
                raise ValueError("invalid source_range")

        if not isinstance(self.source_refs, (tuple, list)) or not all(
            isinstance(x, str) and x for x in self.source_refs
        ):
            raise ValueError("invalid source_refs")

        object.__setattr__(
            self, "source_refs", tuple(self.source_refs)
        )

        if self.search_text is None:
            object.__setattr__(
                self, "search_text", serialized[:8000]
            )
        elif not isinstance(self.search_text, str):
            raise TypeError("search_text must be str or None")
