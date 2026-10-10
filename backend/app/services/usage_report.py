"""Monthly usage reports, read from the usage ledger (docs/P3_MONTHLY_USAGE.md): what a shop used in one calendar
month (AI model calls, paid embeddings requests, inbound WhatsApp messages and send attempts) and the estimated
costs recorded on those events.

Read-only by construction: a report runs in one REPEATABLE READ, READ ONLY transaction on a connection of its own, so
every figure comes from the same snapshot and PostgreSQL refuses any write. It reads `usage_events` only, never
agent_runs or messages, through the tenant's repository; it calls no provider and sends nothing.

Months: a shop's report covers the calendar month of the shop's time zone (`businesses.timezone`), from the first
instant of local day 1 to the first instant of local day 1 of the next month (end excluded). The cross-shop
operator report uses UTC months.

What each row counts:
- llm_call: one real model call; its provider retries are its `attempts` (HTTP attempts), not rows. A turn processed
  again makes new calls, which are new rows because they were made, and billed.
- embedding: one request to a paid embeddings provider; `units` = the texts in it; `attempts` = its HTTP attempts.
- wa_in: one inbound customer message, stored once however often WhatsApp delivered it.
- wa_out / wa_alert: one send attempt (success | failed | unknown); there `attempts` is the attempt's number and is
  never added up; a message is counted once (distinct source). A late failure is its own row (`late_failed`, units
  0): a correction, never a send.
Real and simulated WhatsApp traffic are never added together; an attempt whose realness is unknown is its own group.
Evaluation runs and the free hash embedder never write to the ledger, so they are not in any report.

Costs: summed per currency over priced events only; unpriced events are counted and never treated as 0; a total that
leaves some events out says so (`partially_priced`). Inbound messages are never priced (usage_service records none)."""
from __future__ import annotations

import re
import uuid
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from decimal import Decimal
from typing import Any, Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from app.core.errors import ValidationError
from app.models import Business, UsageEvent
from app.repositories.repos import UsageEventRepo

KINDS = ("llm_call", "embedding", "wa_in", "wa_out", "wa_alert")  # report order
AI_KINDS = frozenset({"llm_call", "embedding"})
SEND_KINDS = frozenset({"wa_out", "wa_alert"})
ATTEMPT_STATUSES = ("success", "failed", "unknown")  # the wa_out / wa_alert rows that are send attempts
DIMENSIONS = ("kind", "provider", "model", "configured_model", "source_type", "status", "is_real", "message_kind",
              "template_name", "market")
FIRST_YEAR, LAST_YEAR = 2000, 2100
_MONTH = re.compile(r"(\d{4})-(0[1-9]|1[0-2])")


