import json
from typing import Any

from ..base import BaseLLM, build_function_tools
from ..types import ChatOptions, LLMResponse, ToolCall


class OpenAILLM(BaseLLM):

    def __init__(
        self,
        base_url: str = "http://localhost:8000/v1",
        api_key: str = "EMPTY",
    ):
        try:
            from openai import OpenAI
        except ImportError as e:
            raise RuntimeError(
                "OpenAI Compatible provider를 사용하려면 "
                "'openai' 패키지가 필요합니다. "
                "pip install openai"
            ) from e

        self.base_url = base_url
        self.api_key = api_key

        self.client = OpenAI(
            base_url=base_url,
            api_key=api_key,
        )

    def get_available_models(self) -> list[str]:
        response = self.client.models.list()

        return [
            model.id
            for model in response.data
            if model.id
        ]

    def models(self) -> list[str]:
        return self.get_available_models()

    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: ChatOptions | None = None,
    ) -> LLMResponse:

        options = options or ChatOptions()

        api_messages = self._convert_messages(messages)
        api_tools = build_function_tools(tools)

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": api_messages,
        }

        if api_tools:
            kwargs["tools"] = api_tools

        # -----------------------------
        # Common options.
        # -----------------------------

        if options.temperature is not None:
            kwargs["temperature"] = options.temperature

        if options.top_p is not None:
            kwargs["top_p"] = options.top_p

        if options.max_tokens is not None:
            kwargs["max_tokens"] = options.max_tokens

        if options.seed is not None:
            kwargs["seed"] = options.seed

        if options.stop is not None:
            kwargs["stop"] = options.stop

        # -----------------------------
        # OpenAI-compatible-specific options.
        # -----------------------------
        #
        # vLLM example:
        #
        # extra={
        #     "extra_body": {
        #         "top_k": 40
        #     }
        # }
        #

        extra_body = options.extra.get("extra_body")

        if isinstance(extra_body, dict):
            kwargs["extra_body"] = extra_body

        # To pass additional OpenAI ChatCompletion options directly:
        #
        # extra={
        #     "request": {
        #         "frequency_penalty": 0.2,
        #         "presence_penalty": 0.1
        #     }
        # }
        request_options = options.extra.get("request")

        if isinstance(request_options, dict):
            kwargs.update(request_options)

        response = self.client.chat.completions.create(
            **kwargs
        )

        choice = response.choices[0]
        message = choice.message

        tool_calls: list[ToolCall] = []

        if message.tool_calls:
            for call in message.tool_calls:

                arguments = self._parse_arguments(
                    call.function.arguments
                )

                tool_calls.append(
                    ToolCall(
                        id=call.id,
                        name=call.function.name,
                        arguments=arguments,
                    )
                )

        usage = (
            response.usage.model_dump(exclude_none=True)
            if response.usage is not None
            else None
        )

        return LLMResponse(
            content=message.content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason,
            thinking=self._extract_thinking(message),
            usage=usage,
        )

    def _convert_messages(
        self,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:

        result: list[dict[str, Any]] = []

        for message in messages:

            role = message["role"]

            # Assistant Tool Call
            if role == "assistant" and message.get("tool_calls"):

                tool_calls = []

                for call in message["tool_calls"]:

                    tool_calls.append(
                        {
                            "id": call["id"],
                            "type": "function",
                            "function": {
                                "name": call["name"],
                                "arguments": json.dumps(
                                    call["arguments"],
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    )

                result.append(
                    {
                        "role": "assistant",
                        "content": message.get("content", ""),
                        "tool_calls": tool_calls,
                    }
                )

            # Tool execution result.
            elif role == "tool":

                result.append(
                    {
                        "role": "tool",
                        "tool_call_id": message["tool_call_id"],
                        "content": str(message["content"]),
                    }
                )

            else:

                result.append(
                    {
                        "role": role,
                        "content": message.get("content", ""),
                    }
                )

        return result

    @staticmethod
    def _extract_thinking(message: Any) -> str | None:
        """Read only reasoning fields exposed by OpenAI-compatible servers.

        Returns None when the official Chat Completions API does not provide internal reasoning.
        """
        for name in ("reasoning_content", "reasoning"):
            value = getattr(message, name, None)
            if isinstance(value, str):
                return value

        extra = getattr(message, "model_extra", None)
        if isinstance(extra, dict):
            for name in ("reasoning_content", "reasoning"):
                value = extra.get(name)
                if isinstance(value, str):
                    return value

        return None

    @staticmethod
    def _parse_arguments(
        arguments: str,
    ) -> dict[str, Any]:

        if not arguments:
            return {}

        try:
            result = json.loads(arguments)

            if isinstance(result, dict):
                return result

        except json.JSONDecodeError:
            pass

        # Do not discard the call if JSON parsing fails.
        return {
            "_raw": arguments
        }