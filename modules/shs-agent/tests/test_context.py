import json
import unittest

from shs_agent import AgentLimits, TaskRequest, TaskStatus
from shs_agent.agent.context import ContextBuilder, TaskContext
from shs_agent.agent.context.compaction import choose_compaction_upto, estimate_context_chars
from shs_agent.llm import LLMResponse, ToolCall

from tests.fakes import (AsyncCase, FakeProvider, calls, complete,
                         events_of, registry_for, tool_results)


class ContextTests(unittest.TestCase):
    def context(self, **limits):
        limits = AgentLimits(**limits)
        self.builder = ContextBuilder(limits)
        return TaskContext("task-id", TaskRequest("initial", context=("host context",)), limits)

    def plan(self, context):
        snapshot = context.snapshot()
        return context.compaction.plan(snapshot.conversation, context_chars=estimate_context_chars(
            self.builder.build(snapshot), [],
        ))

    def test_complete_tool_groups_define_compaction_boundary(self):
        messages = [
            {"role": "assistant", "tool_calls": [{"id": "a"}, {"id": "b"}]},
            {"role": "tool", "tool_call_id": "a"},
            {"role": "tool", "tool_call_id": "b"},
            {"role": "assistant", "content": "recent"},
        ]
        self.assertEqual(choose_compaction_upto(messages, start=0, keep_recent_messages=2), 0)
        self.assertEqual(choose_compaction_upto(messages, start=0, keep_recent_messages=1), 3)
        self.assertEqual(choose_compaction_upto(messages[:2], start=0, keep_recent_messages=1), 0)

    def test_compaction_preserves_conversation_host_instructions_and_memos(self):
        context = self.context(context_compact_threshold_chars=256, context_keep_recent_messages=1)
        context.conversation.add_instruction("exact new constraint")
        context.memo.write("durable note", "fact", ["source"], step=1)
        context.conversation.record_response(LLMResponse("old detail " * 100))
        context.conversation.record_response(LLMResponse("recent"))
        before = context.result_fields()["messages"]
        plan = self.plan(context)
        self.assertIsNotNone(plan)
        context.compaction.apply(plan, LLMResponse("short summary"))
        view = self.builder.build(context.snapshot())
        self.assertEqual(context.result_fields()["messages"], before)
        self.assertIn({"role": "user", "content": "exact new constraint"}, view)
        self.assertIn("durable note", str(view))
        self.assertNotIn("old detail", str(view))
        self.assertNotIn("short summary", view[0]["content"])
        self.assertEqual(view[0], before[0])
        self.assertEqual(view[-1]["content"], "recent")
        self.assertEqual(context.conversation.search("old detail")["matches"][0]["message_index"], 4)
        self.assertIn("old detail", context.conversation.read(4)["serialized_message_chunk"])

    def test_full_tool_record_can_be_retrieved_after_prompt_truncation(self):
        context = self.context(max_tool_output_chars=256)
        call = ToolCall("large", {})
        context.conversation.record_response(LLMResponse(None, [call]))
        value = '"quoted\\text\n' * 1000 + "unique tail"
        context.conversation.record_tool(call, {"ok": True, "output": value})
        view = self.builder.build(context.snapshot())[-1]["content"]
        self.assertLessEqual(len(view), 256)
        self.assertTrue(json.loads(view)["truncated"])
        self.assertIn("unique tail", context.result_fields()["messages"][-1]["content"])
        index = context.conversation.search("unique tail")["matches"][0]["message_index"]
        chunks, offset = [], 0
        while True:
            page = context.conversation.read(index, offset, 256)
            chunks.append(page["serialized_message_chunk"])
            if page["eof"]:
                break
            offset = page["next_char_offset"]
        original = json.loads("".join(chunks))
        self.assertEqual(json.loads(original["content"])["output"], value)

    def test_memo_limits_and_retrieval_input_validation(self):
        context = self.context(max_memo_item_chars=64, max_task_memo_chars=128)
        for _ in range(2):
            context.memo.write("a" * 64, "fact", [], step=1)
        for content in ("b", "x" * 65):
            with self.assertRaises(ValueError):
                context.memo.write(content, "fact", [], step=2)
        with self.assertRaises(TypeError):
            context.memo.write("fact", "kind", "not a list", step=1)
        for kwargs in ({"message_index": True}, {"message_index": -1},
                       {"message_index": 0, "char_offset": -1},
                       {"message_index": 0, "max_chars": 99},
                       {"message_index": 0, "max_chars": 6001},
                       {"message_index": 0, "char_offset": 99999}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                context.conversation.read(**kwargs)
        with self.assertRaises(IndexError):
            context.conversation.read(999)
        with self.assertRaises(ValueError):
            context.conversation.read(1, exclude=1)
        self.assertFalse(context.conversation.search("initial", exclude=1)["matches"])

    def test_summary_validation_does_not_mutate_history(self):
        context = self.context(context_compact_threshold_chars=256, context_keep_recent_messages=1)
        context.conversation.record_response(LLMResponse("older"))
        context.conversation.record_response(LLMResponse("recent"))
        plan = self.plan(context)
        for response in (LLMResponse(""), complete(), LLMResponse("partial", finish_reason="length")):
            with self.assertRaises(ValueError):
                context.compaction.apply(plan, response)
        self.assertEqual(context.compaction.count, 0)
        context.compaction.apply(plan, LLMResponse("x" * 20000))
        self.assertLessEqual(len(context.result_fields()["context_summary"]), 12000)
        with self.assertRaises(ValueError):
            context.compaction.apply(plan, LLMResponse("again"))


class ContextIntegrationTests(AsyncCase):
    async def test_memo_write_search_and_read_use_task_context(self):
        provider = FakeProvider(
            calls(("memo_write", {"content": "retain fact", "kind": "discovery", "source_refs": ["file"]})),
            calls(("conversation_search", {"query": "retain fact"})),
            calls(("conversation_read", {"message_index": 1})), complete(),
        )
        session = await self.start(provider)
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual(result.memos[0].created_step, 1)
        self.assertIn("retain fact", str(provider.requests[1]["messages"]))
        search = tool_results(result)[1]["output"]
        self.assertTrue(search["matches"])
        self.assertTrue(all(item["message_index"] != 4 for item in search["matches"]))
        self.assertIn("test task", tool_results(result)[2]["output"]["serialized_message_chunk"])
        self.assertEqual(sum(e.type == "task_memo_written" for e in await events_of(session)), 1)

    async def test_runtime_compaction_is_separate_from_steps_and_keeps_atomic_groups(self):
        provider = FakeProvider(
            calls(("echo", {}), ("echo", {})),
            LLMResponse("recent progress"), LLMResponse("summary of tool observations"), complete(),
        )
        session = await self.start(provider, registry=registry_for(echo=lambda: "observation " * 100),
                                  limits=AgentLimits(context_compact_threshold_chars=256,
                                                     context_keep_recent_messages=1))
        result = await self.result(session)
        self.assertEqual((result.status, result.steps, result.context_compactions), (TaskStatus.COMPLETED, 3, 1))
        self.assertEqual(provider.requests[2]["tools"], [])
        self.assertIn("summary of tool observations", str(provider.requests[3]["messages"]))
        self.assertIn("observation " * 100, str(result.messages))
        self.assert_balanced(result.messages)
        self.assert_balanced(provider.requests[3]["messages"])

    async def test_compaction_failure_is_reported_with_history_intact(self):
        provider = FakeProvider(LLMResponse("old"), LLMResponse("recent"), LLMResponse(""))
        session = await self.start(provider, limits=AgentLimits(context_compact_threshold_chars=256,
                                                               context_keep_recent_messages=1))
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertEqual((result.steps, result.context_compactions), (2, 0))
        self.assertIn("old", str(result.messages))
        self.assertIn("summarizer", result.warnings[0])

    async def test_instruction_received_during_summary_is_applied_before_next_decision(self):
        gate = self.gate(LLMResponse("summary"))
        provider = FakeProvider(LLMResponse("old"), LLMResponse("recent"), gate, complete())
        session = await self.start(provider, limits=AgentLimits(context_compact_threshold_chars=256,
                                                               context_keep_recent_messages=1))
        await gate.entered.wait()
        await session.send_instruction("constraint during compression")
        gate.release.set()
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertIn("constraint during compression", str(provider.requests[-1]["messages"]))
