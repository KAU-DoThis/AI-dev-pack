"""Data-only execution events and detached, JSON-exportable snapshots."""
from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


STATE_EVENT_TYPES = frozenset({"created", "state_changed", "status_changed", "finished",
                               "plan_created", "plan_updated", "plan_revised"})


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class AgentEvent:
    # Keep the original four positional fields and the public import aliases.
    task_id: str
    sequence: int
    type: str
    payload: dict[str, Any]
    timestamp: str = field(default_factory=utc_timestamp)
    step: int = 0
    artifact_ids: tuple[str, ...] = ()

    @property
    def event_id(self) -> str:
        return f"{self.task_id}:{self.sequence}"

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "event_id": self.event_id}


@dataclass(frozen=True)
class TraceArtifact:
    artifact_id: str
    task_id: str
    kind: str
    timestamp: str
    data: Any
    operation_id: str | None = None
    issues: tuple[dict[str, str], ...] = ()
    references: tuple[str, ...] = ()

    @property
    def details_complete(self) -> bool:
        return not any(issue["category"] == "unavailable" for issue in self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "details_complete": self.details_complete}


@dataclass(frozen=True)
class TracerSnapshot:
    """Detached single-task data; nested payloads are consumer-owned copies.

    Events and artifacts form a self-contained record of observed boundaries.
    complete means the journal closed, independently of detail capture issues.
    No Session, Provider, Future or live Tracer is retained.
    """

    task_id: str
    events: tuple[AgentEvent, ...] = ()
    complete: bool = False
    schema_version: int = 2
    artifacts: dict[str, TraceArtifact] = field(default_factory=dict)
    recording_scope: dict[str, Any] = field(default_factory=lambda: {
        "requests": "AgentCore common requests; SDK keyword arguments when adapter hook is available",
        "responses": "declared common response fields and explicit adapter response projections",
        "tools": "SessionTools dispatch and executor return boundary",
        "planning": "applied plan revisions and their reasons; item states are agent assessments",
        "lifetime": "until journal close; late worker results after infrastructure cancellation are not collected",
        "excluded": ["connection credentials", "HTTP wire bytes", "undisclosed model reasoning",
                     "arbitrary SDK object internals"],
    })

    def __post_init__(self) -> None:
        object.__setattr__(self, "events", tuple(copy.deepcopy(self.events)))
        object.__setattr__(self, "artifacts", copy.deepcopy(self.artifacts))
        object.__setattr__(self, "recording_scope", copy.deepcopy(self.recording_scope))

    @property
    def details_complete(self) -> bool:
        return self.schema_version >= 2 and all(artifact.details_complete for artifact in self.artifacts.values())

    def artifact(self, artifact_id: str) -> TraceArtifact:
        return copy.deepcopy(self.artifacts[artifact_id])

    def state(self, *, at_sequence: int | None = None):
        """Reconstruct small state from actual state-bearing events, without executing work."""
        from ..state import SessionState
        cursor = len(self.events) if at_sequence is None else at_sequence
        if type(cursor) is not int or not 1 <= cursor <= len(self.events):
            raise ValueError("at_sequence must identify an event in this snapshot")
        for event in reversed(self.events[:cursor]):
            if "state" in event.payload and event.type in STATE_EVENT_TYPES:
                return SessionState.from_dict({**event.payload["state"], "last_event_sequence": cursor})
        raise ValueError("this snapshot has no state history (legacy schema)")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "task_id": self.task_id,
            "complete": self.complete,
            "events": [event.to_dict() for event in self.events],
            "artifacts": {key: artifact.to_dict() for key, artifact in self.artifacts.items()},
            "details_complete": self.details_complete,
            "recording_scope": copy.deepcopy(self.recording_scope),
        }

    def to_json(self, *, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, allow_nan=False, indent=indent)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> TracerSnapshot:
        if value.get("schema_version", 1) not in (1, 2):
            raise ValueError("unsupported tracer schema_version")
        events = []
        for item in value["events"]:
            data = dict(item)
            data.pop("event_id", None)
            data["artifact_ids"] = tuple(data.get("artifact_ids", ()))
            events.append(AgentEvent(**data))
        artifacts = {}
        for key, item in value.get("artifacts", {}).items():
            data = dict(item)
            data.pop("details_complete", None)
            data["issues"] = tuple(data.get("issues", ()))
            data["references"] = tuple(data.get("references", ()))
            artifacts[key] = TraceArtifact(**data)
        snapshot = cls(value["task_id"], tuple(events), value["complete"],
                       value.get("schema_version", 1), artifacts,
                       value.get("recording_scope", {"legacy": "event-only snapshot"}))
        for index, event in enumerate(snapshot.events, 1):
            if event.task_id != snapshot.task_id or event.sequence != index:
                raise ValueError("inconsistent task or event sequence")
            for artifact_id in event.artifact_ids:
                if artifact_id not in artifacts:
                    raise ValueError("event refers to missing artifact")
        for key, artifact in artifacts.items():
            if key != artifact.artifact_id or artifact.task_id != snapshot.task_id:
                raise ValueError("inconsistent artifact identity")
            if any(reference not in artifacts for reference in artifact.references):
                raise ValueError("artifact refers to missing artifact")
        return snapshot

    @classmethod
    def from_json(cls, text: str) -> TracerSnapshot:
        return cls.from_dict(json.loads(text))
