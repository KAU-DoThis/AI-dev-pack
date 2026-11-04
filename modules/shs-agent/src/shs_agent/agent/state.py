"""Small, immutable current-state values; TaskChannel is their sole owner."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any

from .types import TaskStatus


class AgentActivity(str, Enum):
    PREPARING = "preparing"
    LLM_CALL = "llm_call"
    TOOL_EXECUTION = "tool_execution"
    WAITING_FOR_HOST = "waiting_for_host"
    WAITING_FOR_SUBAGENTS = "waiting_for_subagents"
    WAITING_FOR_PARENT_REPLY = "waiting_for_parent_reply"
    WAITING_FOR_CHILD_REPLY = "waiting_for_child_reply"
    WAITING_FOR_TOOL_APPROVAL = "waiting_for_tool_approval"
    CONTEXT_COMPACTION = "context_compaction"
    FINISHED = "finished"


@dataclass(frozen=True)
class TaskState:
    task_id: str
    instruction: str
    status: TaskStatus
    created_at: str
    input_artifact_id: str
    started_at: str | None = None
    finished_at: str | None = None
    stop_reason: str | None = None
    cancel_requested: bool = False
    plan_id: str | None = None
    plan_revision: int | None = None
    plan_artifact_id: str | None = None
    parent_task_id: str | None = None
    root_task_id: str | None = None
    depth: int = 0


@dataclass(frozen=True)
class AgentState:
    activity: AgentActivity = AgentActivity.PREPARING
    operation_id: str | None = None
    operation_kind: str | None = None
    operation_artifact_id: str | None = None
    model: str | None = None
    provider: str | None = None
    steps: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    waiting_for: str | None = None
    question_id: str | None = None
    waiting_request_id: str | None = None
    waiting_task_ids: tuple[str, ...] = ()
    outgoing_request_ids: tuple[str, ...] = ()
    incoming_request_ids: tuple[str, ...] = ()
    queued_message_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class SessionState:
    task: TaskState
    agent: AgentState
    last_event_sequence: int

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for key, item in value["agent"].items():
            if isinstance(item, tuple):
                value["agent"][key] = list(item)
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> SessionState:
        task, agent = dict(value["task"]), dict(value["agent"])
        task["status"] = TaskStatus(task["status"])
        agent["activity"] = AgentActivity(agent["activity"])
        for key in ("waiting_task_ids", "outgoing_request_ids", "incoming_request_ids", "queued_message_ids"):
            if key in agent:
                agent[key] = tuple(agent[key])
        return cls(TaskState(**task), AgentState(**agent), value["last_event_sequence"])
