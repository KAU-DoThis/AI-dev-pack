import asyncio
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from shs_agent import AgentCore, AgentLimits, ProblemSolver, TaskRequest, TaskStatus
from shs_agent.llm import LLMResponse, ToolCall
from shs_agent.tool import ToolExecutor, ToolRegistry

from tests.fakes import AsyncCase, FakeProvider, complete, event_of, registry_for


class CoreTests(AsyncCase):
    async def test_provider_input_and_response_are_isolated_snapshots(self):
        response = LLMResponse("answer", [ToolCall("test", {"nested": [1]})], thinking="reason", usage={"tokens": 2})
        class Provider:
            def chat(self, **kwargs):
                kwargs["messages"][0]["content"] = "changed"
                kwargs["tools"].clear()
                kwargs["options"]["x"].append(2)
                return response
        messages, tools, options = [{"role": "user", "content": "original"}], [{"name": "test"}], {"x": [1]}
        result = await AgentCore(Provider()).step(model="fake", messages=messages, tools=tools, options=options)
        self.assertEqual(messages[0]["content"], "original")
        self.assertEqual(tools, [{"name": "test"}])
        self.assertEqual(options, {"x": [1]})
        response.tool_calls[0].arguments["nested"].append(2)
        self.assertEqual(result.tool_calls[0].arguments, {"nested": [1]})
        self.assertEqual(result.thinking, "reason")
        self.assertEqual(result.usage, {"tokens": 2})

    async def test_malformed_responses_fail_before_executing_any_tool(self):
        cases = [object(), SimpleNamespace(content=2, tool_calls=[]),
                 SimpleNamespace(content="x", tool_calls=None),
                 LLMResponse(None, [ToolCall("x", {}, "same"), ToolCall("x", {}, "same")]),
                 LLMResponse(None, [ToolCall("", {})]),
                 LLMResponse(None, [ToolCall("x", {}, "")]),
                 LLMResponse(None, [ToolCall("x", {1: "bad"})]),
                 LLMResponse(None, [ToolCall("x", {"x": float("nan")})]),
                 LLMResponse(None, [ToolCall("x", {"x": object()})]),
                 LLMResponse("x", thinking=42)]
        for response in cases:
            with self.subTest(response=response):
                session = await self.start(FakeProvider(response))
                result = await self.result(session)
                self.assertEqual((result.status, result.tool_calls), (TaskStatus.FAILED, 0))
                self.assertTrue(result.warnings)

    async def test_sync_provider_runs_off_loop_and_receives_control_while_blocked(self):
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        threads = []
        class Provider:
            def chat(self, **kwargs):
                threads.append(threading.get_ident())
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(3):
                    raise RuntimeError("test gate timeout")
                return complete()
        session = await self.start(Provider())
        try:
            await asyncio.wait_for(entered.wait(), 3)
            await session.cancel()
            await event_of(session, "status_changed", lambda e: e.payload["status"] == "cancelling")
        finally:
            release.set()
        self.assertEqual((await self.result(session)).status, TaskStatus.CANCELLED)
        self.assertNotEqual(threads[0], threading.get_ident())

    async def test_sync_provider_returning_awaitable_is_supported(self):
        class Provider:
            def chat(self, **kwargs):
                async def response():
                    return complete()
                return response()
        result = await self.result(await self.start(Provider()))
        self.assertEqual(result.status, TaskStatus.COMPLETED)

    async def test_thinking_is_preserved_using_common_llm_response_message(self):
        response = complete()
        response.thinking = "provider metadata"
        result = await self.result(await self.start(FakeProvider(response)))
        self.assertEqual(result.messages[-2]["thinking"], "provider metadata")


class ContractTests(unittest.TestCase):
    def test_invalid_task_limits_and_solver_wiring_fail_early(self):
        for value in ("", "   ", None, 42):
            with self.subTest(value=value), self.assertRaises(ValueError):
                TaskRequest(value)
        for context in ("single string", (3,)):
            with self.assertRaises(TypeError):
                TaskRequest("goal", context=context)
        for kwargs in ({"max_steps": 0}, {"max_steps": True}, {"max_tool_calls": -1},
                       {"host_response_timeout": float("nan")}, {"host_response_timeout": True},
                       {"context_compact_threshold_chars": 0}, {"max_tool_output_chars": 10},
                       {"max_memo_item_chars": 30000}, {"conversation_read_max_chars": 6001}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                AgentLimits(**kwargs)
        solver = ProblemSolver()
        with self.assertRaises(ValueError):
            solver.create_task(request=TaskRequest("goal"), model="fake")
        registry = ToolRegistry()
        with self.assertRaises(ValueError):
            solver.create_task(request=TaskRequest("goal"), provider=FakeProvider(), model="fake",
                               registry=registry, executor=ToolExecutor(ToolRegistry()))
        for name in ("task_complete", "host_ask", "memo_write", "conversation_search", "conversation_read",
                     "plan_create", "plan_read", "plan_update", "plan_revise"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                solver.create_task(request=TaskRequest("goal"), provider=FakeProvider(), model="fake",
                                   registry=registry_for(**{name: lambda: None}))

    def test_import_works_with_no_site_packages_or_optional_sdks(self):
        src = Path(__file__).resolve().parents[1] / "src"
        script = (
            f"import sys; sys.path.insert(0, {str(src)!r}); "
            "import shs_agent; from shs_agent.llm.provider.ollama import OllamaLLM; "
            "from shs_agent.llm.provider.openai import OpenAILLM; shs_agent.ProblemSolver(); "
            "assert not any(x in sys.modules for x in ['torch','ollama','openai','sqlalchemy']); "
            "assert not any('.old' in x for x in sys.modules); print('stdlib-only import OK')"
        )
        result = subprocess.run([sys.executable, "-I", "-S", "-c", script],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("stdlib-only import OK", result.stdout)

    def test_provider_construction_does_not_probe_server_or_load_model(self):
        from shs_agent.llm.provider.ollama import OllamaLLM
        from shs_agent.llm.provider.openai import OpenAILLM
        ollama_client, openai_client = Mock(), Mock()
        with patch.dict(sys.modules, {
            "ollama": SimpleNamespace(Client=ollama_client),
            "openai": SimpleNamespace(OpenAI=openai_client),
        }):
            OllamaLLM(host="http://remote.invalid:11434")
            OpenAILLM(base_url="http://remote.invalid:8000/v1", api_key="test")
        ollama_client.assert_called_once_with(host="http://remote.invalid:11434")
        openai_client.assert_called_once_with(base_url="http://remote.invalid:8000/v1", api_key="test")
        self.assertEqual(ollama_client.return_value.mock_calls, [])
        self.assertEqual(openai_client.return_value.mock_calls, [])
