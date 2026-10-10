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

Modes per scope (settings.ai_guard_*_mode):
- off: not counted;
- observe: counted; a reservation past a limit is logged (`ai_guard.decision`, would_block) and counted in
  `over_limit`, never refused;
- enforce: the upsert only increments while the row is under its limits; a refusal rolls the whole reservation back
  (nothing is counted for a call that is not made), is counted in `denied`, and raises AIGuardDenied. If the counter
  store cannot be used, a reservation that involves an enforcing scope is refused too (fail closed, decision D1)."""
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import DateTime, and_, bindparam, func, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from app.agents.providers.base import LLMError
from app.core.config import settings
from app.core.logging import get_logger, log_event, safe_error
from app.models import AiUsageCounter

logger = get_logger(__name__)
LIFETIME_START = datetime(1970, 1, 1, tzinfo=timezone.utc)
STORE_UNAVAILABLE = "store_unavailable"


class AIGuardDenied(LLMError):
    """The guard refused a model call or a provider attempt (enforce mode). `reason`: the limit that was reached
    ("calls" or "attempts") or STORE_UNAVAILABLE (the counters could not be trusted: fail closed)."""

    def __init__(self, scope: str, period: str, reason: str):
        super().__init__(f"AI guard refused: {scope} {period} {reason}", attempts=0)
        self.scope, self.period, self.reason = scope, period, reason


@dataclass(frozen=True)
class Bucket:
    scope: str           # message | customer | tenant
    subject_id: uuid.UUID
    period: str          # lifetime | hour | day
    mode: str            # observe | enforce (off scopes are never listed)
    calls_limit: int     # 0 = no limit
    attempts_limit: int  # 0 = no limit

    @property
    def enforcing(self) -> bool:
        return self.mode == "enforce" and bool(self.calls_limit or self.attempts_limit)


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
        self._stopped: AIGuardDenied | None = None  # a provider retry the guard refused, for the engine to report

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
        """Before a model call: one call and its first HTTP attempt. Raises AIGuardDenied when refused."""
        denied = self._reserve(calls=1, at=at)
        if denied is not None:
            raise denied

    def reserve_attempt(self, attempt: int) -> bool:
        """Before HTTP attempt `attempt` of a model call (the first was reserved with the call). False = the guard
        refused it: the provider must stop retrying, and take_denial() tells the engine why."""
        if attempt <= 1:
            return True
        denied = self._reserve(calls=0, at=None)
        if denied is not None:
            self._stopped = denied
            return False
        return True

    def take_denial(self) -> AIGuardDenied | None:
        stopped, self._stopped = self._stopped, None
        return stopped

    def claim_alert(self, period: str, *, at: datetime | None = None) -> bool:
        """True for exactly one caller per tenant and UTC `period` bucket (hour or day): the one that sends the owner
        alert (decision D4). One atomic statement on the tenant's counter row, in its own transaction; if the turn
        that claimed it later rolls back, that bucket's alert is lost rather than sent twice."""
        try:
            with Session(bind=self.engine) as db:
                won = db.execute(_alert_upsert(self.business_id, period, at)).first() is not None
                db.commit()
            return won
        except Exception as exc:
            log_event(logger, "ai_guard.store_error", 40, operation="ai_guard", status="error", error=safe_error(exc),
                      business_id=str(self.business_id), step="claim_alert")
            return False

    def _reserve(self, *, calls: int, at: datetime | None) -> AIGuardDenied | None:
        buckets = self.buckets()
        if not buckets:
            return None
        over: list[tuple[Bucket, int, int]] = []
        refused: Bucket | None = None
        try:
            with Session(bind=self.engine) as db:
                for b in buckets:
                    row = db.execute(_upsert(self.business_id, b, calls, at)).first()
                    if row is None:  # an enforcing row is at its limit: nothing of this reservation is kept
                        refused = b
                        break
                    row_id, calls_now, attempts_now = row
                    if b.mode == "observe" and _past(b, calls, calls_now, attempts_now):
                        db.execute(update(AiUsageCounter).where(AiUsageCounter.id == row_id)
                                   .values(over_limit=AiUsageCounter.over_limit + 1))
                        over.append((b, calls_now, attempts_now))
                if refused is None:
                    db.commit()
                else:
                    db.rollback()
                    reason = _record_refusal(db, self.business_id, refused, calls, at)
                    db.commit()
        except Exception as exc:
            enforcing = [b for b in buckets if b.enforcing]
            log_event(logger, "ai_guard.store_error", 40, operation="ai_guard", status="error", error=safe_error(exc),
                      business_id=str(self.business_id), calls=calls, fail_closed=bool(enforcing))
            if enforcing:  # the limits cannot be trusted: refuse rather than spend without a ceiling (D1)
                return AIGuardDenied(enforcing[0].scope, enforcing[0].period, STORE_UNAVAILABLE)
            return None
        for b, calls_now, attempts_now in over:
            log_event(logger, "ai_guard.decision", 30, operation="ai_guard", status="over_limit", mode=b.mode,
                      decision="would_block", scope=b.scope, period=b.period, business_id=str(self.business_id),
                      calls=calls_now, attempts=attempts_now, calls_limit=b.calls_limit or None,
                      attempts_limit=b.attempts_limit or None)
        if refused is None:
            return None
        log_event(logger, "ai_guard.decision", 30, operation="ai_guard", status="refused", mode=refused.mode,
                  decision="refused", scope=refused.scope, period=refused.period, reason=reason,
                  business_id=str(self.business_id), calls_limit=refused.calls_limit or None,
                  attempts_limit=refused.attempts_limit or None)
        return AIGuardDenied(refused.scope, refused.period, reason)


