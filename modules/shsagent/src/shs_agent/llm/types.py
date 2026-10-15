from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    id: str = field(default_factory=lambda: uuid4().hex)


@dataclass
class ChatOptions:
    # Provider parameters
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    seed: int | None = None
    stop: list[str] | None = None

    # Provider options examples:
    #
    # Ollama:
    # {
    #     "think": True,
    #     "keep_alive": "30m",
    #     "options": {
    #         "top_k": 40
    #     }
    # }
    #
    # OpenAI/vLLM:
    # {
    #     "extra_body": {
    #         "top_k": 40
    #     }
    # }
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    # Reasoning text returned by the provider (available only from supported models/servers).
    thinking: str | None = None
    # Common token usage and additional provider-specific metrics.
    usage: dict[str, Any] | None = None

    def to_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {
            "role": "assistant",
            "content": self.content or "",
        }

        # Keep thinking in the internal message representation.
        # Provider adapters should pass this field only to APIs that support it.
        if self.thinking is not None:
            message["thinking"] = self.thinking

        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "name": call.name,
                    "arguments": call.arguments,
                }
                for call in self.tool_calls
            ]

        return message