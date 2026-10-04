"""Operational status for the platform operator: readiness checks and Prometheus metrics.

Everything is derived from PostgreSQL, so numbers survive restarts and are the same whichever instance answers.
Only aggregate counts across tenants are exposed (no tenant data), and details require OPS_TOKEN in production.

Readiness levels: ok | degraded (works, but something needs a look) | down (customers are affected) -> HTTP 503.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import AgentRun, Message, Notification, Order, WebhookEvent

BACKEND_DIR = Path(__file__).resolve().parents[1]
_HEAD: str | None = None


def alembic_head() -> str | None:
    global _HEAD
    if _HEAD is None:
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        cfg = Config(str(BACKEND_DIR / "alembic.ini"))
        cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
        _HEAD = ScriptDirectory.from_config(cfg).get_current_head()
    return _HEAD


@dataclass
class Check:
    name: str
    level: str  # ok | degraded | down
    detail: str


def readiness(db: Session, workers_running: bool) -> tuple[str, list[Check]]:
    now = datetime.now(timezone.utc)
    checks: list[Check] = []
    try:
        db.execute(text("SELECT 1"))
        checks.append(Check("database", "ok", "reachable"))
    except Exception as exc:  # noqa: BLE001 - any failure here means down
        return "down", [Check("database", "down", type(exc).__name__)]

    current = db.scalar(text("SELECT version_num FROM alembic_version"))
    head = alembic_head()
    checks.append(Check("migrations", "ok" if current == head else "down", f"db={current} code={head}"))

    expected = settings.background_workers > 0
    checks.append(Check("workers", "ok" if workers_running or not expected else "down",
                        "running" if workers_running else "disabled" if not expected else "not running"))

    oldest = db.scalar(select(func.min(WebhookEvent.created_at)).where(WebhookEvent.status.in_(("pending", "retry"))))
    age = (now - oldest).total_seconds() if oldest else 0
    checks.append(Check("inbound_backlog", "down" if age > 600 else "degraded" if age > 120 else "ok",
                        f"oldest unprocessed message {int(age)}s old"))

    dead = db.scalar(select(func.count()).select_from(WebhookEvent).where(
        WebhookEvent.status == "dead", WebhookEvent.updated_at > now - timedelta(hours=24)))
    checks.append(Check("dead_letters_24h", "degraded" if dead else "ok", f"{dead} message(s) could not be processed"))

    failed = db.scalar(select(func.count()).select_from(Message).where(
        Message.delivery_status == "failed", Message.created_at > now - timedelta(hours=1)))
    checks.append(Check("send_failures_1h", "degraded" if failed else "ok", f"{failed} outbound message(s) failed"))

    runs, errors = db.execute(select(func.count(), func.count().filter(AgentRun.status == "error"))
                              .where(AgentRun.created_at > now - timedelta(hours=1))).one()
    rate = errors / runs if runs else 0
    checks.append(Check("agent_errors_1h", "degraded" if runs >= 5 and rate > 0.2 else "ok",
                        f"{errors}/{runs} agent runs failed"))

    n_failed = db.scalar(select(func.count()).select_from(Notification).where(
        Notification.status == "failed", Notification.created_at > now - timedelta(hours=24)))
    checks.append(Check("owner_alert_failures_24h", "degraded" if n_failed else "ok", f"{n_failed} alert(s) failed"))

    level = "down" if any(c.level == "down" for c in checks) else \
        "degraded" if any(c.level == "degraded" for c in checks) else "ok"
    return level, checks


def metrics(db: Session, workers_running: bool) -> str:
    """Prometheus text exposition (gauges computed from the database)."""
    now = datetime.now(timezone.utc)
    hour, day = now - timedelta(hours=1), now - timedelta(hours=24)
    lines: list[str] = []

    def gauge(name: str, help_: str, samples: list[tuple[dict[str, str], float]]) -> None:
        lines.append(f"# HELP {name} {help_}")
        lines.append(f"# TYPE {name} gauge")
        for labels, value in samples:
            lbl = ",".join(f'{k}="{v}"' for k, v in labels.items())
            lines.append(f"{name}{{{lbl}}} {value}" if lbl else f"{name} {value}")

    gauge("duka_up", "1 if the API process is serving.", [({}, 1)])
    gauge("duka_workers_running", "1 if background workers are alive in this process.", [({}, int(workers_running))])
    rows = db.execute(select(WebhookEvent.status, func.count()).group_by(WebhookEvent.status)).all()
    gauge("duka_webhook_events", "Inbound WhatsApp messages by processing status.", [({"status": s}, n) for s, n in rows])
    oldest = db.scalar(select(func.min(WebhookEvent.created_at)).where(WebhookEvent.status.in_(("pending", "retry"))))
    gauge("duka_inbound_oldest_pending_seconds", "Age of the oldest unprocessed inbound message.",
          [({}, round((now - oldest).total_seconds(), 1) if oldest else 0)])
    rows = db.execute(select(Message.delivery_status, func.count())
                      .where(Message.delivery_status.in_(("queued", "sending", "retry", "failed")))
                      .group_by(Message.delivery_status)).all()
    gauge("duka_outbox_messages", "Outbound messages not yet delivered, by status.", [({"status": s}, n) for s, n in rows])
    rows = db.execute(select(AgentRun.status, func.count()).where(AgentRun.created_at > hour)
                      .group_by(AgentRun.status)).all()
    gauge("duka_agent_runs_1h", "Agent turns in the last hour by outcome.", [({"status": s}, n) for s, n in rows])
    p50, p95 = db.execute(select(
        func.percentile_cont(0.5).within_group(AgentRun.latency_ms),
        func.percentile_cont(0.95).within_group(AgentRun.latency_ms)).where(AgentRun.created_at > hour)).one()
    gauge("duka_agent_latency_ms_1h", "Agent turn latency over the last hour.",
          [({"quantile": "0.5"}, round(p50 or 0, 1)), ({"quantile": "0.95"}, round(p95 or 0, 1))])
    pt, ct = db.execute(select(func.coalesce(func.sum(AgentRun.prompt_tokens), 0),
                               func.coalesce(func.sum(AgentRun.completion_tokens), 0))
                        .where(AgentRun.created_at > hour)).one()
    gauge("duka_llm_tokens_1h", "LLM tokens used in the last hour.", [({"type": "prompt"}, pt), ({"type": "completion"}, ct)])
    orders = db.scalar(select(func.count()).select_from(Order).where(Order.created_at > day))
    gauge("duka_orders_created_24h", "Orders confirmed by customers in the last 24 hours.", [({}, orders)])
    rows = db.execute(select(Notification.status, func.count()).where(Notification.created_at > day)
                      .group_by(Notification.status)).all()
    gauge("duka_owner_alerts_24h", "Owner alerts in the last 24 hours by status.", [({"status": s}, n) for s, n in rows])
    return "\n".join(lines) + "\n"


def purge_processed_events(db: Session, older_than_days: int) -> int:
    """webhook_events.payload holds message text (PII); the message itself lives in `messages`. Keep finished
    events only as long as they are useful for debugging. Unfinished events are never purged."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    result = db.execute(WebhookEvent.__table__.delete().where(WebhookEvent.status.in_(("done", "dead")),
                                                              WebhookEvent.updated_at < cutoff))
    return result.rowcount or 0