@dataclass(frozen=True)
class Month:
    year: int
    month: int

    @classmethod
    def parse(cls, value: str | None) -> Month:
        m = _MONTH.fullmatch(value or "")
        if m is None:
            raise ValidationError("month must be YYYY-MM, for example 2026-10")
        month = cls(int(m.group(1)), int(m.group(2)))
        if not FIRST_YEAR <= month.year <= LAST_YEAR:
            raise ValidationError(f"month must be between {FIRST_YEAR}-01 and {LAST_YEAR}-12")
        return month

    @classmethod
    def current(cls, tz: tzinfo, now: datetime | None = None) -> Month:
        local = (now or datetime.now(timezone.utc)).astimezone(tz)
        return cls(local.year, local.month)

    def following(self) -> Month:
        return Month(self.year + self.month // 12, self.month % 12 + 1)

    def start(self, tz: tzinfo) -> datetime:
        """The first instant of local day 1 in `tz`, in UTC. A local midnight that a daylight-saving change skips is
        read with the offset in force before the change (zoneinfo's fold=0): for a change at midnight, that is the
        change itself, the day's first instant (America/Asuncion, 2023-10-01: 00:00 became 01:00)."""
        return datetime(self.year, self.month, 1, tzinfo=tz).astimezone(timezone.utc)

    def __str__(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"


@dataclass(frozen=True)
class Period:
    month: Month
    zone: tzinfo
    zone_name: str
    zone_note: str | None = None  # set when the shop's configured zone could not be used

    @property
    def start(self) -> datetime:
        return self.month.start(self.zone)

    @property
    def end(self) -> datetime:  # excluded
        return self.month.following().start(self.zone)

    def header(self) -> dict[str, Any]:
        return {"month": str(self.month), "timezone": self.zone_name, "timezone_note": self.zone_note,
                "start": self.start.isoformat(), "end": self.end.isoformat()}


def shop_zone(name: str | None) -> tuple[tzinfo, str, str | None]:
    """The shop's configured time zone; UTC (which needs no time zone data) with a note when it cannot be used."""
    try:
        return ZoneInfo(name), name, None
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return timezone.utc, "UTC", f"the shop's time zone {name!r} is not available, so UTC is used"


def shop_period(zone_name: str | None, month: str | None, *, now: datetime | None = None) -> Period:
    zone, name, note = shop_zone(zone_name)
    return Period(Month.parse(month) if month else Month.current(zone, now), zone, name, note)


def utc_period(month: str | None, *, now: datetime | None = None) -> Period:
    return Period(Month.parse(month) if month else Month.current(timezone.utc, now), timezone.utc, "UTC")


@contextmanager
def snapshot(bind: Engine | Connection) -> Iterator[Session]:
    """One REPEATABLE READ, READ ONLY transaction on a connection of its own (never the caller's transaction): the
    whole report reads one snapshot of the ledger, and PostgreSQL refuses any write made through it."""
    with bind.engine.connect() as conn:
        conn.execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True)
        with Session(bind=conn) as db:
            try:
                yield db
            finally:
                db.rollback()


# ---------------------------------------------------------------- queries (tenant-scoped through the repository)
def _columns() -> list[Any]:
    return [getattr(UsageEvent, d) for d in DIMENSIONS]


def _in_period(start: datetime, end: datetime) -> list[Any]:
    return [UsageEvent.occurred_at >= start, UsageEvent.occurred_at < end]


def lines_query(repo: UsageEventRepo, start: datetime, end: datetime) -> Select:
    e = UsageEvent
    unreported = and_(e.status == "success", or_(e.input_tokens.is_(None),
                                                 and_(e.kind == "llm_call", e.output_tokens.is_(None))))
    return (repo.select(*_columns(),
                        func.count().label("events"),
                        func.coalesce(func.sum(e.units), 0).label("units"),
                        func.coalesce(func.sum(e.attempts), 0).label("attempts"),
                        func.coalesce(func.sum(e.input_tokens), 0).label("input_tokens"),
                        func.coalesce(func.sum(e.output_tokens), 0).label("output_tokens"),
                        func.coalesce(func.sum(e.tool_calls), 0).label("tool_calls"),
                        func.count().filter(unreported).label("unreported_usage"),
                        func.count(e.source_id.distinct()).label("sources"),
                        func.count(e.cost_micros).label("priced"))
            .where(*_in_period(start, end)).group_by(*_columns()))


def amounts_query(repo: UsageEventRepo, start: datetime, end: datetime) -> Select:
    e = UsageEvent
    return (repo.select(*_columns(), e.currency, func.sum(e.cost_micros).label("micros"),
                        func.array_agg(e.price_version.distinct()).label("versions"))
            .where(*_in_period(start, end), e.cost_micros.is_not(None)).group_by(*_columns(), e.currency))


def messages_query(repo: UsageEventRepo, start: datetime, end: datetime) -> Select:
    """Messages counted once, per send kind and traffic: attempted, accepted by WhatsApp, failed later."""
    e = UsageEvent
    distinct = func.count(e.source_id.distinct())
    return (repo.select(e.kind, e.is_real,
                        distinct.filter(e.status.in_(ATTEMPT_STATUSES)).label("attempted"),
                        distinct.filter(e.status == "success").label("accepted"),
                        distinct.filter(e.status == "late_failed").label("late_failed"))
            .where(*_in_period(start, end), e.kind.in_(SEND_KINDS)).group_by(e.kind, e.is_real))


# ---------------------------------------------------------------- shaping
def _traffic(kind: str, is_real: bool | None) -> str | None:
    if kind in AI_KINDS:
        return None  # always real requests to a provider, whatever the conversation came from
    return "unknown" if is_real is None else "real" if is_real else "simulated"


def _pricing(priced: int, unpriced: int) -> str:
    if priced == 0:
        return "unpriced" if unpriced else "no_usage"
    return "partially_priced" if unpriced else "priced"


def _amount(micros: int) -> str:
    return f"{Decimal(micros).scaleb(-6):f}"


class _Cost:
    """Costs of a set of events: amounts per currency over priced events, and the events left unpriced."""

    def __init__(self) -> None:
        self.priced = 0
        self.unpriced = 0
        self.micros: dict[str, int] = defaultdict(int)
        self.versions: dict[str, set[str]] = defaultdict(set)
        self.unpriced_by_kind: dict[str, int] = defaultdict(int)

    @classmethod
    def of(cls, out: dict[str, Any]) -> _Cost:
        """Back from what out() wrote, to add it to another total."""
        cost = cls()
        cost.priced, cost.unpriced = out["priced_events"], out["unpriced_events"]
        for a in out["amounts"]:
            cost.micros[a["currency"]] = a["micros"]
            cost.versions[a["currency"]] = set(a["price_versions"])
        cost.unpriced_by_kind.update(out.get("unpriced_events_by_kind", {}))
        return cost

    def add(self, other: _Cost) -> None:
        self.priced += other.priced
        self.unpriced += other.unpriced
        for currency, micros in other.micros.items():
            self.micros[currency] += micros
            self.versions[currency] |= other.versions[currency]
        for kind, n in other.unpriced_by_kind.items():
            self.unpriced_by_kind[kind] += n

    def out(self, *, by_kind: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "pricing": _pricing(self.priced, self.unpriced), "priced_events": self.priced,
            "unpriced_events": self.unpriced,
            "amounts": [{"currency": c, "micros": m, "amount": _amount(m), "price_versions": sorted(self.versions[c])}
                        for c, m in sorted(self.micros.items())]}
        if by_kind:
            out["unpriced_events_by_kind"] = {k: self.unpriced_by_kind[k] for k in KINDS if self.unpriced_by_kind[k]}
        return out


def _line(row: Any, cost: _Cost) -> dict[str, Any]:
    kind = row.kind
    ai, send = kind in AI_KINDS, kind in SEND_KINDS
    return {
        "kind": kind, "traffic": _traffic(kind, row.is_real), "provider": row.provider, "model": row.model,
        "configured_model": row.configured_model, "source_type": row.source_type, "status": row.status,
        "message_kind": row.message_kind, "template_name": row.template_name, "market": row.market,
        "events": row.events, "units": int(row.units),
        # Measures that do not apply to a kind are None, never 0.
        "http_attempts": int(row.attempts) if ai else None,
        "input_tokens": int(row.input_tokens) if ai else None,
        "output_tokens": int(row.output_tokens) if kind == "llm_call" else None,
        "tool_calls": int(row.tool_calls) if kind == "llm_call" else None,
        "unreported_usage": row.unreported_usage if ai else None,
        "messages": row.sources if send else None,
        "cost": cost.out(),
    }


def _sort_key(line: dict[str, Any]) -> tuple:
    return (KINDS.index(line["kind"]),) + tuple(
        (line[d] is not None, str(line[d])) for d in ("traffic", "provider", "model", "configured_model",
                                                     "source_type", "message_kind", "template_name", "market",
                                                     "status"))


def _kind_summaries(lines: list[dict[str, Any]], messages: dict[tuple[str, str | None], Any]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str | None], list[dict[str, Any]]] = defaultdict(list)
    for line in lines:
        groups[(line["kind"], line["traffic"])].append(line)
    out = []
    for (kind, traffic), group in sorted(groups.items(), key=lambda g: (KINDS.index(g[0][0]), str(g[0][1]))):
        cost = _Cost()
        for line in group:
            cost.add(_Cost.of(line["cost"]))
        by_status: dict[str, int] = defaultdict(int)
        for line in group:
            by_status[line["status"]] += line["events"]
        summary: dict[str, Any] = {"kind": kind, "traffic": traffic, "events": sum(x["events"] for x in group),
                                   "by_status": dict(sorted(by_status.items()))}
        if kind in AI_KINDS:
            summary.update(units=sum(x["units"] for x in group), http_attempts=sum(x["http_attempts"] for x in group),
                           input_tokens=sum(x["input_tokens"] for x in group),
                           unreported_usage=sum(x["unreported_usage"] for x in group))
            if kind == "llm_call":
                summary.update(output_tokens=sum(x["output_tokens"] for x in group),
                               tool_calls=sum(x["tool_calls"] for x in group))
        elif kind == "wa_in":
            summary["messages"] = summary["events"]
        elif kind in SEND_KINDS:
            m = messages.get((kind, traffic))
            summary.update(send_attempts=sum(x["events"] for x in group if x["status"] in ATTEMPT_STATUSES),
                           messages_attempted=m.attempted if m else 0, messages_accepted=m.accepted if m else 0,
                           messages_late_failed=m.late_failed if m else 0)
        summary["cost"] = cost.out()
        out.append(summary)
    return out


