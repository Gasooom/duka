from app.agents.providers.base import LLMError, LLMProvider, LLMResponse, ToolCall
from app.core.config import settings

_override: LLMProvider | None = None


def set_provider_override(provider: LLMProvider | None) -> None:
    global _override
    _override = provider


class UnavailableProvider(LLMProvider):
    """Stands in for a provider that cannot be built (e.g. missing key): every call fails like an outage, so the
    customer gets the fallback message and the run is recorded as an error instead of the turn crashing."""
    name = "unavailable"

    def __init__(self, reason: str):
        self.reason = reason

    def complete(self, messages, tools, *, model=None, temperature=0.2, timeout=None) -> LLMResponse:
        raise LLMError(self.reason)


def get_llm_provider() -> LLMProvider:
    if _override is not None:
        return _override
    if settings.llm_provider == "openai_compat":
        from app.agents.providers.openai_compat import OpenAICompatProvider
        try:
            return OpenAICompatProvider()
        except LLMError as exc:
            return UnavailableProvider(str(exc))
    from app.agents.providers.rules import RulesProvider
    return RulesProvider()


__all__ = ["LLMError", "LLMProvider", "LLMResponse", "ToolCall", "get_llm_provider", "set_provider_override"]
