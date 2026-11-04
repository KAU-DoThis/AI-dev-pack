"""Immutable messages and request lifecycle values owned by TaskChannel."""
from dataclasses import asdict, dataclass


class CommunicationError(ValueError):
    """Invalid relationship, request access or communication arguments."""


@dataclass(frozen=True)
class AgentMessage:
    message_id: str
    request_id: str | None
    from_task_id: str | None
    to_task_id: str | None
    kind: str
    content: str
    related_request_id: str | None = None

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class AgentRequest:
    message: AgentMessage
    artifact_id: str
    status: str = "pending"
    reply: AgentMessage | None = None
    reply_artifact_id: str | None = None

    @property
    def reference(self):
        return {"task_id": self.message.from_task_id or self.message.to_task_id, "artifact_id": self.artifact_id}