def tenant_usage(db: Session, business_id: uuid.UUID, start: datetime, end: datetime) -> dict[str, Any]:
    """One tenant's usage in [start, end), read in `db` (a snapshot() session)."""
    repo = UsageEventRepo(db, business_id)
    costs: dict[tuple, _Cost] = defaultdict(_Cost)
    rows = db.execute(lines_query(repo, start, end)).all()
    for row in rows:
        key = tuple(getattr(row, d) for d in DIMENSIONS)
        costs[key].priced = row.priced
        costs[key].unpriced = row.events - row.priced
        costs[key].unpriced_by_kind[row.kind] = row.events - row.priced
    for row in db.execute(amounts_query(repo, start, end)).all():
        key = tuple(getattr(row, d) for d in DIMENSIONS)
        costs[key].micros[row.currency] += int(row.micros)
        costs[key].versions[row.currency] |= set(row.versions)
    messages = {(r.kind, _traffic(r.kind, r.is_real)): r for r in db.execute(messages_query(repo, start, end)).all()}
    lines = sorted((_line(row, costs[tuple(getattr(row, d) for d in DIMENSIONS)]) for row in rows), key=_sort_key)
    total = _Cost()
    for c in costs.values():
        total.add(c)
    return {"kinds": _kind_summaries(lines, messages), "lines": lines, "cost": total.out(by_kind=True)}


