"""One in-memory event journal. TaskChannel owns notification and subscription."""
from __future__ import annotations

import copy
from typing import Any
from uuid import uuid4

from .models import AgentEvent, TraceArtifact, TracerSnapshot, utc_timestamp
from .serialization import capture


class Tracer:
    """Append actual observations in sequence order on the owning asyncio loop.

    Records are never dropped or duplicated into per-subscriber queues. Memory
    grows with the task history; persistence and retention policies are external.
    """

    def __init__(self, task_id: str):
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("task_id must be a nonempty string")
        self.task_id = task_id
        self._events: list[AgentEvent] = []
        self._closed = False
        self._artifacts: dict[str, TraceArtifact] = {}

    def __len__(self) -> int:
        return len(self._events)

    @property
    def closed(self) -> bool:
        return self._closed

    def record(self, kind: str, payload: dict[str, Any], *, step: int = 0,
               artifact_ids: tuple[str, ...] = ()) -> AgentEvent:
        if self._closed:
            raise RuntimeError("cannot record after the tracer has closed")
        if type(step) is not int or step < 0:
            raise ValueError("step must be an integer >= 0")
        for artifact_id in artifact_ids:
            if artifact_id not in self._artifacts:
                raise ValueError("publish artifacts before referencing them")
        event = AgentEvent(self.task_id, len(self._events) + 1, kind,
                           copy.deepcopy(payload), step=step, artifact_ids=artifact_ids)
        self._events.append(event)
        return copy.deepcopy(event)

    def read(self, index: int) -> AgentEvent:
        """Return an independent event at a zero-based journal index."""
        return copy.deepcopy(self._events[index])

    def close(self) -> None:
        self._closed = True

    def add_artifact(self, kind: str, data: Any, *, operation_id: str | None = None,
                     credentials: bool = False,
                     captured: tuple[Any, tuple[dict[str, str], ...]] | None = None,
                     references: tuple[str, ...] = ()) -> str:
        if self._closed:
            raise RuntimeError("cannot record after the tracer has closed")
        if any(reference not in self._artifacts for reference in references):
            raise ValueError("record referenced artifacts first")
        value, issues = captured if captured is not None else capture(data, credentials=credentials)
        artifact_id = uuid4().hex
        self._artifacts[artifact_id] = TraceArtifact(
            artifact_id, self.task_id, kind, utc_timestamp(), value, operation_id, issues, references,
        )
        return artifact_id

    def artifact(self, artifact_id: str) -> TraceArtifact:
        return copy.deepcopy(self._artifacts[artifact_id])

    def snapshot(self) -> TracerSnapshot:
        return TracerSnapshot(self.task_id, tuple(self._events), complete=self._closed,
                              artifacts=self._artifacts)
