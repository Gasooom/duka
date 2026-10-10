"""Runaway Conversation Guard (docs/P1_RUNAWAY_GUARD.md): bounds the model calls and provider HTTP attempts one inbound
message, one customer and one tenant can cause, in every process and instance.

Every real (metered) model call and every provider HTTP attempt is RESERVED in ai_usage_counters before it is made:
- message: the webhook event, across its processing retries (period `lifetime`);
- customer and tenant: fixed UTC buckets, `hour` and `day` (date_trunc in UTC, PostgreSQL's clock). Fixed buckets,
  not rolling windows: one row per bucket makes a reservation one atomic upsert; the cost is that a burst straddling
  a boundary can use up to twice an hourly limit within 60 minutes (the daily bucket still bounds it).

A reservation is one short transaction on its own connection, committed before the call: it survives a rollback of
the turn (the spend was real), holds row locks only for its own statements, and touches the rows in a fixed order
(message, customer hour/day, tenant hour/day), so concurrent reservations never deadlock. A crash between the
reservation and the call over-counts by one: the safe direction. The insert-only usage_events ledger is not used for
any of this: it is written after the call, best effort, and stays the history.

Modes per scope (settings.ai_guard_*_mode): off (not counted), observe (counted; a reservation past a limit is logged
as `ai_guard.decision` and counted in `over_limit`, never refused)."""
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import DateTime, bindparam, func, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger, log_event, safe_error
from app.models import AiUsageCounter

logger = get_logger(__name__)
LIFETIME_START = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Bucket:
    scope: str           # message | customer | tenant
    subject_id: uuid.UUID
    period: str          # lifetime | hour | day
    mode: str            # observe (off scopes are never listed)
    calls_limit: int     # 0 = no limit
    attempts_limit: int  # 0 = no limit


def _limits(scope: str, period: str) -> tuple[int, int]:
    if scope == "message":
        return settings.ai_guard_message_limits
    return (getattr(settings, f"ai_guard_{scope}_calls_per_{period}"),
            getattr(settings, f"ai_guard_{scope}_attempts_per_{period}"))


class AIGuard:
    """The guard for one agent turn of one tenant. `business_id` comes from server-side state (the tenant the webhook
    account belongs to), never from input; `event_id` is the durable webhook event (None outside the inbox, e.g. a
    direct engine call in a test: the message scope is then skipped)."""

    def __init__(self, bind: Engine | Connection, business_id: uuid.UUID, *, customer_id: uuid.UUID | None = None,
                 event_id: uuid.UUID | None = None):
        self.engine = bind.engine  # a new connection for every reservation, never the turn's transaction
        self.business_id = business_id
        self.customer_id = customer_id
        self.event_id = event_id

    def buckets(self) -> list[Bucket]:
        """The rows a reservation touches, in the fixed lock order."""
        subjects = (("message", self.event_id, ("lifetime",)), ("customer", self.customer_id, ("hour", "day")),
                    ("tenant", self.business_id, ("hour", "day")))
        out = []
        for scope, subject, periods in subjects:
            mode = getattr(settings, f"ai_guard_{scope}_mode")
            if mode == "off" or subject is None:
                continue
            out += [Bucket(scope, subject, period, mode, *_limits(scope, period)) for period in periods]
        return out

    def reserve_call(self, *, at: datetime | None = None) -> None:
        """Before a model call: one call and its first HTTP attempt."""
        self._reserve(calls=1, at=at)

    def reserve_attempt(self, attempt: int) -> bool:
        """Before HTTP attempt `attempt` of a model call (the first was reserved with the call). True = may send."""
        if attempt > 1:
            self._reserve(calls=0, at=None)
        return True

    def _reserve(self, *, calls: int, at: datetime | None) -> None:
        buckets = self.buckets()
        if not buckets:
            return
        over: list[tuple[Bucket, int, int]] = []
        try:
            with Session(bind=self.engine) as db:
                for b in buckets:
                    row_id, calls_now, attempts_now = db.execute(_upsert(self.business_id, b, calls, at)).one()
                    if (calls and b.calls_limit and calls_now > b.calls_limit) or \
                            (b.attempts_limit and attempts_now > b.attempts_limit):
                        db.execute(update(AiUsageCounter).where(AiUsageCounter.id == row_id)
                                   .values(over_limit=AiUsageCounter.over_limit + 1))
                        over.append((b, calls_now, attempts_now))
                db.commit()
        except Exception as exc:  # observe never blocks: the call goes ahead, the gap is in the log
            log_event(logger, "ai_guard.store_error", 40, operation="ai_guard", status="error", error=safe_error(exc),
                      business_id=str(self.business_id), calls=calls)
            return
        for b, calls_now, attempts_now in over:
            log_event(logger, "ai_guard.decision", 30, operation="ai_guard", status="over_limit", mode=b.mode,
                      decision="would_block", scope=b.scope, period=b.period, business_id=str(self.business_id),
                      calls=calls_now, attempts=attempts_now, calls_limit=b.calls_limit or None,
                      attempts_limit=b.attempts_limit or None)


def period_start(period: str, at: datetime | None):
    """The bucket a reservation falls in: fixed UTC buckets on PostgreSQL's clock (`at` only in tests)."""
    if period == "lifetime":
        return bindparam("lifetime_start", LIFETIME_START, type_=DateTime(timezone=True))
    now = bindparam("at", at, type_=DateTime(timezone=True)) if at is not None else func.now()
    return func.date_trunc(period, now, "UTC")


def _upsert(business_id: uuid.UUID, b: Bucket, calls: int, at: datetime | None):
    c = AiUsageCounter.__table__.c
    stmt = pg_insert(AiUsageCounter).values(
        id=uuid.uuid4(), business_id=business_id, scope=b.scope, subject_id=b.subject_id, period=b.period,
        period_start=period_start(b.period, at), calls=calls, attempts=1, over_limit=0, denied=0, updated_at=func.now())
    return stmt.on_conflict_do_update(
        constraint="uq_ai_usage_counters_key",
        set_={"calls": c.calls + calls, "attempts": c.attempts + 1, "updated_at": func.now()},
    ).returning(c.id, c.calls, c.attempts)
