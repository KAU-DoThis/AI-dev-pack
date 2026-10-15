from abc import ABC, abstractmethod
from typing import Any

from .types import ChatOptions, LLMResponse


class BaseLLM(ABC):

    @abstractmethod
    def models(self) -> list[str]:
        """
        Return the models available from the current provider.
        """
        pass

    @abstractmethod
    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: ChatOptions | None = None,
    ) -> LLMResponse:
        """
        Send a chat request to the specified model.
        """
        pass


def build_function_tools(
    tools: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """
    Convert the common Tool format from ToolRegistry.schemas()
    to the function tool format used by LLM APIs.

    Input example:

    {
        "name": "add",
        "description": "Add two numbers.",
        "parameters": {...}
    }

    Output example:

    {
        "type": "function",
        "function": {
            "name": "add",
            "description": "...",
            "parameters": {...}
        }
    }
    """

    if not tools:
        return []

    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["parameters"],
            },
        }
        for tool in tools
    ]