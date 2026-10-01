"""OpenAI-compatible Chat Completions provider with tool calling.

Works with any endpoint implementing /chat/completions + tools, so the model vendor is a
config change, not a code change:
  OpenAI      LLM_BASE_URL=https://api.openai.com/v1                              LLM_MODEL=gpt-4o-mini
  Gemini      LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai LLM_MODEL=gemini-2.0-flash
  Groq        LLM_BASE_URL=https://api.groq.com/openai/v1                          LLM_MODEL=llama-3.3-70b-versatile
  OpenRouter  LLM_BASE_URL=https://openrouter.ai/api/v1                            LLM_MODEL=<any>
  DeepSeek    LLM_BASE_URL=https://api.deepseek.com/v1                             LLM_MODEL=deepseek-chat
"""
import json
import time
from typing import Any

import httpx

from app.agents.providers.base import LLMError, LLMProvider, LLMResponse, ToolCall
from app.core.config import settings

RETRYABLE = {408, 409, 429, 500, 502, 503, 504}


class OpenAICompatProvider(LLMProvider):
    name = "openai_compat"

    def __init__(self, base_url: str | None = None, api_key: str | None = None, model: str | None = None,
                 timeout: float | None = None, client: httpx.Client | None = None, max_attempts: int = 3):
        self.base_url = (base_url or settings.llm_base_url).rstrip("/")
        self.api_key = api_key if api_key is not None else settings.llm_api_key
        if not self.api_key:
            raise LLMError("LLM_API_KEY is not set (BLOCKED BY EXTERNAL CREDENTIAL). "
                           "Set LLM_PROVIDER=rules for offline development.")
        self.model = model or settings.llm_model
        self.client = client or httpx.Client(timeout=timeout or settings.llm_timeout_seconds)
        self.max_attempts = max_attempts

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, model: str | None = None,
                 temperature: float = 0.2) -> LLMResponse:
        body: dict[str, Any] = {"model": model or self.model, "messages": messages, "temperature": temperature,
                                "max_tokens": 500}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        last_err = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                r = self.client.post(f"{self.base_url}/chat/completions", json=body,
                                     headers={"Authorization": f"Bearer {self.api_key}"})
                if r.status_code == 200:
                    return self._parse(r.json(), body["model"])
                last_err = f"HTTP {r.status_code}: {r.text[:300]}"
                if r.status_code not in RETRYABLE:
                    break
            except httpx.TransportError as exc:
                last_err = f"{type(exc).__name__}: {exc}"
            if attempt < self.max_attempts:
                time.sleep(0.5 * (2 ** (attempt - 1)))
        raise LLMError(f"LLM request failed: {last_err}")

    @staticmethod
    def _parse(data: dict[str, Any], model: str) -> LLMResponse:
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"Malformed LLM response: {str(data)[:200]}") from exc
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except json.JSONDecodeError:
                args = {"__invalid_json__": raw[:200]}
            calls.append(ToolCall(id=tc.get("id") or f"call_{len(calls)}", name=fn.get("name", ""), arguments=args))
        usage = data.get("usage") or {}
        return LLMResponse(content=msg.get("content"), tool_calls=calls, prompt_tokens=usage.get("prompt_tokens"),
                           completion_tokens=usage.get("completion_tokens"), model=data.get("model") or model)
