import copy
import json
import unittest
from dataclasses import FrozenInstanceError, asdict
from unittest.mock import patch

from shs_agent import AgentLimits, ProblemSolver, TaskMemo, TaskRequest, TaskStatus
from shs_agent.agent.context import ContextBuilder, TaskContext
from shs_agent.agent.context.compaction import estimate_context_chars
from shs_agent.llm import LLMResponse, ToolCall

from tests.fakes import AsyncCase, FakeProvider, calls, complete, events_of, registry_for, tool_results


class ContextBuilderTests(unittest.TestCase):
    def setUp(self):
        self.limits = AgentLimits(max_tool_output_chars=256, context_compact_threshold_chars=256,
                                  context_keep_recent_messages=3)
        self.request = TaskRequest("  original task\nkeep this exactly  ",
                                   context=("host evidence\nline two", "second context"),
                                   system_prompt="custom system prompt")
        self.context = TaskContext("origin-task", self.request, self.limits)
        self.builder = ContextBuilder(self.limits)

    def record_batch(self, label):
        response = LLMResponse("batch " + label, [
            ToolCall("echo", {"nested": [label]}, id=label + "-a"),
            ToolCall("echo", {"nested": [label]}, id=label + "-b"),
        ], thinking="reasoning " + label)
        self.context.conversation.record_response(response)
        for call in response.tool_calls:
            self.context.conversation.record_tool(call, {"ok": True, "output": call.id})
        return response

    def plan(self):
        snapshot = self.context.snapshot()
        return self.context.compaction.plan(snapshot.conversation, context_chars=estimate_context_chars(
            self.builder.build(snapshot), [],
        ))

    def test_input_roles_order_and_memo_provenance(self):
        memo = self.context.memo.write("observation", "fact", ["message:4"], step=2)
        self.context.conversation.add_instruction("  extra instruction\n ")
        response = self.record_batch("recent")
        messages = self.builder.build(self.context.snapshot())
        self.assertEqual(messages[:4], [
            {"role": "system", "content": self.request.system_prompt},
            {"role": "user", "content": self.request.instruction},
            *({"role": "user", "content": "Host context:\n" + value} for value in self.request.context),
        ])
        self.assertEqual([m["role"] for m in messages],
                         ["system", "user", "user", "user", "assistant", "user", "assistant", "tool", "tool"])
        memos = json.loads(messages[4]["content"].split("\n", 1)[1])
        self.assertEqual(memos, [{**asdict(memo), "source_refs": ["message:4"]}])
        self.assertEqual(memos[0]["task_id"], "origin-task")
        self.assertEqual(messages[5], {"role": "user", "content": "  extra instruction\n "})
        self.assertEqual(messages[6], response.to_message())
        self.assertEqual([m["tool_call_id"] for m in messages[7:]], ["recent-a", "recent-b"])

    def test_build_is_repeatable_and_input_and_returned_copies_are_isolated(self):
        refs = ["source"]
        memo = self.context.memo.write("kept memo", "fact", refs, step=1)
        refs.append("mutated outside")
        response = self.record_batch("recent")
        response.tool_calls[0].arguments["nested"].append("mutated outside")
        snapshot = self.context.snapshot()
        before = copy.deepcopy(snapshot)
        original = self.context.result_fields()
        first = self.builder.build(snapshot)
        expected = copy.deepcopy(first)
        first[0]["content"] = "changed system"
        first[4]["content"] = "changed memo"
        first[5]["tool_calls"][0]["arguments"]["nested"].append("changed call")
        first[6]["content"] = "changed tool result"
        first.append({"role": "user", "content": "extra"})
        for _ in range(3):
            self.assertEqual(self.builder.build(snapshot), expected)
            self.assertEqual(self.builder.build(self.context.snapshot()), expected)
        self.assertEqual(snapshot, before)
        self.assertEqual(self.context.result_fields(), original)
        self.assertEqual(memo.source_refs, ("source",))
        with self.assertRaises(FrozenInstanceError):
            memo.content = "changed"
        # A snapshot is detached from both later appends and caller dictionary edits.
        snapshot.conversation.messages[0]["content"] = "changed snapshot"
        self.assertEqual(self.context.result_fields(), original)
        self.context.conversation.add_instruction("later")
        self.context.memo.write("later memo", "fact", [], step=2)
        self.assertNotIn("later", str(self.builder.build(before)))

    def test_compaction_preserves_instructions_and_whole_tool_batches_across_builds(self):
        self.context.memo.write("retained discovery", "fact", ["tool:older-a"], step=1)
        self.context.conversation.add_instruction("  older constraint\n ")
        self.record_batch("older")
        self.context.conversation.add_instruction("newer constraint")
        self.record_batch("recent")
        original = self.context.result_fields()["messages"]
        before = self.builder.build(self.context.snapshot())
        plan = self.plan()
        self.assertEqual((plan.start, plan.upto), (4, 9))
        folded = json.loads(plan.messages[1]["content"])["messages_to_fold_in"]
        self.assertEqual(folded, list(original[4:9]))
        self.context.compaction.apply(plan, LLMResponse("older observations summarized"))
        after = self.builder.build(self.context.snapshot())
        self.assertEqual(after[:4], before[:4])
        self.assertEqual(after[4]["role"], "assistant")
        self.assertIn("indices before 9", after[4]["content"])
        self.assertIn("older observations summarized", after[4]["content"])
        self.assertEqual(after[5], before[4])  # Same assistant Memo and provenance.
        self.assertEqual(after[6:8], [original[4], original[8]])
        self.assertEqual(after[8:], list(original[9:]))
        self.assertEqual([m for m in before if m["role"] == "user"],
                         [m for m in after if m["role"] == "user"])
        self.assertEqual(self.context.result_fields()["messages"], original)
        # A second compaction incorporates the previous summary and the next full batch.
        self.context.conversation.add_instruction("latest constraint")
        self.record_batch("latest")
        next_plan = self.plan()
        summary_input = json.loads(next_plan.messages[1]["content"])
        self.assertEqual(summary_input["previous_summary"], "older observations summarized")
        self.assertEqual((next_plan.start, next_plan.upto), (9, 13))
        self.context.compaction.apply(next_plan, LLMResponse("rolling summary"))
        final = self.builder.build(self.context.snapshot())
        self.assertEqual([m["content"] for m in final if m["role"] == "user"],
                         [self.request.instruction, *["Host context:\n" + x for x in self.request.context],
                          "  older constraint\n ", "newer constraint", "latest constraint"])
        self.assertEqual([m["tool_call_id"] for m in final if m["role"] == "tool"],
                         ["latest-a", "latest-b"])

    def test_conversation_search_keeps_original_scope_and_indices(self):
        conversation = self.context.conversation
        other = TaskContext("other-task", TaskRequest("other private data"), self.limits)
        other.memo.write("other memo", "fact", [], step=1)
        self.context.memo.write("memo-only secret", "fact", [], step=1)
        conversation.record_response(LLMResponse("needle exact match"))
        conversation.record_response(LLMResponse("needle exact match"))
        self.assertFalse(conversation.search("memo-only secret")["matches"])
        self.assertFalse(conversation.search("other private data")["matches"])
        self.assertFalse(conversation.search("other memo")["matches"])
        result = conversation.search("NEEDLE exact")
        self.assertEqual([m["message_index"] for m in result["matches"]], [5, 4])
        self.assertEqual((result["transcript_messages"], result["searched_messages"]), (6, 6))
        self.assertTrue(result["original_transcript_preserved"])
        excluded = conversation.search("needle", exclude=5)
        self.assertEqual([m["message_index"] for m in excluded["matches"]], [4])
        self.assertEqual(excluded["searched_messages"], 5)