# ---------------------------------------------------------------- reports
def tenant_month(bind: Engine | Connection, business_id: uuid.UUID, zone_name: str | None, month: str | None = None,
                 *, now: datetime | None = None) -> dict[str, Any]:
    """A shop's report for a calendar month of its own time zone `zone_name` (this month when `month` is None).
    `business_id` comes from server-side state (the signed-in user's shop, or the operator's command line)."""
    period = shop_period(zone_name, month, now=now)
    with snapshot(bind) as db:
        usage = tenant_usage(db, business_id, period.start, period.end)
    return {**period.header(), "business_id": str(business_id), **usage}


def operator_month(bind: Engine | Connection, month: str | None = None, *, now: datetime | None = None
                   ) -> dict[str, Any]:
    """Every shop's usage in one UTC calendar month, with platform totals, from one snapshot. Shops without usage in
    the month are counted, not listed."""
    period = utc_period(month, now=now)
    shops, total, kinds_total = [], _Cost(), []
    with snapshot(bind) as db:
        businesses = db.execute(select(Business.id, Business.name).order_by(Business.name, Business.id)).all()
        for business_id, name in businesses:
            usage = tenant_usage(db, business_id, period.start, period.end)
            if usage["lines"]:
                shops.append({"business_id": str(business_id), "name": name, **usage})
    for shop in shops:
        total.add(_Cost.of(shop["cost"]))
        kinds_total.extend(shop["kinds"])
    return {**period.header(), "shops_with_usage": len(shops), "shops_without_usage": len(businesses) - len(shops),
            "shops": shops, "kinds": _sum_kinds(kinds_total), "cost": total.out(by_kind=True)}