def _past(b: Bucket, calls: int, calls_now: int, attempts_now: int) -> bool:
    return bool((calls and b.calls_limit and calls_now > b.calls_limit) or
                (b.attempts_limit and attempts_now > b.attempts_limit))


def period_start(period: str, at: datetime | None):
    """The bucket a reservation falls in: fixed UTC buckets on PostgreSQL's clock (`at` only in tests)."""
    if period == "lifetime":
        return bindparam("lifetime_start", LIFETIME_START, type_=DateTime(timezone=True))
    now = bindparam("at", at, type_=DateTime(timezone=True)) if at is not None else func.now()
    return func.date_trunc(period, now, "UTC")


def _key(business_id: uuid.UUID, b: Bucket, at: datetime | None):
    c = AiUsageCounter.__table__.c
    return and_(c.business_id == business_id, c.scope == b.scope, c.subject_id == b.subject_id,
                c.period == b.period, c.period_start == period_start(b.period, at))


def _upsert(business_id: uuid.UUID, b: Bucket, calls: int, at: datetime | None):
    """Add `calls` calls and one attempt to the bucket's row. In enforce mode the existing row only changes while it
    is under its limits; no row returned = refused. (A new row starts at 1 attempt and at most 1 call: always within
    a limit, which is at least 1.)"""
    c = AiUsageCounter.__table__.c
    stmt = pg_insert(AiUsageCounter).values(
        id=uuid.uuid4(), business_id=business_id, scope=b.scope, subject_id=b.subject_id, period=b.period,
        period_start=period_start(b.period, at), calls=calls, attempts=1, over_limit=0, denied=0, updated_at=func.now())
    within = None
    if b.mode == "enforce":
        conditions = []
        if calls and b.calls_limit:
            conditions.append(c.calls < b.calls_limit)
        if b.attempts_limit:
            conditions.append(c.attempts < b.attempts_limit)
        within = and_(*conditions) if conditions else None
    return stmt.on_conflict_do_update(
        constraint="uq_ai_usage_counters_key",
        set_={"calls": c.calls + calls, "attempts": c.attempts + 1, "updated_at": func.now()},
        where=within,
    ).returning(c.id, c.calls, c.attempts)


def _record_refusal(db: Session, business_id: uuid.UUID, b: Bucket, calls: int, at: datetime | None) -> str:
    """Count the refusal on the refusing row and say which limit was reached ("calls" or "attempts")."""
    c = AiUsageCounter.__table__.c
    row = db.execute(update(AiUsageCounter.__table__).where(_key(business_id, b, at))
                     .values(denied=c.denied + 1).returning(c.calls, c.attempts)).first()
    if row is not None and calls and b.calls_limit and row.calls >= b.calls_limit:
        return "calls"
    return "attempts"


def _alert_upsert(business_id: uuid.UUID, period: str, at: datetime | None):
    c = AiUsageCounter.__table__.c
    stmt = pg_insert(AiUsageCounter).values(
        id=uuid.uuid4(), business_id=business_id, scope="tenant", subject_id=business_id, period=period,
        period_start=period_start(period, at), calls=0, attempts=0, over_limit=0, denied=0, alerted_at=func.now(),
        updated_at=func.now())
    return stmt.on_conflict_do_update(
        constraint="uq_ai_usage_counters_key", set_={"alerted_at": func.now()}, where=c.alerted_at.is_(None),
    ).returning(c.id)
