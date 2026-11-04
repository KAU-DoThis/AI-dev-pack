"""Task-bound management operations; whole-tree observation belongs to the Host."""
from __future__ import annotations

from .models import SubagentError


class SubagentScope:
    def __init__(self, manager, task_id: str, config_ids: tuple[str, ...]):
        self._manager = manager
        self._task_id = task_id
        self.config_ids = config_ids

    @property
    def can_spawn(self) -> bool:
        return bool(self.config_ids)

    def bind_environment(self, tools, planning):
        self._manager.bind_environment(self._task_id, tools, planning)

    async def spawn(self, **kwargs):
        return await self._manager.spawn(self._task_id, **kwargs)

    async def status(self, task_id=None):
        return await self._manager.status(self._task_id, task_id)

    async def result(self, task_id, **kwargs):
        return await self._manager.result(self._task_id, task_id, **kwargs)

    async def wait(self, task_ids, **kwargs):
        return await self._manager.wait(self._task_id, task_ids, **kwargs)

    async def cancel(self, task_id, **kwargs):
        return await self._manager.cancel(self._task_id, task_id, **kwargs)

    def ensure_can_complete(self):
        self._manager.ensure_can_complete(self._task_id)

    def interrupt_wait(self, reason):
        self._manager.interrupt_wait(reason, caller=self._task_id)

    async def finish_parent(self, status):
        await self._manager.finish_parent(status, caller=self._task_id)

    async def snapshot(self):
        if self._task_id != self._manager.root_task_id:
            raise SubagentError("whole-tree snapshots are available through the root")
        return await self._manager.snapshot()