def _sum_kinds(summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Platform totals per kind and traffic: every count is additive across shops (their messages are distinct)."""
    additive = ("events", "messages", "units", "http_attempts", "input_tokens", "output_tokens", "tool_calls",
                "unreported_usage", "send_attempts", "messages_attempted", "messages_accepted", "messages_late_failed")
    merged: dict[tuple[str, str | None], dict[str, Any]] = {}
    for s in summaries:
        key = (s["kind"], s["traffic"])
        if key not in merged:
            merged[key] = {"kind": s["kind"], "traffic": s["traffic"], "by_status": defaultdict(int), "_cost": _Cost()}
        m = merged[key]
        for field in additive:
            if field in s:
                m[field] = m.get(field, 0) + s[field]
        for status, n in s["by_status"].items():
            m["by_status"][status] += n
        m["_cost"].add(_Cost.of(s["cost"]))
    out = []
    for key in sorted(merged, key=lambda k: (KINDS.index(k[0]), str(k[1]))):
        m = merged[key]
        m["by_status"] = dict(sorted(m["by_status"].items()))
        m["cost"] = m.pop("_cost").out()
        out.append(m)
    return out


# ---------------------------------------------------------------- text (the operator command)
_TITLES = {"llm_call": "AI model calls", "embedding": "Embeddings requests", "wa_in": "WhatsApp messages in",
           "wa_out": "WhatsApp messages to customers", "wa_alert": "WhatsApp alerts to the owner"}


def _money(cost: dict[str, Any]) -> str:
    return ", ".join(f"{a['currency']} {a['amount']}" for a in cost["amounts"])


def _cost_text(cost: dict[str, Any]) -> str:
    if cost["pricing"] == "priced":
        return f"cost {_money(cost)} ({cost['priced_events']} priced)"
    if cost["pricing"] == "unpriced":
        return f"cost not known: none of its {cost['unpriced_events']} event(s) has a price"
    return (f"cost {_money(cost)} for {cost['priced_events']} priced event(s); {cost['unpriced_events']} event(s) have "
            "no price and are not included")


def _kind_text(k: dict[str, Any]) -> list[str]:
    title = _TITLES[k["kind"]] + (f", {k['traffic']}" if k["traffic"] else "")
    statuses = ", ".join(f"{s} {n}" for s, n in k["by_status"].items()
                         if k["kind"] not in SEND_KINDS or s in ATTEMPT_STATUSES)
    if k["kind"] == "llm_call":
        facts = (f"{k['events']} call(s) ({statuses}); HTTP attempts {k['http_attempts']}; tokens in "
                 f"{k['input_tokens']}, out {k['output_tokens']}; tool calls {k['tool_calls']}")
    elif k["kind"] == "embedding":
        facts = (f"{k['events']} request(s) ({statuses}); texts {k['units']}; HTTP attempts {k['http_attempts']}; "
                 f"input tokens {k['input_tokens']}")
    elif k["kind"] == "wa_in":
        facts = f"{k['messages']} message(s)"
    else:
        facts = (f"{k['send_attempts']} send attempt(s) ({statuses}); {k['messages_attempted']} message(s), "
                 f"{k['messages_accepted']} accepted by WhatsApp, {k['messages_late_failed']} failed later")
    out = [f"  {title}: {facts}"]
    if k.get("unreported_usage"):
        out.append(f"    {k['unreported_usage']} successful event(s) without reported tokens")
    out.append(f"    {_cost_text(k['cost'])}")
    return out


def _total_text(cost: dict[str, Any]) -> list[str]:
    if cost["pricing"] == "no_usage":
        return ["  No usage recorded in this month."]
    out = [f"  Priced cost: {_money(cost) or 'none'} from {cost['priced_events']} priced event(s)."]
    if cost["unpriced_events"]:
        kinds = ", ".join(f"{k} {n}" for k, n in cost["unpriced_events_by_kind"].items())
        out.append(f"  Not priced: {cost['unpriced_events']} event(s) ({kinds}). The cost above leaves them out: "
                   f"pricing is {cost['pricing'].replace('_', ' ')}.")
    return out


def format_text(report: dict[str, Any]) -> str:
    """The operator command's plain-text view of a tenant_month() or operator_month() report."""
    who = f"{report.get('name') or 'shop'} ({report['business_id']})" if "business_id" in report else "all shops"
    out = [f"Usage report {report['month']}: {who}, time zone {report['timezone']}",
           f"From {report['start']} to {report['end']} (end excluded)"]
    if report["timezone_note"]:
        out.append(f"Note: {report['timezone_note']}")
    if "shops" in report:
        out.append(f"Shops with usage: {report['shops_with_usage']}; without usage: {report['shops_without_usage']}")
        for shop in report["shops"]:
            out += ["", f"{shop['name']} ({shop['business_id']})"]
            for k in shop["kinds"]:
                out += _kind_text(k)
            out += _total_text(shop["cost"])
        out += ["", "All shops"]
    else:
        out.append("")
    for k in report["kinds"]:
        out += _kind_text(k)
    out += _total_text(report["cost"])
    return "\n".join(out)
