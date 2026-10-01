from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    model: str | None = None


class LLMError(Exception):
    pass


class LLMProvider(ABC):
    """Provider-agnostic chat interface using OpenAI-style message dicts:
    {"role": "system"|"user"|"assistant"|"tool", "content": ..., "tool_calls": [...], "tool_call_id": ...}"""

    name: str
    is_llm: bool = True  # False for the deterministic rules engine

    @abstractmethod
    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, model: str | None = None,
                 temperature: float = 0.2) -> LLMResponse: ...
