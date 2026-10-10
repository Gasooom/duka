"""Migration 0010 (WhatsApp usage in usage_events) on scratch databases of its own: upgrading keeps the AI rows as they
are, the ledger stays insert-only, and downgrading is refused while WhatsApp usage exists (the rows could only be
removed by defeating the append-only trigger) but works, and can be redone, while there is none."""
import uuid
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from app.core.config import settings
from app.db.session import engine

BACKEND = Path(__file__).resolve().parents[1]
BUSINESS = uuid.uuid4()
NEW_COLUMNS = {"is_real", "message_kind", "template_name", "market"}


@pytest.fixture
def scratch(monkeypatch):
    """A database nobody else uses (the test database is rebuilt by other tests), migrated by the real Alembic env."""
    name = f"duka_mig_{uuid.uuid4().hex[:8]}"
    admin = create_engine(engine.url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = engine.url.set(database=name)
    monkeypatch.setattr(settings, "database_url", url.render_as_string(hide_password=False))
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    eng = create_engine(url)
    yield cfg, eng
    eng.dispose()
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
    admin.dispose()


def version(eng) -> str:
    with eng.connect() as conn:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()


def columns(eng) -> set[str]:
    with eng.connect() as conn:
        return set(conn.execute(text("SELECT column_name FROM information_schema.columns "
                                     "WHERE table_name = 'usage_events'")).scalars())


def insert_business(eng) -> None:
    with eng.begin() as conn:
        conn.execute(text(
            "INSERT INTO businesses (id, name, slug, business_type, currency, timezone, language, business_hours, "
            "order_prefix, delivery_enabled, payment_enabled, human_handoff_enabled, is_active) VALUES "
            "(:id, 'Migration Shop', 'migration-shop', 'retail', 'RWF', 'Africa/Kigali', 'en', '{}', 'ORD', true, "
            "true, true, true)"), {"id": BUSINESS})


def insert_event(eng, kind: str, key: str, status: str, **extra) -> None:
    cols = {"id": uuid.uuid4(), "business_id": BUSINESS, "kind": kind, "idempotency_key": key, "status": status,
            **extra}
    with eng.begin() as conn:
        conn.execute(text(f"INSERT INTO usage_events ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})"),
                     cols)


def test_upgrade_keeps_ai_usage_and_adds_whatsapp_usage(scratch):
    cfg, eng = scratch
    command.upgrade(cfg, "0009")
    assert NEW_COLUMNS.isdisjoint(columns(eng))
    insert_business(eng)
    insert_event(eng, "llm_call", "llm:before", "success", input_tokens=7)
    command.upgrade(cfg, "0010")
    assert version(eng) == "0010" and NEW_COLUMNS <= columns(eng)
    with eng.connect() as conn:
        row = conn.execute(text("SELECT kind, input_tokens, is_real, message_kind, template_name, market "
                                "FROM usage_events")).one()
    assert tuple(row) == ("llm_call", 7, None, None, None, None)  # untouched, nothing backfilled
    insert_event(eng, "wa_out", "wa_out:x:1", "success", is_real=True, message_kind="free_form", market="250")
    with pytest.raises(IntegrityError, match="ck_usage_events_wa_fields"):  # an AI row never carries WhatsApp fields
        insert_event(eng, "llm_call", "llm:after", "success", is_real=False)
    with pytest.raises(IntegrityError, match="append-only"):  # still insert-only, the new columns included
        with eng.begin() as conn:
            conn.execute(text("UPDATE usage_events SET is_real = false"))


def test_downgrade_is_refused_while_whatsapp_usage_exists(scratch):
    cfg, eng = scratch
    command.upgrade(cfg, "0010")
    insert_business(eng)
    insert_event(eng, "llm_call", "llm:kept", "success")
    insert_event(eng, "wa_in", "wa_in:wamid.1", "received", is_real=False, market="250")
    with pytest.raises(RuntimeError, match="Cannot downgrade 0010"):
        command.downgrade(cfg, "0009")
    assert version(eng) == "0010" and NEW_COLUMNS <= columns(eng)  # nothing was half done
    with eng.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM usage_events")).scalar() == 2


def test_downgrade_without_whatsapp_usage_restores_0009_and_can_be_redone(scratch):
    cfg, eng = scratch
    command.upgrade(cfg, "0010")
    insert_business(eng)
    insert_event(eng, "llm_call", "llm:kept", "success", input_tokens=3)
    command.downgrade(cfg, "0009")
    assert version(eng) == "0009" and NEW_COLUMNS.isdisjoint(columns(eng))
    with pytest.raises(IntegrityError, match="ck_usage_events_kind"):
        insert_event(eng, "wa_in", "wa_in:wamid.1", "received")
    with eng.connect() as conn:
        assert conn.execute(text("SELECT input_tokens FROM usage_events")).scalar() == 3
    command.upgrade(cfg, "head")
    assert version(eng) == "0010"
    insert_event(eng, "wa_in", "wa_in:wamid.1", "received", is_real=True)


def test_the_models_describe_the_migrated_schema(scratch):
    """`alembic check`: autogenerate finds nothing left to migrate."""
    cfg, eng = scratch
    command.upgrade(cfg, "head")
    command.check(cfg)
