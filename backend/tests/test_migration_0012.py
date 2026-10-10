"""Migration 0012 (embeddings requests in usage_events) on scratch databases of its own: upgrading keeps every existing
row as it is and admits the kind `embedding`, without WhatsApp fields; downgrading is refused while embeddings usage
exists (the ledger is insert-only) but works, and can be redone, while there is none. The model's CHECK constraints
are the migrated ones: autogenerate (`alembic check`) does not compare CHECK constraints, so this test does."""
import pytest
from sqlalchemy import CheckConstraint, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from app.models import UsageEvent
from tests.test_migration_0010 import insert_business, insert_event, scratch, version  # noqa: F401
from tests.test_migration_0011 import ledger


def test_upgrade_keeps_the_ledger_and_admits_embedding_usage(scratch):  # noqa: F811
    cfg, eng = scratch
    command.upgrade(cfg, "0011")
    insert_business(eng)
    insert_event(eng, "llm_call", "llm:kept", "success", input_tokens=7)
    insert_event(eng, "wa_out", "wa_out:x:1", "success", is_real=True, message_kind="free_form", market="250")
    with pytest.raises(IntegrityError, match="ck_usage_events_kind"):
        insert_event(eng, "embedding", "emb:early", "success")
    before = ledger(eng)
    command.upgrade(cfg, "0012")
    assert version(eng) == "0012" and ledger(eng) == before  # untouched, nothing backfilled
    insert_event(eng, "embedding", "emb:1", "success", units=3, input_tokens=12, attempts=1)
    insert_event(eng, "embedding", "emb:2", "error", units=1, attempts=3)
    for key, extra in (("emb:3", {"is_real": False}), ("emb:4", {"market": "250"})):
        with pytest.raises(IntegrityError, match="ck_usage_events_wa_fields"):  # never WhatsApp fields
            insert_event(eng, "embedding", key, "success", **extra)
    with pytest.raises(IntegrityError, match="ck_usage_events_wa_fields"):  # the WhatsApp rules still hold
        insert_event(eng, "wa_in", "wa_in:wamid.2", "received")
    with pytest.raises(IntegrityError, match="ck_usage_events_wa_status"):
        insert_event(eng, "wa_in", "wa_in:wamid.3", "error", is_real=True)
    with pytest.raises(IntegrityError, match="append-only"):
        with eng.begin() as conn:
            conn.execute(text("UPDATE usage_events SET units = 0 WHERE kind = 'embedding'"))


def test_downgrade_is_refused_while_embedding_usage_exists(scratch):  # noqa: F811
    cfg, eng = scratch
    command.upgrade(cfg, "0012")
    insert_business(eng)
    insert_event(eng, "embedding", "emb:1", "success", units=1)
    before = ledger(eng)
    with pytest.raises(RuntimeError, match="Cannot downgrade 0012"):
        command.downgrade(cfg, "0011")
    assert version(eng) == "0012" and ledger(eng) == before  # nothing was half done
    insert_event(eng, "embedding", "emb:2", "success", units=1)  # the 0012 checks are still in place


def test_downgrade_without_embedding_usage_restores_0011_and_can_be_redone(scratch):  # noqa: F811
    cfg, eng = scratch
    command.upgrade(cfg, "0012")
    insert_business(eng)
    insert_event(eng, "llm_call", "llm:kept", "success", input_tokens=3)
    insert_event(eng, "wa_in", "wa_in:wamid.1", "received", is_real=True, market="250")
    before = ledger(eng)
    command.downgrade(cfg, "0011")
    assert version(eng) == "0011" and ledger(eng) == before
    with pytest.raises(IntegrityError, match="ck_usage_events_kind"):
        insert_event(eng, "embedding", "emb:1", "success")
    with pytest.raises(IntegrityError, match="ck_usage_events_wa_fields"):  # the 0010 rules are back
        insert_event(eng, "llm_call", "llm:2", "success", is_real=False)
    command.upgrade(cfg, "0012")
    assert version(eng) == "0012"
    insert_event(eng, "embedding", "emb:1", "success")


def test_the_models_check_constraints_are_the_migrated_ones(scratch):  # noqa: F811
    """Each CHECK the model declares on usage_events is added to an empty copy of the migrated table and must read
    back exactly as the migrated one, and the migrated table has no other."""
    cfg, eng = scratch
    command.upgrade(cfg, "head")
    declared = {c.name: str(c.sqltext) for c in UsageEvent.__table__.constraints if isinstance(c, CheckConstraint)}
    with eng.begin() as conn:
        conn.execute(text("CREATE TEMP TABLE model_copy (LIKE usage_events)"))
        for name, sql in declared.items():
            conn.execute(text(f"ALTER TABLE model_copy ADD CONSTRAINT {name} CHECK ({sql})"))
        rows = conn.execute(text(
            "SELECT conrelid = 'usage_events'::regclass, conname, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE contype = 'c' AND conrelid IN ('usage_events'::regclass, 'model_copy'::regclass)")).all()
    migrated = {name: sql for is_migrated, name, sql in rows if is_migrated}
    model = {name: sql for is_migrated, name, sql in rows if not is_migrated}
    assert len(model) == len(declared) == 8 and model == migrated