class BuilderRuntimeTests(AsyncCase):
    async def test_runtime_uses_builder_for_simple_task_without_plan_or_subagent(self):
        built = []
        build = ContextBuilder.build

        def observe(builder, state):
            messages = build(builder, state)
            messages.append({"role": "assistant", "content": "builder boundary marker"})
            built.append(copy.deepcopy(messages))
            return messages

        provider = FakeProvider(LLMResponse("progress"), complete("finished"))
        with patch.object(ContextBuilder, "build", observe):
            result = await ProblemSolver().solve("simple task", provider=provider, model="fake",
                                                  limits=AgentLimits(context_compact_threshold_chars=None))
        self.assertEqual((result.status, result.output, result.steps), (TaskStatus.COMPLETED, "finished", 2))
        self.assertFalse(result.memos)
        self.assertNotIn("builder boundary marker", str(result.messages))
        for request in provider.requests:
            self.assertIn(request["messages"], built)
            self.assertEqual(request["messages"][-1]["content"], "builder boundary marker")
            self.assertEqual({tool["name"] for tool in request["tools"]},
                             {"host_ask", "memo_write", "conversation_search", "conversation_read", "task_complete",
                              "plan_create", "plan_read", "plan_update", "plan_revise",
                              "parent_ask", "parent_notify", "agent_reply", "agent_wait_reply",
                              "workspace_request", "resource_forward", "resource_grant",
                              "resource_wait", "resource_status", "resource_deny",
                              "tool_request", "tool_forward", "tool_wait", "tool_status"})

    async def test_memo_survives_compaction_with_same_source_in_context_events_and_result(self):
        provider = FakeProvider(
            calls(("memo_write", {"content": "confirmed observation", "kind": "discovery",
                                    "source_refs": ["file:line:3"]})),
            LLMResponse("recent progress"), LLMResponse("summary"), complete(),
        )
        session = await self.start(provider, limits=AgentLimits(context_compact_threshold_chars=256,
                                                               context_keep_recent_messages=1))
        result = await self.result(session)
        self.assertEqual((result.status, result.context_compactions), (TaskStatus.COMPLETED, 1))
        self.assertIsInstance(result.memos[0], TaskMemo)
        memo = result.memos[0]
        self.assertEqual((memo.task_id, memo.source_refs, memo.created_step),
                         (result.task_id, ("file:line:3",), 1))
        for request_index in (1, 3):
            messages = provider.requests[request_index]["messages"]
            memo_message = next(m for m in messages if m.get("content", "").startswith("Task memos"))
            self.assertEqual(memo_message["role"], "assistant")
            self.assertEqual(json.loads(memo_message["content"].split("\n", 1)[1]),
                             [{**asdict(memo), "source_refs": list(memo.source_refs)}])
            self.assert_balanced(messages)
        self.assertEqual(provider.requests[2]["tools"], [])
        self.assertEqual(tool_results(result)[0]["output"],
                         {"memo_id": memo.memo_id, "available_in_context": True})
        events = await events_of(session)
        written = [event for event in events if event.type == "task_memo_written"]
        self.assertEqual([event.payload for event in written], [asdict(memo)])
        self.assertEqual(events[-1].payload["memo_items"], 1)
        self.assertNotIn("memory_items", events[-1].payload)
        written[0].payload["content"] = "tampered"
        self.assertEqual((await session.result()).memos, (memo,))

    async def test_large_tool_tail_is_readable_through_runtime_after_bounded_context(self):
        value = "large output " * 200 + "unique end marker"

        def search_tail(request):
            content = request["messages"][-1]["content"]
            self.assertLessEqual(len(content), 1024)
            self.assertTrue(json.loads(content)["truncated"])
            return calls(("conversation_search", {"query": "unique end marker"}))

        def read_tail(request):
            search = json.loads(request["messages"][-1]["content"])["output"]
            return calls(("conversation_read", {"message_index": search["matches"][0]["message_index"],
                                                 "char_offset": len(value) - 120,
                                                 "max_chars": 256}))

        def finish(request):
            page = json.loads(request["messages"][-1]["content"])["output"]
            self.assertIn("unique end marker", page["serialized_message_chunk"])
            return complete()

        provider = FakeProvider(calls(("large", {})), search_tail, read_tail, finish)
        session = await self.start(provider, registry=registry_for(large=lambda: value),
                                  limits=AgentLimits(max_tool_output_chars=1024,
                                                     context_compact_threshold_chars=None))
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        outputs = tool_results(result)
        self.assertEqual(outputs[0]["output"], value)
        self.assertEqual(outputs[1]["output"]["matches"][0]["message_index"], 3)
        self.assertIn("unique end marker", outputs[2]["output"]["serialized_message_chunk"])
        self.assert_balanced(result.messages)
        for request in provider.requests:
            self.assert_balanced(request["messages"])
