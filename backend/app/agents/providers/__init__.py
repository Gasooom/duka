from app.agents.providers.base import LLMError, LLMProvider, LLMResponse, ToolCall
from app.core.config import settings

_override: LLMProvider | None = None


def set_provider_override(provider: LLMProvider | None) -> None:
    global _override
    _override = provider


def get_llm_provider() -> LLMProvider:
    if _override is not None:
        return _override
    if settings.llm_provider == "openai_compat":
        from app.agents.providers.openai_compat import OpenAICompatProvider
        return OpenAICompatProvider()
    from app.agents.providers.rules import RulesProvider
    return RulesProvider()


__all__ = ["LLMError", "LLMProvider", "LLMResponse", "ToolCall", "get_llm_provider", "set_provider_override"]
