import copy
from types import SimpleNamespace as NS

from shs_agent import AgentLimits, TaskStatus
from shs_agent.llm import ChatOptions
from shs_agent.llm.provider.ollama import OllamaLLM
from shs_agent.llm.provider.openai import OpenAILLM

from tests.fakes import AsyncCase
from tests.test_state_details import artifacts, events


def openai_completion(arguments='{"output": "done"}'):
    return NS(id="response-id", model="sdk-model", created=123, object="chat.completion",
              service_tier="default", system_fingerprint="fp", model_extra={"server": "metadata"},
              choices=[NS(index=0, finish_reason="tool_calls", logprobs=None, message=NS(
                  role="assistant", content="visible content", reasoning_content="disclosed reasoning",
                  reasoning="additional disclosed field", refusal=None, model_extra={"custom": "message metadata"},
                  tool_calls=[NS(id="provider-call", type="function", function=NS(name="task_complete", arguments=arguments))]))],
              usage=NS(prompt_tokens=12, completion_tokens=8, total_tokens=20,
                       completion_tokens_details=NS(reasoning_tokens=3),
                       model_dump=lambda **kwargs: {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20,
                                                    "completion_tokens_details": {"reasoning_tokens": 3}}))


class AdapterObservationTests(AsyncCase):
    async def test_openai_sdk_boundary_fields_credentials_and_common_values(self):
        response = openai_completion()
        sent = []

        def sdk(**kwargs):
            sent.append(copy.deepcopy(kwargs))
            kwargs["messages"].clear()  # Recorder must already own its request copy.
            return response

        provider = OpenAILLM.__new__(OpenAILLM)
        provider.client = NS(chat=NS(completions=NS(create=sdk)))
        session = await self.start(provider, options=ChatOptions(temperature=0.2, max_tokens=128,
            extra={"request": {"extra_headers": {"Authorization": "credential-never-export", "X-Trace": "kept"}},
                   "extra_body": {"api_key": "literal-body-field", "custom": 42}}))
        result = await self.result(session)
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        snapshot = result.tracer
        common = artifacts(snapshot, "llm_common_request")[0].data
        adapter = artifacts(snapshot, "llm_adapter_request")[0].data
        self.assertEqual(adapter["boundary"], "sdk_kwargs")
        self.assertEqual(common["options"]["max_tokens"], 128)
        self.assertEqual(adapter["kwargs"]["max_tokens"], 128)
        self.assertEqual(adapter["kwargs"]["tools"][0]["function"], common["tools"][0])
        self.assertTrue(common["messages"])
        self.assertEqual(adapter["kwargs"]["messages"], sent[0]["messages"])
        self.assertEqual(sent[0]["extra_headers"]["Authorization"], "credential-never-export")
        self.assertNotIn("credential-never-export", snapshot.to_json())
        self.assertIn("literal-body-field", snapshot.to_json())
        self.assertEqual(artifacts(snapshot, "llm_adapter_request")[0].issues[0]["category"], "excluded")
        self.assertTrue(snapshot.details_complete)
        raw = artifacts(snapshot, "llm_adapter_response")[0].data["response"]
        message = raw["choices"][0]["message"]
        self.assertEqual(message["content"], "visible content")
        self.assertEqual(message["reasoning_content"], "disclosed reasoning")
        self.assertEqual(message["reasoning"], "additional disclosed field")
        self.assertEqual(message["tool_calls"][0]["function"]["arguments"], '{"output": "done"}')
        self.assertEqual(raw["id"], "response-id")
        self.assertEqual(raw["usage"]["completion_tokens_details"]["reasoning_tokens"], 3)
        normalized = artifacts(snapshot, "llm_normalized_response")[0].data
        self.assertEqual(normalized["thinking"], "disclosed reasoning")
        self.assertEqual(normalized["content"], "visible content")
        self.assertEqual(normalized["tool_calls"][0]["arguments"], {"output": "done"})
        response.choices[0].message.content = "external mutation"
        self.assertEqual(message["content"], "visible content")
        ended = events(snapshot, "llm_call_finished")[0].payload
        self.assertEqual(ended["adapter_observation"], "sdk_boundary")
        self.assertTrue(ended["response_received"])
        self.assertTrue(ended["validation_succeeded"])

    async def test_ollama_sdk_conversion_metadata_and_generated_execution_id(self):
        response = NS(model="sdk-ollama", created_at="2026-10-17T00:00:00Z", done=True, done_reason="stop",
                      total_duration=500, load_duration=50, prompt_eval_count=7, eval_count=9,
                      prompt_eval_duration=100, eval_duration=350, model_extra={"server": "local"},
                      message=NS(role="assistant", content="visible", thinking="disclosed", images=None,
                                 tool_calls=[NS(function=NS(name="task_complete", arguments={"output": "done"}, index=0))]))
        sent = []

        def sdk(**kwargs):
            sent.append(copy.deepcopy(kwargs))
            return response

        provider = OllamaLLM.__new__(OllamaLLM)
        provider.client = NS(chat=sdk)
        session = await self.start(provider, options=ChatOptions(max_tokens=99, temperature=0,
                                                               extra={"think": True, "options": {"top_k": 15}}))
        snapshot = (await self.result(session)).tracer
        adapter = artifacts(snapshot, "llm_adapter_request")[0].data["kwargs"]
        self.assertEqual(adapter, sent[0])
        self.assertEqual(adapter["options"], {"num_predict": 99, "temperature": 0, "top_k": 15})
        self.assertTrue(adapter["think"])
        raw = artifacts(snapshot, "llm_adapter_response")[0].data["response"]
        self.assertEqual(raw["message"]["content"], "visible")
        self.assertEqual(raw["message"]["thinking"], "disclosed")
        self.assertEqual(raw["eval_count"], 9)
        self.assertEqual(raw["total_duration"], 500)
        self.assertNotIn("id", raw["message"]["tool_calls"][0])
        common = artifacts(snapshot, "llm_common_response")[0].data["fields"]
        self.assertTrue(common["tool_calls"][0]["id"])
        self.assertEqual(common["usage"]["total_tokens"], 16)
        self.assertEqual(common["usage"]["total_duration_ns"], 500)
        self.assertTrue(snapshot.details_complete)

    async def test_adapter_conversion_failure_preserves_received_raw_data(self):
        # JSON loads succeeds, but an illegal scalar cannot be parsed as arguments.
        response = openai_completion(arguments=123)
        provider = OpenAILLM.__new__(OpenAILLM)
        provider.client = NS(chat=NS(completions=NS(create=lambda **kwargs: response)))
        snapshot = (await self.result(await self.start(provider))).tracer
        raw = artifacts(snapshot, "llm_adapter_response")[0].data["response"]
        self.assertEqual(raw["choices"][0]["message"]["content"], "visible content")
        self.assertEqual(raw["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"], 123)
        self.assertFalse(artifacts(snapshot, "llm_common_response"))
        ended = events(snapshot, "llm_call_finished")[0].payload
        self.assertEqual(ended["status"], "failed")
        self.assertTrue(ended["response_received"])
        self.assertFalse(ended["validation_succeeded"])
        self.assertEqual(snapshot.state().task.status, TaskStatus.FAILED)

    async def test_raw_malformed_json_is_distinct_from_normalized_fallback_arguments(self):
        response = openai_completion(arguments="not-json")
        provider = OpenAILLM.__new__(OpenAILLM)
        provider.client = NS(chat=NS(completions=NS(create=lambda **kwargs: response)))
        session = await self.start(provider, limits=AgentLimits(max_steps=1))
        snapshot = (await self.result(session)).tracer
        raw = artifacts(snapshot, "llm_adapter_response")[0].data["response"]
        self.assertEqual(raw["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"], "not-json")
        common = artifacts(snapshot, "llm_normalized_response")[0].data
        self.assertEqual(common["tool_calls"][0]["arguments"], {"_raw": "not-json"})
        self.assertEqual(artifacts(snapshot, "tool_result")[0].data["status"], "failed")
        self.assertTrue(snapshot.complete)

    async def test_malformed_tool_shape_does_not_erase_other_received_fields(self):
        response = openai_completion()
        response.choices[0].message.tool_calls = [NS(id="bad", type="function")]
        provider = OpenAILLM.__new__(OpenAILLM)
        provider.client = NS(chat=NS(completions=NS(create=lambda **kwargs: response)))
        snapshot = (await self.result(await self.start(provider))).tracer
        raw = artifacts(snapshot, "llm_adapter_response")[0].data["response"]
        self.assertEqual(raw["choices"][0]["message"]["content"], "visible content")
        self.assertEqual(raw["choices"][0]["message"]["reasoning_content"], "disclosed reasoning")
        self.assertEqual(snapshot.state().task.status, TaskStatus.FAILED)
        self.assertTrue(events(snapshot, "llm_call_finished")[0].payload["response_received"])

