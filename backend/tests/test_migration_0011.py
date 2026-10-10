"""Migration 0011 (Runaway Conversation Guard counters) on a scratch database of its own: the usage ledger is untouched
by upgrade and downgrade, a downgrade removes only the operational counters, and it can be redone."""
from sqlalchemy import text

from alembic import command
from tests.test_migration_0010 import BUSINESS, insert_business, insert_event, scratch, version  # noqa: F401


def tables(eng) -> set[str]:
    with eng.connect() as conn:
        return set(conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")).scalars())


def ledger(eng) -> list[tuple]:
    with eng.connect() as conn:
        return [tuple(r) for r in conn.execute(text("SELECT idempotency_key, kind, status, is_real FROM usage_events "
                                                    "ORDER BY idempotency_key")).all()]


def test_0011_adds_and_removes_only_the_counters(scratch):  # noqa: F811
    cfg, eng = scratch
    command.upgrade(cfg, "0010")
    insert_business(eng)
    insert_event(eng, "llm_call", "llm:kept", "success")
    insert_event(eng, "wa_in", "wa_in:wamid.1", "received", is_real=True, market="250")
    before = ledger(eng)
    command.upgrade(cfg, "0011")
    assert version(eng) == "0011" and "ai_usage_counters" in tables(eng) and ledger(eng) == before
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO ai_usage_counters (id, business_id, scope, subject_id, period, period_start, "
                          "calls, attempts) VALUES (gen_random_uuid(), :b, 'tenant', :b, 'hour', now(), 3, 4)"),
                     {"b": BUSINESS})
    command.downgrade(cfg, "0010")  # allowed: the counters are operational, not history
    assert version(eng) == "0010" and "ai_usage_counters" not in tables(eng) and ledger(eng) == before
    command.upgrade(cfg, "0011")
    assert version(eng) == "0011" and ledger(eng) == before
