from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

# The Runaway Conversation Guard's check before each provider HTTP attempt after the first (app/services/ai_guard.py):
# set by the agent engine around a metered call, so an adapter's own retries are reserved like the call itself.
_attempt_gate: ContextVar[Callable[[int], bool] | None] = ContextVar("duka_attempt_gate", default=None)


def may_send_attempt(attempt: int) -> bool:
    """Ask before sending HTTP attempt `attempt` (1-based) of the current model call. True outside a metered call."""
    gate = _attempt_gate.get()
    return True if gate is None else gate(attempt)


@contextmanager
def attempt_gate(gate: Callable[[int], bool]) -> Iterator[None]:
    token = _attempt_gate.set(gate)
    try:
        yield
    finally:
        _attempt_gate.reset(token)


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
    model: str | None = None  # the model the provider says served the request (None: it did not say)
    attempts: int = 1  # requests sent to the provider for this response (more than 1 after retries)


class LLMError(Exception):
    """`attempts`: requests sent to the provider before it gave up (0 = none was sent)."""

    def __init__(self, *args: object, attempts: int = 1):
        super().__init__(*args)
        self.attempts = attempts


class LLMProvider(ABC):
    """Provider-agnostic chat interface using OpenAI-style message dicts:
    {"role": "system"|"user"|"assistant"|"tool", "content": ..., "tool_calls": [...], "tool_call_id": ...}"""

    name: str
    is_llm: bool = True  # False for the deterministic rules engine
    # The agent engine records every call of an LLM provider in usage_events (app/services/usage_service.py).
    # False for calls that are never a tenant's usage: no model behind the provider, evaluation runs.
    metered: bool = True

    @abstractmethod
    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, model: str | None = None,
                 temperature: float = 0.2, timeout: float | None = None) -> LLMResponse:
        """`timeout`: seconds left in the caller's turn budget; the provider must not exceed it."""
