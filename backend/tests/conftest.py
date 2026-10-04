"""Test harness: real PostgreSQL + pgvector (no SQLite fakes). The schema is built with
`alembic upgrade head` on a clean database, proving migrations work from scratch."""
import os

os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://commerce:commerce@localhost:5432/commerce_test")
os.environ["APP_ENV"] = "test"
os.environ["LLM_PROVIDER"] = "rules"
os.environ["EMBEDDING_PROVIDER"] = "hash"
os.environ["PAYMENT_WEBHOOK_SECRET"] = "test-payment-secret"
os.environ["WHATSAPP_VERIFY_TOKEN"] = "test-verify-token"
os.environ["WHATSAPP_APP_SECRET"] = ""
os.environ["JWT_SECRET"] = "test-jwt-secret-0123456789abcdef"
os.environ["BACKGROUND_WORKERS"] = "0"  # tests drain the queue explicitly (see Tenant.send / drain())

import hashlib  # noqa: E402
import hmac  # noqa: E402
import json  # noqa: E402
import uuid  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from alembic import command  # noqa: E402
from app.agents.providers import set_provider_override  # noqa: E402
from app.core.ratelimit import auth_limiter, inbound_message_limiter  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402
from app.integrations.whatsapp.adapters import SendResult, WhatsAppAdapter, set_adapter_override  # noqa: E402
from app.integrations.whatsapp.parser import build_text_webhook  # noqa: E402
from app.main import app  # noqa: E402
from app.workflows.inbound import run_due  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session", autouse=True)
def migrated_db():
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    command.upgrade(cfg, "head")
    yield


@pytest.fixture(autouse=True)
def clean_db():
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    auth_limiter.reset()
    inbound_message_limiter.reset()
    set_provider_override(None)
    set_adapter_override(None)
    yield
    set_provider_override(None)
    set_adapter_override(None)


@pytest.fixture
def db():
    s = SessionLocal()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def client():
    return TestClient(app)


def drain() -> list:
    """Do what the background workers do in production: process every due webhook event."""
    return run_due(SessionLocal)


class CapturingAdapter(WhatsAppAdapter):
    """Records outbound WhatsApp messages instead of sending them."""
    mode = "test"

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send_text(self, to: str, body: str) -> SendResult:
        self.sent.append((to, body))
        return SendResult(ok=True, wa_message_id=f"wamid.out.{uuid.uuid4().hex[:8]}", delivery_status="sent")


@pytest.fixture
def outbox():
    a = CapturingAdapter()
    set_adapter_override(a)
    return a


class Tenant:
    def __init__(self, client: TestClient, name: str, phone_number_id: str):
        self.client = client
        email = f"{name.lower().replace(' ', '').replace(chr(39), '')}-{uuid.uuid4().hex[:6]}@test.dev"
        r = client.post("/api/auth/register", json={"business_name": name, "email": email, "password": "password123"})
        assert r.status_code == 201, r.text
        self.token = r.json()["access_token"]
        self.business_id = r.json()["business_id"]
        self.h = {"Authorization": f"Bearer {self.token}"}
        self.phone_number_id = phone_number_id
        r = client.post("/api/whatsapp/accounts", headers=self.h,
                        json={"phone_number_id": phone_number_id, "display_phone_number": "+250700", "mode": "dev"})
        assert r.status_code == 201, r.text

    def get(self, path, **kw):
        return self.client.get(path, headers=self.h, **kw)

    def post(self, path, **kw):
        return self.client.post(path, headers=self.h, **kw)

    def patch(self, path, **kw):
        return self.client.patch(path, headers=self.h, **kw)

    def delete(self, path, **kw):
        return self.client.delete(path, headers=self.h, **kw)

    def import_csv(self, csv_text: str, **params):
        return self.post("/api/products/import", files={"file": ("p.csv", csv_text.encode(), "text/csv")},
                         params=params)

    def zone(self, name="Kigali", fee=2000, areas=("Kigali", "Remera"), default=True):
        r = self.post("/api/delivery-zones", json={"name": name, "fee": fee, "areas": list(areas),
                                                   "is_default": default})
        assert r.status_code == 201, r.text
        return r.json()

    def send(self, text_: str, from_number="250788111222", wa_id=None, sign_secret: str | None = None,
             process: bool = True):
        payload = build_text_webhook(self.phone_number_id, "+250700", from_number, text_,
                                     wa_id or f"wamid.{uuid.uuid4().hex}", "Test Customer")
        raw = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        if sign_secret:
            headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(sign_secret.encode(), raw, hashlib.sha256).hexdigest()
        r = self.client.post("/webhooks/whatsapp", content=raw, headers=headers)
        if process:
            drain()
        return r


FASHION_CSV = (BACKEND / "seed/data/kigali_fashion_products.csv").read_text()
ELECTRONICS_CSV = (BACKEND / "seed/data/mamas_electronics_products.csv").read_text()


@pytest.fixture
def fashion(client):
    t = Tenant(client, "Kigali Fashion", "pnid-fashion")
    assert t.import_csv(FASHION_CSV).json()["created"] == 20
    t.zone("Kigali City", 2000, ["Kigali", "Remera", "Kicukiro"], True)
    t.zone("Outside Kigali", 5000, ["Musanze", "Huye"], False)
    return t


@pytest.fixture
def electronics(client):
    t = Tenant(client, "Mama's Electronics", "pnid-electronics")
    assert t.import_csv(ELECTRONICS_CSV).json()["created"] == 12
    t.zone("Kigali", 3000, ["Kigali", "Remera"], True)
    return t


def mock_signature(body: bytes) -> str:
    return hmac.new(b"test-payment-secret", body, hashlib.sha256).hexdigest()
