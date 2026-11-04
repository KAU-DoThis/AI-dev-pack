"""Validated, atomic plan revisions owned by one Task's Planning instance.

Item states are LLM assessments, never independent verification or TaskStatus.
Dependencies describe relationships only; they impose no execution policy.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable
from uuid import uuid4


class PlanError(ValueError):
    """A rejected plan request; the existing revision remains usable."""


class PlanItemStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"


def _text(value: Any, name: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise PlanError(f"{name} must be {'a string' if empty else 'a nonempty string'}")
    return value


def _refs(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise PlanError(f"{name} must be an array of nonempty strings")
    return tuple(_text(ref, name) for ref in value)


@dataclass(frozen=True)
class PlanItem:
    item_id: str
    description: str
    status: PlanItemStatus = PlanItemStatus.PENDING
    depends_on: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()
    status_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"item_id": self.item_id, "description": self.description,
                "status": self.status.value, "depends_on": list(self.depends_on),
                "source_refs": list(self.source_refs), "status_note": self.status_note}


@dataclass(frozen=True)
class PlanSnapshot:
    task_id: str
    plan_id: str
    revision: int
    items: tuple[PlanItem, ...]
    change_kind: str
    reason: str
    source_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"task_id": self.task_id, "plan_id": self.plan_id, "revision": self.revision,
                "items": [item.to_dict() for item in self.items], "change_kind": self.change_kind,
                "reason": self.reason, "source_refs": list(self.source_refs)}


class Planning:
    """Single plan owner, with immutable current and historical read snapshots.

    publish connects to TaskChannel's synchronous artifact/state/event boundary.
    Validation finishes before publication; no method yields or invokes an LLM.
    """

    def __init__(self, task_id: str, *,
                 publish: Callable[[PlanSnapshot, PlanSnapshot | None, dict[str, Any]], None] | None = None):
        self.task_id = _text(task_id, "task_id")
        self._history: list[PlanSnapshot] = []
        self._publish = publish

    def snapshot(self, revision: int | None = None) -> PlanSnapshot | None:
        if revision is None:
            return self._history[-1] if self._history else None
        if type(revision) is not int or not 1 <= revision <= len(self._history):
            raise PlanError("revision must identify an existing plan revision")
        return self._history[revision - 1]

    def create(self, items: list[dict[str, Any]], reason: str,
               source_refs: list[str] | None = None, *,
               tool_ref: dict[str, Any] | None = None) -> PlanSnapshot:
        if self._history:
            raise PlanError("plan already exists; use plan_update or plan_revise")
        return self._apply(self._items(items, {}), "created", reason, source_refs, tool_ref)

    def update(self, updates: list[dict[str, Any]], expected_revision: int, reason: str,
               source_refs: list[str] | None = None, *,
               tool_ref: dict[str, Any] | None = None) -> PlanSnapshot:
        current = self._expected(expected_revision)
        previous = {item.item_id: item for item in current.items}
        changed = self._items(updates, previous, update=True)
        by_id = {item.item_id: item for item in changed}
        items = tuple(by_id.get(item.item_id, item) for item in current.items)
        return self._apply(items, "updated", reason, source_refs, tool_ref)

    def revise(self, items: list[dict[str, Any]], expected_revision: int, reason: str,
               source_refs: list[str] | None = None, *,
               tool_ref: dict[str, Any] | None = None) -> PlanSnapshot:
        current = self._expected(expected_revision)
        previous = {item.item_id: item for item in current.items}
        revised = self._items(items, previous)
        retired = {item.item_id for plan in self._history for item in plan.items} - previous.keys()
        if any(item.item_id in retired for item in revised):
            raise PlanError("excluded item IDs cannot be reused; use a new ID for replacement work")
        return self._apply(revised, "revised", reason, source_refs, tool_ref)

    def _expected(self, revision: int) -> PlanSnapshot:
        current = self.snapshot()
        if current is None:
            raise PlanError("no plan exists; use plan_create first")
        if type(revision) is not int or revision != current.revision:
            raise PlanError(f"expected_revision must equal current revision {current.revision}")
        return current

    @staticmethod
    def _items(values: Any, previous: dict[str, PlanItem], *, update: bool = False) -> tuple[PlanItem, ...]:
        if not isinstance(values, list):
            raise PlanError("items/updates must be an array")
        result: list[PlanItem] = []
        seen: set[str] = set()
        allowed = {"item_id", "description", "status", "source_refs", "status_note"}
        if not update:
            allowed.add("depends_on")
        for value in values:
            if not isinstance(value, dict) or value.keys() - allowed:
                raise PlanError("invalid item fields; dependency changes require plan_revise")
            item_id = _text(value.get("item_id"), "item_id")
            if item_id in seen:
                raise PlanError(f"duplicate item_id: {item_id}")
            seen.add(item_id)
            old = previous.get(item_id)
            if update and old is None:
                raise PlanError(f"unknown item_id: {item_id}")
            data = {**(old.to_dict() if old else {}), **value}
            try:
                status = PlanItemStatus(data.get("status", "pending"))
            except (ValueError, TypeError) as exc:
                raise PlanError("invalid item status") from exc
            result.append(PlanItem(
                item_id, _text(data.get("description"), "description"), status,
                _refs(data.get("depends_on", []), "depends_on"),
                _refs(data.get("source_refs", []), "item source_refs"),
                _text(data.get("status_note", ""), "status_note", empty=True),
            ))
        return tuple(result)

    @staticmethod
    def _validate_graph(items: tuple[PlanItem, ...]) -> None:
        # Iterative topological validation also handles long valid dependency chains.
        dependents: dict[str, list[str]] = {item.item_id: [] for item in items}
        counts = {}
        for item in items:
            if len(set(item.depends_on)) != len(item.depends_on):
                raise PlanError(f"duplicate dependency in {item.item_id}")
            counts[item.item_id] = len(item.depends_on)
            for dependency in item.depends_on:
                if dependency == item.item_id:
                    raise PlanError(f"self dependency: {item.item_id}")
                if dependency not in dependents:
                    raise PlanError(f"unknown dependency: {dependency}")
                dependents[dependency].append(item.item_id)
        ready = [key for key, count in counts.items() if count == 0]
        visited = 0
        while ready:
            visited += 1
            for key in dependents[ready.pop()]:
                counts[key] -= 1
                if counts[key] == 0:
                    ready.append(key)
        if visited != len(items):
            raise PlanError("cyclic plan dependencies")

    def _apply(self, items: tuple[PlanItem, ...], kind: str, reason: str,
               source_refs: list[str] | None, tool_ref: dict[str, Any] | None) -> PlanSnapshot:
        reason = _text(reason, "reason")
        refs = _refs([] if source_refs is None else source_refs, "source_refs")
        self._validate_graph(items)
        previous = self.snapshot()
        if previous is not None and items == previous.items:
            # A repeat/empty patch is not a revision, even with different commentary.
            return previous
        snapshot = PlanSnapshot(self.task_id, previous.plan_id if previous else uuid4().hex,
                                len(self._history) + 1, items, kind, reason, refs)
        if self._publish is not None:
            self._publish(snapshot, previous, dict(tool_ref or {}))
        self._history.append(snapshot)
        return snapshot
