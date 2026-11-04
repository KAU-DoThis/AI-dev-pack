"""Whole-tree observation index. Payloads remain in each task's original trace."""
import asyncio

from ..types import integer


class TreeJournal:
    def __init__(self):
        self.channels = {}
        self.order = []
        self.ready = asyncio.Event()

    def attach(self, channel):
        if channel.task_id in self.channels:
            return
        self.channels[channel.task_id] = channel
        channel.journal = self
        for sequence in range(1, channel.state().last_event_sequence + 1):
            self.record(channel.task_id, sequence)

    def record(self, task_id, sequence):
        self.order.append((task_id, sequence))
        self.ready.set()

    async def events(self, after_sequence):
        integer(after_sequence, "after_sequence")
        if after_sequence > len(self.order):
            raise ValueError("cursor is beyond the tree journal")
        index = after_sequence
        while True:
            while index < len(self.order):
                task_id, sequence = self.order[index]
                index += 1
                yield {"tree_sequence": index,
                       "event": self.channels[task_id].event_at(sequence)}
            if all(channel.done for channel in self.channels.values()):
                return
            self.ready.clear()
            await self.ready.wait()
