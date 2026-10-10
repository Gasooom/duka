"""M8: probes, metrics, log hygiene and retention."""
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.logging import JsonFormatter, log_event, safe_error, scrub
from app.db.session import SessionLocal
from app.models import WebhookEvent
from app.ops import purge_processed_events


def test_liveness_needs_no_dependencies(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_readiness_ok_and_details(client, fashion, outbox):
    fashion.send("black sneakers")
    r = client.get("/readyz", params={"details": True})
    assert r.status_code == 200 and r.json()["status"] == "ok"
    names = {c["name"] for c in r.json()["checks"]}
    assert {"database", "migrations", "workers", "inbound_backlog", "dead_letters_24h", "send_failures_1h",
            "agent_errors_1h", "owner_alert_failures_24h"} <= names


def test_readiness_degrades_on_dead_letters_and_goes_down_on_stuck_backlog(client, fashion, outbox, db):
    fashion.send("black sneakers", process=False)
    db.execute(update(WebhookEvent).values(status="dead"))
    db.commit()
    assert client.get("/readyz").json()["status"] == "degraded"
    fashion.send("black sneakers again", process=False)
    db.execute(update(WebhookEvent).where(WebhookEvent.status == "pending")
               .values(created_at=datetime.now(timezone.utc) - timedelta(minutes=15)))
    db.commit()
    r = client.get("/readyz")
    assert r.status_code == 503 and r.json()["status"] == "down"


def test_readiness_down_when_workers_expected_but_dead(client, monkeypatch):
    monkeypatch.setattr(settings, "background_workers", 2)  # configured, but no thread runs in tests
    assert client.get("/readyz").status_code == 503


def test_readiness_down_when_migrations_are_behind(client, monkeypatch):
    from app import ops
    monkeypatch.setattr(ops, "_HEAD", "9999")
    r = client.get("/readyz", params={"details": True})
    assert r.status_code == 503 and any(c["name"] == "migrations" and c["level"] == "down" for c in r.json()["checks"])


def test_metrics_exposition(client, fashion, outbox):
    fashion.send("black sneakers")
    body = client.get("/metrics").text
    for series in ("duka_up 1", 'duka_webhook_events{status="done"} 1', 'duka_agent_runs_1h{status="success"} 1',
                   'duka_agent_latency_ms_1h{quantile="0.95"}', "duka_inbound_oldest_pending_seconds 0",
                   "duka_orders_created_24h 0", "# TYPE duka_outbox_messages gauge"):
        assert series in body, series


def test_ops_endpoints_need_the_token_in_production(client, monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    assert client.get("/metrics").status_code == 404
    assert "checks" not in client.get("/readyz", params={"details": True}).json()
    monkeypatch.setattr(settings, "ops_token", "ops-secret-123")
    assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 404
    assert client.get("/metrics", headers={"Authorization": "Bearer ops-secret-123"}).status_code == 200
    assert "checks" in client.get("/readyz", params={"details": True},
                                  headers={"Authorization": "Bearer ops-secret-123"}).json()


# ---------------------------------------------------------------- logs never carry secrets or customer data
def _formatted(fn) -> list[dict]:
    records: list[logging.LogRecord] = []

    class Grab(logging.Handler):
        def emit(self, record):
            records.append(record)

    h = Grab()
    logging.getLogger().addHandler(h)
    try:
        fn()
    finally:
        logging.getLogger().removeHandler(h)
    fmt = JsonFormatter()
    return [json.loads(fmt.format(r)) for r in records]


def test_formatter_redacts_secrets_and_masks_phone_numbers():
    [line] = _formatted(lambda: log_event(logging.getLogger("t"), "x", api_key="sk-live-123", token="EAAG123",
                                          error="call to +250788123456 failed: Bearer abc.def",
                                          phone_number_id="123456789012345"))
    blob = json.dumps(line)
    assert "sk-live-123" not in blob and "EAAG123" not in blob and "250788123456" not in blob and "abc.def" not in blob
    assert line["error"] == "call to ***456 failed: Bearer ***"
    assert line["phone_number_id"] == "123456789012345"  # a business identifier, kept for debugging


def test_database_errors_are_logged_without_sql_parameters(db, fashion):
    with pytest.raises(IntegrityError) as info:
        db.execute(text("INSERT INTO customers (id, business_id, whatsapp_number, metadata) "
                        "VALUES (:i, :b, :n, '{}'), (:i2, :b, :n, '{}')"),
                   {"i": uuid.uuid4(), "i2": uuid.uuid4(), "b": uuid.UUID(fashion.business_id), "n": "250788999111"})
    db.rollback()
    assert "250788999111" in str(info.value)  # the raw exception does contain customer data...
    cleaned = safe_error(info.value)
    assert "250788999111" not in cleaned and "[parameters: ***]" in cleaned and "IntegrityError" in cleaned


def test_access_log_never_records_query_strings(client):
    lines = _formatted(lambda: client.get("/webhooks/whatsapp", params={
        "hub.mode": "subscribe", "hub.verify_token": "test-verify-token", "hub.challenge": "1"}))
    access = [line for line in lines if line.get("msg") == "http.request"]
    assert access and access[0]["path"] == "/webhooks/whatsapp" and access[0]["status"] == 200
    assert "test-verify-token" not in json.dumps(lines)


def test_scrub_examples():
    assert scrub("https://graph.facebook.com/x?access_token=EAAB123&y=1") == "https://graph.facebook.com/x?access_token=***&y=1"
    assert scrub("amount 1,200,000 RWF") == "amount 1,200,000 RWF"  # money is not a phone number


# ---------------------------------------------------------------- retention
def test_processed_webhook_payloads_are_purged_after_retention(fashion, outbox, db):
    fashion.send("old message")
    fashion.send("recent message")
    fashion.send("still pending", process=False)
    db.execute(update(WebhookEvent).where(WebhookEvent.payload["text"].astext == "old message")
               .values(updated_at=datetime.now(timezone.utc) - timedelta(days=40)))
    db.commit()
    with SessionLocal() as s:
        assert purge_processed_events(s, 30) == 1
        s.commit()
    db.expire_all()
    left = {e.payload["text"]: e.status for e in db.query(WebhookEvent)}
    assert left == {"recent message": "done", "still pending": "pending"}


def test_operator_can_requeue_dead_letters(fashion, outbox, db, monkeypatch):
    from app.cli import main
    from app.workflows import inbound
    from tests.conftest import drain
    real = inbound.process_message
    monkeypatch.setattr(inbound, "process_message", lambda db_, msg, **kw: (_ for _ in ()).throw(RuntimeError("bug")))
    fashion.send("black sneakers")
    db.execute(update(WebhookEvent).values(status="dead"))
    db.commit()
    monkeypatch.setattr(inbound, "process_message", real)  # the bug is fixed and deployed
    assert main(["requeue-dead"]) == 0
    drain()
    db.expire_all()
    assert db.query(WebhookEvent).one().status == "done" and len(outbox.sent) == 1
