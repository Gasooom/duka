"""The platform decides which LLM models a business may use: LLM_MODEL (the default) plus LLM_ALLOWED_MODELS.
Anything else is refused when the setting is saved; a stored choice that is no longer allowed falls back to the
default instead of breaking replies."""
import logging

import pytest
from pydantic import ValidationError as SettingsError
from sqlalchemy import select, update

from app.agents.providers import LLMProvider, LLMResponse, set_provider_override
from app.core.config import Settings, settings
from app.db.session import SessionLocal
from app.models import AgentConfig, AgentRun


class ModelSpy(LLMProvider):
    name = "spy"

    def __init__(self):
        self.models = []

    def complete(self, messages, tools, *, model=None, temperature=0.2, timeout=None):
        self.models.append(model)
        return LLMResponse(content="Which colour would you like?", model=model or "provider-default")


def _model(t) -> str | None:
    with SessionLocal() as s:
        return s.scalar(select(AgentConfig.model).where(AgentConfig.business_id == t.business_id))


def test_allowlist_is_the_default_plus_configured_models():
    assert Settings(llm_model="base-model", llm_allowed_models="").allowed_llm_models == ["base-model"]
    s = Settings(llm_model="base-model", llm_allowed_models=" fast-model, ,base-model,big-model ")
    assert s.allowed_llm_models == ["base-model", "fast-model", "big-model"]
    assert s.permitted_llm_model("big-model") == "big-model"
    assert s.permitted_llm_model("unknown-model") is None and s.permitted_llm_model(None) is None


def test_only_allowed_models_can_be_saved(fashion):
    default = settings.llm_model
    r = fashion.patch("/api/business/agent-config", json={"model": "gpt-9-ultra-expensive"})
    assert r.status_code == 422 and r.json()["errors"][0]["field"] == "model"
    assert "not available on this platform" in r.json()["errors"][0]["message"] and default in r.text
    assert _model(fashion) is None  # nothing was stored
    assert fashion.patch("/api/business/agent-config", json={"model": default}).json()["model"] == default
    for blank in ("", "  ", None):  # blank = back to the platform default
        assert fashion.patch("/api/business/agent-config", json={"model": blank}).json()["model"] is None
    assert fashion.patch("/api/business/agent-config", json={"tone": "warm"}).status_code == 200  # other fields


def test_operator_can_offer_more_models(fashion, monkeypatch):
    monkeypatch.setattr(settings, "llm_allowed_models", "fast-model,big-model")
    cfg = fashion.get("/api/business/agent-config").json()
    assert cfg["default_model"] == settings.llm_model
    assert cfg["available_models"] == [settings.llm_model, "fast-model", "big-model"]
    assert fashion.patch("/api/business/agent-config", json={"model": "big-model"}).status_code == 200
    monkeypatch.setattr(settings, "llm_allowed_models", "fast-model")
    assert fashion.patch("/api/business/agent-config", json={"model": "big-model"}).status_code == 422


def test_the_assistant_calls_the_chosen_allowed_model(fashion, outbox, monkeypatch):
    monkeypatch.setattr(settings, "llm_allowed_models", "big-model")
    fashion.patch("/api/business/agent-config", json={"model": "big-model"})
    spy = ModelSpy()
    set_provider_override(spy)
    fashion.send("do you have jackets?")
    assert spy.models == ["big-model"]
    with SessionLocal() as s:
        assert s.scalar(select(AgentRun.model)) == "big-model"


def test_a_stored_model_no_longer_allowed_falls_back_to_the_default(fashion, outbox, caplog):
    with SessionLocal() as s:  # e.g. saved before the allowlist existed, or since removed from LLM_ALLOWED_MODELS
        s.execute(update(AgentConfig).where(AgentConfig.business_id == fashion.business_id)
                  .values(model="retired-model"))
        s.commit()
    spy = ModelSpy()
    set_provider_override(spy)
    with caplog.at_level(logging.WARNING, logger="app"):
        fashion.send("do you have jackets?")
    assert spy.models == [None]  # None = the provider's own default (LLM_MODEL), never the retired model
    assert outbox.sent[-1][1] == "Which colour would you like?"
    with SessionLocal() as s:
        assert s.scalar(select(AgentRun.model)) == settings.llm_model
    assert any(r.getMessage() == "agent.model_not_allowed" for r in caplog.records)
    cfg = fashion.get("/api/business/agent-config").json()  # shown as is, so the dashboard can say it is unused
    assert cfg["model"] == "retired-model" and "retired-model" not in cfg["available_models"]


def test_the_default_needs_no_configuration(fashion):
    cfg = fashion.get("/api/business/agent-config").json()
    assert cfg["model"] is None and cfg["available_models"] == [settings.llm_model]


def test_a_commented_out_value_is_refused_like_every_other_setting():
    with pytest.raises(SettingsError):
        Settings(llm_allowed_models="# models go here")
