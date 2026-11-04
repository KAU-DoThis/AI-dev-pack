import asyncio
import copy
import inspect
import json
import unittest
from collections import deque

from shs_agent.llm import LLMResponse, ToolCall
from shs_agent.tool import Tool, ToolRegistry, ToolSpec


def calls(*items, content=None):
    return LLMResponse(content, [ToolCall(name, arguments) for name, arguments in items])


def complete(output="done"):
    return calls(("task_complete", {"output": output}))


class FakeProvider:
    def __init__(self, *responses):
        self.responses = deque(responses)
        self.requests = []

    async def chat(self, **request):
        self.requests.append(copy.deepcopy(request))
        if not self.responses:
            raise AssertionError("unexpected provider call")
        response = self.responses.popleft()
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            response = response(request)
        if inspect.isawaitable(response):
            response = await response
        return response


class Gate:
    """Deterministic in-flight operation, no timing-based sleeps."""
    def __init__(self, value):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.value = value

    async def __call__(self, *args, **kwargs):
        self.entered.set()
        await self.release.wait()
        return self.value


def registry_for(**handlers):
    registry = ToolRegistry()
    for name, handler in handlers.items():
        registry.register(Tool(ToolSpec(name, name, {"type": "object", "properties": {}}), handler))
    return registry


async def event_of(session, kind, predicate=lambda event: True):
    async with asyncio.timeout(3):
        async for event in session.events():
            if event.type == kind and predicate(event):
                return event
    raise AssertionError(f"no {kind} event before session closed")


async def events_of(session):
    return [event async for event in session.events()]


def tool_results(result):
    return [json.loads(message["content"]) for message in result.messages if message["role"] == "tool"]


class AsyncCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sessions = []
        self.gates = []

    async def start(self, provider, **kwargs):
        from shs_agent import ProblemSolver
        session = await ProblemSolver().start("test task", model="fake", provider=provider, **kwargs)
        self.sessions.append(session)
        return session

    def gate(self, value):
        gate = Gate(value)
        self.gates.append(gate)
        return gate

    async def result(self, session):
        return await asyncio.wait_for(session.result(), 3)

    async def asyncTearDown(self):
        for gate in self.gates:
            gate.release.set()
        for session in self.sessions:
            if not session.done:
                await session.cancel()
            await asyncio.wait_for(session.result(), 3)

    def assert_balanced(self, messages):
        pending = []
        for message in messages:
            if message["role"] == "tool":
                self.assertTrue(pending, "orphan tool result")
                self.assertEqual(pending.pop(0), message["tool_call_id"])
            else:
                self.assertFalse(pending, "message inserted inside a tool batch")
                pending = [call["id"] for call in message.get("tool_calls", [])]
        self.assertFalse(pending, "unanswered tool calls")
