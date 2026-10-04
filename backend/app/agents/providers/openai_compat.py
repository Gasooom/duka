"""OpenAI-compatible Chat Completions provider with tool calling — the production LLM adapter.

Works with any endpoint implementing /chat/completions + tools, so the model vendor is a
config change, not a code change:
  OpenAI      LLM_BASE_URL=https://api.openai.com/v1                              LLM_MODEL=gpt-4o-mini
  Gemini      LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai LLM_MODEL=gemini-2.0-flash
  Groq        LLM_BASE_URL=https://api.groq.com/openai/v1                          LLM_MODEL=llama-3.3-70b-versatile
  OpenRouter  LLM_BASE_URL=https://openrouter.ai/api/v1                            LLM_MODEL=<any>
  DeepSeek    LLM_BASE_URL=https://api.deepseek.com/v1                             LLM_MODEL=deepseek-chat

Reliability: every attempt has a timeout capped by the caller's remaining turn budget; retries are bounded
(LLM_MAX_ATTEMPTS) and only on 408/409/429/5xx/network errors, honouring Retry-After when it fits the budget.
Verify a real key with:  python -m app.cli llm-check
"""
import json
import time
from typing import Any

import httpx

from app.agents.providers.base import LLMError, LLMProvider, LLMResponse, ToolCall
from app.core.config import settings

RETRYABLE = {408, 409, 429, 500, 502, 503, 504}
MIN_ATTEMPT_SECONDS = 2.0


class OpenAICompatProvider(LLMProvider):
    name = "openai_compat"

    def __init__(self, base_url: str | None = None, api_key: str | None = None, model: str | None = None,
                 timeout: float | None = None, client: httpx.Client | None = None, max_attempts: int | None = None):
        self.base_url = (base_url or settings.llm_base_url).rstrip("/")
        self.api_key = api_key if api_key is not None else settings.llm_api_key
        if not self.api_key:
            raise LLMError("LLM_API_KEY is not set (BLOCKED BY EXTERNAL CREDENTIAL). "
                           "Set LLM_PROVIDER=rules for offline development.")
        self.model = model or settings.llm_model
        self.timeout = timeout or settings.llm_timeout_seconds
        self.client = client or httpx.Client(timeout=self.timeout)
        self.max_attempts = max_attempts or settings.llm_max_attempts

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, model: str | None = None,
                 temperature: float = 0.2, timeout: float | None = None) -> LLMResponse:
        body: dict[str, Any] = {"model": model or self.model, "messages": messages, "temperature": temperature,
                                "max_tokens": settings.llm_max_tokens}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        deadline = time.monotonic() + (timeout if timeout is not None else self.timeout * self.max_attempts)
        last_err = None
        for attempt in range(1, self.max_attempts + 1):
            remaining = deadline - time.monotonic()
            if remaining < MIN_ATTEMPT_SECONDS:
                last_err = last_err or "no time left in the turn budget"
                break
            retry_after = None
            try:
                r = self.client.post(f"{self.base_url}/chat/completions", json=body,
                                     headers={"Authorization": f"Bearer {self.api_key}"},
                                     timeout=min(self.timeout, remaining))
                if r.status_code == 200:
                    try:
                        data = r.json()
                    except ValueError as exc:
                        raise LLMError(f"Malformed LLM response (not JSON): {r.text[:200]}") from exc
                    return self._parse(data, body["model"])
                last_err = f"HTTP {r.status_code}: {r.text[:300]}"
                if r.status_code not in RETRYABLE:
                    break
                retry_after = _retry_after(r)
            except httpx.TransportError as exc:  # timeouts, connection errors
                last_err = f"{type(exc).__name__}: {exc}"
            if attempt < self.max_attempts:
                pause = retry_after if retry_after is not None else 0.5 * (2 ** (attempt - 1))
                if deadline - time.monotonic() - pause < MIN_ATTEMPT_SECONDS:
                    break
                time.sleep(pause)
        raise LLMError(f"LLM request failed: {last_err}")

    @staticmethod
    def _parse(data: dict[str, Any], model: str) -> LLMResponse:
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Malformed LLM response: {str(data)[:200]}") from exc
        content = msg.get("content")
        if isinstance(content, list):  # some providers return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw)
                if not isinstance(args, dict):
                    raise ValueError("arguments must be an object")
            except (json.JSONDecodeError, ValueError, TypeError):
                args = {"__invalid_json__": str(raw)[:200]}  # rejected by the tool's schema (extra=forbid)
            calls.append(ToolCall(id=tc.get("id") or f"call_{len(calls)}", name=fn.get("name") or "", arguments=args))
        usage = data.get("usage") or {}
        return LLMResponse(content=content, tool_calls=calls, prompt_tokens=usage.get("prompt_tokens"),
                           completion_tokens=usage.get("completion_tokens"), model=data.get("model") or model)


def _retry_after(r: httpx.Response) -> float | None:
    try:
        return min(float(r.headers.get("retry-after", "")), 10.0)
    except ValueError:
        return None
