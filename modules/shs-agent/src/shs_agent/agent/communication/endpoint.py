"""Narrow caller-bound capabilities, without child access to root management."""
from typing import Callable


class ParentConnection:
    """Same caller-bound operations at every depth; transport checks relationships."""
    def __init__(self, send: Callable, reply: Callable, wait: Callable):
        self._send, self._reply, self._wait = send, reply, wait

    def reply(self, request_id, message, *, tool_ref=None):
        return self._reply(request_id=request_id, message=message, tool_ref=tool_ref)

    async def wait_reply(self, request_id, *, tool_ref=None):
        return await self._wait(request_id, tool_ref=tool_ref)


    def ask_parent(self, message, *, related_request_id=None, tool_ref=None):
        return self._send(kind="question", message=message,
                          related_request_id=related_request_id, tool_ref=tool_ref)

    def notify_parent(self, message, *, tool_ref=None):
        return self._send(kind="notify", message=message, tool_ref=tool_ref)


    def ask_child(self, task_id, message, *, related_request_id=None, tool_ref=None):
        return self._send(task_id=task_id, kind="question", message=message,
                          related_request_id=related_request_id, tool_ref=tool_ref)

    def send_child(self, task_id, message, *, kind="notify", tool_ref=None):
        if kind not in {"notify", "instruction"}:
            from .models import CommunicationError
            raise CommunicationError("kind must be notify or instruction")
        return self._send(task_id=task_id, kind=kind, message=message, tool_ref=tool_ref)


# Compatibility names refer to the same implementation, not different agent roles.
ChildCommunication = ParentConnection
RootCommunication = ParentConnection
ParentChildCommunication = ParentConnection


def host_connection(channel, timeout):
    """Host is an external endpoint, not a synthetic Agent Task."""
    def send(**kwargs):
        if "task_id" in kwargs:
            from .models import CommunicationError
            raise CommunicationError("this task has no child transport")
        return channel.send_parent_request(timeout=timeout, **kwargs)
    return ParentConnection(
        send,
        channel.reply_to_host,
        channel.wait_reply,
    )
