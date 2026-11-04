"""Host-owned execution profiles and detached recursive task trace archives."""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..tracer import TraceArtifact, TracerSnapshot
from ..types import AgentLimits, LLMProvider, TaskRequest, ToolExecutorLike, ToolRegistryLike, nonempty
from shs_agent.workspace import Workspace


class SubagentError(ValueError):
    """A rejected child operation; the parent may recover on its next decision."""


class _Inherit(Enum):
    OPTIONS = "parent_options"


INHERIT_OPTIONS = _Inherit.OPTIONS


@dataclass(frozen=True, kw_only=True)
class SubagentConfig:
    """Explicit host capability profile. Provider and tools are shared objects.

    Missing provider/model/options/per-task limits use the parent's settings.
    The LLM selects a registered profile ID, never arbitrary objects or overrides.
    Tool sets are always explicit. Options are copied per task; state is not shared.
    allowed_subagent_config_ids grants this profile permission to spawn those
    profiles from the root's Host catalog. Empty (the default) grants no spawn.
    allowed_tool_ids bounds dynamic Host decisions for this task; it does not
    apply tools or give this Agent permission to approve its own children.
    """

    registry: ToolRegistryLike
    executor: ToolExecutorLike
    provider: LLMProvider | None = None
    model: str | None = None
    limits: AgentLimits | None = None
    options: Any = INHERIT_OPTIONS
    system_prompt: str = TaskRequest.__dataclass_fields__["system_prompt"].default
    allowed_subagent_config_ids: tuple[str, ...] = ()
    allowed_tool_ids: tuple[str, ...] = ()
    delegable_tool_ids: tuple[str, ...] = ()
    workspace: Workspace | None = None

    def __post_init__(self) -> None:
        # Local import avoids a composition-time tools/subagent import cycle.
        from ..tools.integration import tool_ids
        object.__setattr__(self, "allowed_tool_ids", tool_ids(self.allowed_tool_ids))
        object.__setattr__(self, "delegable_tool_ids", tool_ids(self.delegable_tool_ids))
        if self.workspace is not None and not isinstance(self.workspace, Workspace):
            raise TypeError("workspace must be Workspace or None")
        if self.model is not None:
            nonempty(self.model, "model")
        if self.provider is not None and not callable(getattr(self.provider, "chat", None)):
            raise TypeError("provider must provide chat()")
        if self.limits is not None and not isinstance(self.limits, AgentLimits):
            raise TypeError("limits must be AgentLimits")
        if not isinstance(self.system_prompt, str):
            raise TypeError("system_prompt must be a string")
        if not isinstance(self.allowed_subagent_config_ids, (tuple, list)):
            raise TypeError("allowed_subagent_config_ids must be a sequence of config IDs")
        for key in self.allowed_subagent_config_ids:
            nonempty(key, "allowed_subagent_config_id")
        if len(set(self.allowed_subagent_config_ids)) != len(self.allowed_subagent_config_ids):
            raise ValueError("allowed_subagent_config_ids must be unique")
        object.__setattr__(self, "allowed_subagent_config_ids", tuple(self.allowed_subagent_config_ids))


@dataclass(frozen=True)
class TaskTraceRef:
    """Cross-task reference, separate from same-task Artifact.references."""

    task_id: str
    artifact_id: str

    def to_dict(self) -> dict[str, str]:
        return {"task_id": self.task_id, "artifact_id": self.artifact_id}


