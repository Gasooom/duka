"""Production-mode guards and failure isolation."""
import json

import pytest

from app.core.config import settings


@pytest.fixture
def production(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "whatsapp_app_secret", "")


def test_dev_tools_disabled_in_production(fashion, production, client):
    assert fashion.post("/api/dev/simulate", json={"text": "hi"}).status_code == 404
    body = json.dumps({"reference": "x", "status": "successful"}).encode()
    assert client.post("/webhooks/payments/mock", content=body).status_code == 404


def test_unsigned_webhooks_rejected_in_production(fashion, production):
    assert fashion.send("hi").status_code == 503


def test_encryption_key_required_in_production(production):
    from app.core.security import encrypt_secret
    with pytest.raises(RuntimeError, match="ENCRYPTION_KEY"):
        encrypt_secret("token")


def test_one_bad_message_does_not_block_the_batch(fashion, outbox, monkeypatch):
    """A crash while processing one message rolls back that message only."""
    from app.workflows import inbound
    real = inbound.process_message
    calls = {"n": 0}

    def flaky(db, msg):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real(db, msg)

    monkeypatch.setattr(inbound, "process_message", flaky)
    from app.integrations.whatsapp.parser import build_text_webhook
    p1 = build_text_webhook(fashion.phone_number_id, "1", "250788000001", "black sneakers", "w-a")
    p2 = build_text_webhook(fashion.phone_number_id, "1", "250788000002", "black sneakers", "w-b")
    p1["entry"][0]["changes"].append(p2["entry"][0]["changes"][0])
    results = inbound.process_webhook_payload(p1)
    assert [r.status for r in results] == ["error", "replied"]
    assert len(outbox.sent) == 1


def test_inbound_rate_limit_per_customer(fashion, outbox):
    from app.core.ratelimit import inbound_message_limiter
    inbound_message_limiter.limit = 3
    try:
        for i in range(5):
            fashion.send(f"jeans {i}")
        assert len(outbox.sent) == 3
    finally:
        inbound_message_limiter.limit = 30


def test_settings_reject_env_values_that_are_really_comments():
    """docker compose env_file parses `WHATSAPP_APP_SECRET=   # comment` as the value '# comment'."""
    from pydantic import ValidationError

    from app.core.config import Settings
    with pytest.raises(ValidationError, match="WHATSAPP_APP_SECRET"):
        Settings(whatsapp_app_secret="# (CREDENTIAL) Meta App > Settings > Basic > App secret")


def test_production_refuses_unsafe_configuration():
    from app.core.config import Settings
    unsafe = Settings(app_env="production", jwt_secret="change-me-to-a-long-random-string", encryption_key="",
                      llm_provider="rules", llm_api_key="", whatsapp_app_secret="", whatsapp_verify_token="",
                      database_url="postgresql+psycopg://commerce:commerce@db:5432/commerce")
    problems = " ".join(unsafe.production_problems())
    for needle in ("JWT_SECRET", "ENCRYPTION_KEY", "LLM_PROVIDER", "WHATSAPP_APP_SECRET", "WHATSAPP_VERIFY_TOKEN",
                   "DATABASE_URL"):
        assert needle in problems
    from cryptography.fernet import Fernet
    assert "ENCRYPTION_KEY is not a valid" in " ".join(Settings(app_env="production", encryption_key="k").production_problems())
    assert "PUBLIC_BASE_URL" in problems
    safe = Settings(app_env="production", jwt_secret="x" * 40, encryption_key=Fernet.generate_key().decode(),
                    llm_provider="openai_compat", llm_api_key="sk-test", whatsapp_app_secret="s",
                    whatsapp_verify_token="v" * 20, database_url="postgresql+psycopg://duka:Str0ng@db:5432/duka",
                    public_base_url="https://duka.example.rw")
    assert safe.production_problems() == []


def test_missing_llm_key_behaves_like_an_outage_not_a_crash(fashion, outbox, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "llm_provider", "openai_compat")
    monkeypatch.setattr(settings, "llm_api_key", "")
    fashion.send("black sneakers")
    assert outbox.sent[-1][1].startswith("Sorry, I'm having trouble")
    run = fashion.get(f"/api/conversations/{fashion.get('/api/conversations').json()[0]['id']}").json()["agent_runs"][0]
    assert run["status"] == "error" and "LLM_API_KEY" in run["error"]


def test_simulated_whatsapp_is_refused_in_production(fashion, outbox, monkeypatch, db):
    from app.core.config import settings
    from app.integrations.whatsapp.adapters import get_adapter, set_adapter_override
    from app.models import WhatsAppAccount
    monkeypatch.setattr(settings, "app_env", "production")
    r = fashion.post("/api/whatsapp/accounts", json={"phone_number_id": "pnid-dev-prod", "mode": "dev"})
    assert r.status_code == 422
    set_adapter_override(None)
    acct = db.query(WhatsAppAccount).filter_by(phone_number_id=fashion.phone_number_id).one()  # created in dev
    result = get_adapter(acct).send_text("250788111222", "hi")
    assert not result.ok and "disabled in production" in result.error


def test_expired_and_unsigned_tokens_are_refused(fashion, monkeypatch):
    import base64
    import uuid

    from app.core import security
    me = fashion.get("/api/auth/me").json()["user"]
    monkeypatch.setattr(settings, "jwt_expire_minutes", -1)
    expired = security.create_access_token(uuid.UUID(me["id"]), uuid.UUID(fashion.business_id), me["role"])
    assert fashion.client.get("/api/orders", headers={"Authorization": f"Bearer {expired}"}).status_code == 401
    claims = {"sub": me["id"], "bid": fashion.business_id, "role": me["role"], "tv": 0, "exp": 4102444800}
    part = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    unsigned = f"{part({'alg': 'none', 'typ': 'JWT'})}.{part(claims)}."
    assert fashion.client.get("/api/orders", headers={"Authorization": f"Bearer {unsigned}"}).status_code == 401


def test_hosted_postgres_urls_use_the_installed_driver():
    """Render (and most hosts) give postgresql:// or postgres:// URLs; the app ships psycopg 3, not psycopg2."""
    from app.core.config import Settings
    for url in ("postgresql://u:p@dpg-x-a/duka", "postgres://u:p@dpg-x-a/duka"):
        assert Settings(database_url=url).database_url == "postgresql+psycopg://u:p@dpg-x-a/duka"
    assert Settings(database_url="postgresql+psycopg://u:p@h/d").database_url == "postgresql+psycopg://u:p@h/d"


def test_demo_seed_refuses_production_before_touching_the_database(production, monkeypatch):
    """The seed creates demo accounts with a published password: in production it must stop before any write."""
    from sqlalchemy import func, select

    from app.db.session import SessionLocal
    from app.models import Business, User
    from seed import seed as demo_seed

    def no_database():
        raise AssertionError("the seed opened a database session in production")

    monkeypatch.setattr(demo_seed, "session_scope", no_database)
    with pytest.raises(SystemExit) as stopped:
        demo_seed.main()
    assert "APP_ENV=production" in str(stopped.value.code)
    with SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(Business)) == 0
        assert s.scalar(select(func.count()).select_from(User)) == 0