@dataclass(frozen=True)
class TaskTreeSnapshot:
    """Single root and all descendants, with each task's original trace exactly once.

    This is a detached export, not a scheduler or resume file.
    Subagent link artifacts use data.task_refs for explicit cross-task edges.
    """

    root_task_id: str
    traces: dict[str, TracerSnapshot]
    event_order: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "traces", copy.deepcopy(self.traces))
        object.__setattr__(self, "event_order", tuple(tuple(ref) for ref in self.event_order))
        self.validate()

    @property
    def complete(self) -> bool:
        return all(trace.complete for trace in self.traces.values())

    @property
    def details_complete(self) -> bool:
        return all(trace.details_complete for trace in self.traces.values())

    def artifact(self, reference: TaskTraceRef | dict[str, str]) -> TraceArtifact:
        if isinstance(reference, dict):
            reference = TaskTraceRef(**reference)
        return self.traces[reference.task_id].artifact(reference.artifact_id)

    def result(self, task_id: str) -> dict[str, Any]:
        """Read preserved terminal output/Memo/Plan without a Session or Provider."""
        trace = self.traces[task_id]
        if not trace.complete:
            return {"task_id": task_id, "available": False, "state": trace.state().to_dict()}
        finished = next(event for event in reversed(trace.events) if event.type == "finished")
        context_id = finished.payload.get("context_artifact_id")
        context = trace.artifact(context_id).data if context_id else {}
        plan_id = trace.state().task.plan_artifact_id
        return {"task_id": task_id, "available": True, **copy.deepcopy(finished.payload),
                "memos": context.get("memos", []),
                "workspace": context.get("workspace"),
                "plan": trace.artifact(plan_id).data if plan_id else None}

    def validate(self) -> None:
        if self.root_task_id not in self.traces:
            raise ValueError("tree archive is missing its root")
        registered = set()
        for task_id, trace in self.traces.items():
            # Preserve all existing same-task identity/reference validation.
            TracerSnapshot.from_dict(trace.to_dict())
            if task_id != trace.task_id:
                raise ValueError("tree key does not match trace task_id")
            state = trace.state().task
            if state.task_id != task_id or type(state.depth) is not int or state.depth < 0:
                raise ValueError("invalid task state identity or depth")
            if trace.complete and (not state.status.terminal or trace.events[-1].type != "finished"):
                raise ValueError("complete trace must contain its actual terminal state and finished event")
            if task_id == self.root_task_id:
                if state.parent_task_id is not None or state.depth != 0:
                    raise ValueError("invalid root relationship")
            else:
                parent = self.traces.get(state.parent_task_id)
                if parent is None or state.depth < 1 or state.depth != parent.state().task.depth + 1:
                    raise ValueError("descendant must reference its parent at the preceding depth")
            if state.root_task_id != self.root_task_id:
                raise ValueError("trace belongs to another root")
            for event in trace.events:
                if event.type == "subagent_created":
                    child_id = event.payload["task_id"]
                    child = self.traces.get(child_id)
                    if child is None or child.state().task.parent_task_id != task_id:
                        raise ValueError("registration must belong to the child's actual parent")
                    if child_id in registered:
                        raise ValueError("duplicate child registration")
                    registered.add(child_id)
            for artifact in trace.artifacts.values():
                if artifact.kind == "subagent_created" and "task_id" in artifact.data:
                    child = self.traces.get(artifact.data["task_id"])
                    if child is None:
                        raise ValueError("creation refers to missing child")
                    child_state = child.state().task
                    if any(artifact.data.get(key) != getattr(child_state, key)
                           for key in ("parent_task_id", "root_task_id", "depth")):
                        raise ValueError("creation relationship disagrees with child state")
                if artifact.kind.startswith(("subagent_", "agent_", "tool_access_", "tool_host_", "tool_approval_", "tool_grant_", "tool_resource_")):
                    for reference in artifact.data.get("task_refs", []):
                        try:
                            self.artifact(reference)
                        except (KeyError, TypeError) as exc:
                            raise ValueError("unresolved cross-task artifact reference") from exc
        if registered != set(self.traces) - {self.root_task_id}:
            raise ValueError("registered child traces must all be included exactly once")
        if self.event_order:
            cursors = dict.fromkeys(self.traces, 0)
            for task_id, sequence in self.event_order:
                if task_id not in cursors or type(sequence) is not int or sequence != cursors[task_id] + 1:
                    raise ValueError("invalid tree event ordering")
                cursors[task_id] = sequence
            if any(cursors[key] != len(trace.events) for key, trace in self.traces.items()):
                raise ValueError("tree event index must cover every task event exactly once")

    def events(self, *, after_sequence=0):
        """Replay the exact live tree cursor; old archives without an index reject."""
        if not self.event_order:
            raise ValueError("this legacy archive has no tree event order")
        if type(after_sequence) is not int or not 0 <= after_sequence <= len(self.event_order):
            raise ValueError("invalid tree cursor")
        for index in range(after_sequence, len(self.event_order)):
            task_id, sequence = self.event_order[index]
            yield {"tree_sequence": index + 1,
                   "event": copy.deepcopy(self.traces[task_id].events[sequence - 1])}

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": 2, "scope": "root_and_descendants",
                "root_task_id": self.root_task_id,
                "complete": self.complete, "details_complete": self.details_complete,
                "traces": {key: trace.to_dict() for key, trace in self.traces.items()},
                "event_order": [list(ref) for ref in self.event_order]}

    def to_json(self, *, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, allow_nan=False, indent=indent)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> TaskTreeSnapshot:
        version = (value.get("schema_version"), value.get("scope"))
        if version not in {(1, "root_and_direct_children"), (2, "root_and_descendants")}:
            raise ValueError("unsupported task tree archive")
        tree = cls(value["root_task_id"], {
            key: TracerSnapshot.from_dict(trace) for key, trace in value["traces"].items()
        }, tuple(value.get("event_order", ())))
        if version[0] == 1 and any(trace.state().task.depth > 1 for trace in tree.traces.values()):
            raise ValueError("version 1 archives support only direct children")
        return tree

    @classmethod
    def from_json(cls, text: str) -> TaskTreeSnapshot:
        return cls.from_dict(json.loads(text))
