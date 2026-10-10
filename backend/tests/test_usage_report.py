"""Monthly usage reports (C2, docs/P3_MONTHLY_USAGE.md): read from the usage ledger only, for one shop in the calendar
month of its own time zone (API and operator command) or for every shop in UTC months (operator command).

Rows are written straight into usage_events at chosen instants, as the metering code writes them. Covered: month,
year and daylight-saving boundaries; empty months; unpriced, partially priced and multi-currency costs (an unknown
price is never 0); retries counted as attempts and messages counted once; real and simulated traffic apart; every
figure checked against an independent row-by-row fold of the ledger; tenant isolation; and that a report only reads:
one READ ONLY snapshot, no provider call, nothing sent, the ledger unchanged."""
import hashlib
import json
import random
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import InternalError

from app.agents.providers import LLMProvider, set_provider_override
from app.cli import main
from app.db.session import SessionLocal, engine
from app.integrations.whatsapp.adapters import WhatsAppAdapter, set_adapter_override
from app.models import Business
from app.repositories.repos import UsageEventRepo
from app.services import embeddings, usage_report
from app.services.business_service import register_business
from app.services.usage_report import AI_KINDS, ATTEMPT_STATUSES, KINDS, SEND_KINDS

UTC = timezone.utc
TRAFFIC = {True: "real", False: "simulated", None: "unknown"}
DEFAULTS = {
    "llm_call": dict(status="success", provider="openai_compat", model="m-served", configured_model="m",
                     source_type="agent_run", units=1, attempts=1, tool_calls=0, input_tokens=10, output_tokens=2),
    "embedding": dict(status="success", provider="openai_compat", model="e-served", configured_model="e",
                      source_type="product_search", units=1, attempts=1, tool_calls=0, input_tokens=4),
    "wa_in": dict(status="received", provider="whatsapp", source_type="message", units=1, attempts=0, tool_calls=0,
                  is_real=True, market="250"),
    "wa_out": dict(status="success", provider="whatsapp", source_type="message", units=1, attempts=1, tool_calls=0,
                   is_real=True, message_kind="free_form", market="250"),
    "wa_alert": dict(status="success", provider="whatsapp", source_type="notification", units=1, attempts=1,
                     tool_calls=0, is_real=True, message_kind="free_form", market="250"),
}


def at(*parts) -> datetime:
    return datetime(*parts, tzinfo=UTC)


def event(when: datetime, kind: str = "llm_call", **fields) -> dict:
    return {**DEFAULTS[kind], "source_id": uuid.uuid4(), **fields, "kind": kind, "occurred_at": when,
            "idempotency_key": f"test:{uuid.uuid4()}"}


def write(business_id: uuid.UUID, *rows: dict) -> list[dict]:
    with SessionLocal() as s:
        for row in rows:
            assert UsageEventRepo(s, business_id).record(**row)
        s.commit()
    return list(rows)


@pytest.fixture
def shop():
    """A shop in time zone `tz`, created as the operator command creates one."""
    def make(tz: str = "Africa/Kigali", name: str = "Shop") -> uuid.UUID:
        with SessionLocal() as db:
            business, _, _ = register_business(db, business_name=f"{name} {uuid.uuid4().hex[:6]}",
                                               email=f"{uuid.uuid4().hex[:10]}@test.dev", password="password123")
            business.timezone = tz
            db.commit()
            return business.id
    return make


def report(business_id: uuid.UUID, tz: str, month: str) -> dict:
    return usage_report.tenant_month(engine, business_id, tz, month)


def kind(rep: dict, name: str, traffic: str | None = None) -> dict | None:
    return next((k for k in rep["kinds"] if k["kind"] == name and k["traffic"] == traffic), None)


def calls(rep: dict) -> int:
    k = kind(rep, "llm_call")
    return k["events"] if k else 0


def shop_in(rep: dict, business_id: uuid.UUID) -> dict:
    return next(s for s in rep["shops"] if s["business_id"] == str(business_id))


# ---------------------------------------------------------------- months
def test_a_shops_month_is_its_local_calendar_month_and_the_operators_is_utc(shop):
    bid = shop("Africa/Kigali")
    write(bid, event(at(2026, 10, 31, 21, 59, 59)),  # 23:59:59 on 31 October in Kigali (UTC+2)
          event(at(2026, 10, 31, 22, 0)))  # 00:00 on 1 November in Kigali
    october, november = report(bid, "Africa/Kigali", "2026-10"), report(bid, "Africa/Kigali", "2026-11")
    assert (october["start"], october["end"]) == ("2026-09-30T22:00:00+00:00", "2026-10-31T22:00:00+00:00")
    assert november["start"] == october["end"] and october["timezone"] == "Africa/Kigali"
    assert calls(october) == calls(november) == 1
    utc = usage_report.operator_month(engine, "2026-10")
    assert (utc["timezone"], utc["start"], utc["end"]) == ("UTC", "2026-10-01T00:00:00+00:00",
                                                           "2026-11-01T00:00:00+00:00")
    assert calls(shop_in(utc, bid)) == 2  # both instants are in October in UTC


def test_the_year_boundary(shop):
    bid = shop("Africa/Kigali")
    write(bid, event(at(2026, 12, 31, 21, 59, 59)), event(at(2026, 12, 31, 22, 0)))
    december, january = report(bid, "Africa/Kigali", "2026-12"), report(bid, "Africa/Kigali", "2027-01")
    assert december["end"] == january["start"] == "2026-12-31T22:00:00+00:00"
    assert january["end"] == "2027-01-31T22:00:00+00:00"
    assert calls(december) == calls(january) == 1
    assert calls(shop_in(usage_report.operator_month(engine, "2026-12"), bid)) == 2
    assert usage_report.operator_month(engine, "2027-01")["shops"] == []


def test_months_across_daylight_saving_changes(shop):
    """Europe/Paris: summer time begins on 29 March 2026 and ends on 25 October 2026, so March is an hour short and
    October an hour long, and each month ends at local midnight with the offset of that night."""
    bid = shop("Europe/Paris")
    write(bid, event(at(2026, 3, 31, 21, 59, 59)), event(at(2026, 3, 31, 22, 0)),  # around 00:00 CEST, 1 April
          event(at(2026, 10, 31, 22, 59, 59)), event(at(2026, 10, 31, 23, 0)))  # around 00:00 CET, 1 November
    march, april, october, november = (report(bid, "Europe/Paris", m)
                                       for m in ("2026-03", "2026-04", "2026-10", "2026-11"))
    assert (march["start"], march["end"]) == ("2026-02-28T23:00:00+00:00", "2026-03-31T22:00:00+00:00")
    assert (october["start"], october["end"]) == ("2026-09-30T22:00:00+00:00", "2026-10-31T23:00:00+00:00")
    assert [calls(r) for r in (march, april, october, november)] == [1, 1, 1, 1]


def test_a_month_that_begins_with_a_daylight_saving_change_at_midnight(shop):
    """America/Asuncion, 1 October 2023: clocks went from 00:00 straight to 01:00. October begins at that change."""
    bid = shop("America/Asuncion")
    write(bid, event(at(2023, 10, 1, 3, 59, 59)),  # 23:59:59 on 30 September (UTC-4)
          event(at(2023, 10, 1, 4, 0)))  # 01:00 on 1 October (UTC-3): the day's first instant
    september, october = report(bid, "America/Asuncion", "2023-09"), report(bid, "America/Asuncion", "2023-10")
    assert september["end"] == october["start"] == "2023-10-01T04:00:00+00:00"
    assert october["end"] == "2023-11-01T03:00:00+00:00"
    assert calls(september) == calls(october) == 1


def test_an_empty_month_is_no_usage_not_zero_cost(shop):
    bid = shop()
    write(bid, event(at(2026, 9, 15)), event(at(2026, 11, 15)))
    october = report(bid, "Africa/Kigali", "2026-10")
    assert october["kinds"] == october["lines"] == []
    assert october["cost"] == {"pricing": "no_usage", "priced_events": 0, "unpriced_events": 0, "amounts": [],
                               "unpriced_events_by_kind": {}}
    every_shop = usage_report.operator_month(engine, "2026-10")
    assert every_shop["shops"] == [] and every_shop["shops_without_usage"] >= 1
    assert every_shop["cost"]["pricing"] == "no_usage"
    assert "No usage recorded in this month." in usage_report.format_text(october)


def test_month_values_are_checked(shop):
    bid = shop()
    for bad in ("2026-13", "2026-1", "26-10", "2026/10", "1999-12", "2101-01", "2026-10-01", " 2026-10"):
        with pytest.raises(usage_report.ValidationError):
            report(bid, "Africa/Kigali", bad)
    now = at(2026, 10, 31, 22, 30)  # 1 November in Kigali
    assert usage_report.tenant_month(engine, bid, "Africa/Kigali", None, now=now)["month"] == "2026-11"
    assert usage_report.operator_month(engine, None, now=now)["month"] == "2026-10"


def test_a_shop_time_zone_that_cannot_be_used_is_replaced_by_utc_and_said(shop):
    bid = shop("Mars/Olympus_Mons")
    write(bid, event(at(2026, 10, 1, 0, 30)))
    october = report(bid, "Mars/Olympus_Mons", "2026-10")
    assert (october["timezone"], october["start"]) == ("UTC", "2026-10-01T00:00:00+00:00") and calls(october) == 1
    assert "Mars/Olympus_Mons" in october["timezone_note"]
    assert "Note: the shop's time zone 'Mars/Olympus_Mons'" in usage_report.format_text(october)


# ---------------------------------------------------------------- prices
def test_unpriced_usage_is_never_counted_as_zero(shop):
    bid = shop()
    write(bid, event(at(2026, 10, 5)), event(at(2026, 10, 6), "wa_in"))
    october = report(bid, "Africa/Kigali", "2026-10")
    assert october["cost"] == {"pricing": "unpriced", "priced_events": 0, "unpriced_events": 2, "amounts": [],
                               "unpriced_events_by_kind": {"llm_call": 1, "wa_in": 1}}
    assert all(line["cost"]["amounts"] == [] for line in october["lines"])
    out = usage_report.format_text(october)
    assert "cost not known: none of its 1 event(s) has a price" in out and "Priced cost: none" in out


def test_partially_priced_usage_is_labelled_and_sums_only_priced_events(shop):
    bid = shop()
    write(bid,
          event(at(2026, 10, 2), cost_micros=1500, currency="USD", price_version="v1"),
          event(at(2026, 10, 3), cost_micros=2500, currency="USD", price_version="v2"),
          event(at(2026, 10, 4)),  # no price for it
          # A failed call is priced at 0 when a price list is loaded: 0 is a known price, unlike "unpriced".
          event(at(2026, 10, 5), status="error", input_tokens=None, output_tokens=None, cost_micros=0, currency="USD",
                price_version="v2"))
    october = report(bid, "Africa/Kigali", "2026-10")
    assert october["cost"] == {
        "pricing": "partially_priced", "priced_events": 3, "unpriced_events": 1,
        "amounts": [{"currency": "USD", "micros": 4000, "amount": "0.004000", "price_versions": ["v1", "v2"]}],
        "unpriced_events_by_kind": {"llm_call": 1}}
    by_status = {line["status"]: line["cost"] for line in october["lines"]}
    assert by_status["success"]["pricing"] == "partially_priced"
    assert by_status["error"] == {"pricing": "priced", "priced_events": 1, "unpriced_events": 0, "amounts": [
        {"currency": "USD", "micros": 0, "amount": "0.000000", "price_versions": ["v2"]}]}
    out = usage_report.format_text(october)
    assert "1 event(s) have no price and are not included" in out and "pricing is partially priced" in out


def test_amounts_in_different_currencies_are_never_added_together(shop):
    bid = shop()
    write(bid, event(at(2026, 10, 2), cost_micros=1_000_000, currency="USD", price_version="v1"),
          event(at(2026, 10, 3), "wa_out", cost_micros=50_000_000, currency="RWF", price_version="r1"))
    amounts = report(bid, "Africa/Kigali", "2026-10")["cost"]["amounts"]
    assert [(a["currency"], a["micros"], a["amount"]) for a in amounts] == [("RWF", 50_000_000, "50.000000"),
                                                                           ("USD", 1_000_000, "1.000000")]


# ---------------------------------------------------------------- what is counted
def test_retries_are_attempts_and_each_message_is_counted_once(shop):
    bid = shop()
    message, interrupted = uuid.uuid4(), uuid.uuid4()
    write(bid, event(at(2026, 10, 2), attempts=3),  # one model call that needed three HTTP attempts
          event(at(2026, 10, 2), "embedding", units=4, attempts=2),  # one request for four texts, retried once
          event(at(2026, 10, 3), "wa_out", status="failed", source_id=message, attempts=1),
          event(at(2026, 10, 3), "wa_out", status="success", source_id=message, attempts=2),
          event(at(2026, 10, 9), "wa_out", status="late_failed", source_id=message, attempts=2, units=0),
          event(at(2026, 10, 4), "wa_out", status="unknown", source_id=interrupted, attempts=1, is_real=None))
    october = report(bid, "Africa/Kigali", "2026-10")
    llm, emb = kind(october, "llm_call"), kind(october, "embedding")
    assert (llm["events"], llm["http_attempts"]) == (1, 3)
    assert (emb["events"], emb["units"], emb["http_attempts"]) == (1, 4, 2)
    real = kind(october, "wa_out", "real")
    assert real["by_status"] == {"failed": 1, "late_failed": 1, "success": 1}
    assert (real["send_attempts"], real["messages_attempted"], real["messages_accepted"],
            real["messages_late_failed"]) == (2, 1, 1, 1)
    unknown = kind(october, "wa_out", "unknown")  # whether that attempt was real is not known: its own group
    assert (unknown["send_attempts"], unknown["messages_attempted"], unknown["messages_accepted"]) == (1, 1, 0)
    # A WhatsApp row's `attempts` is the attempt's number: never added up as HTTP attempts.
    assert all(line["http_attempts"] is None for line in october["lines"] if line["kind"] == "wa_out")
    out = usage_report.format_text(october)
    assert "2 send attempt(s) (failed 1, success 1); 1 message(s), 1 accepted by WhatsApp, 1 failed later" in out


def test_real_and_simulated_traffic_are_never_added_together(shop):
    bid = shop()
    write(bid, event(at(2026, 10, 2), "wa_in"), event(at(2026, 10, 2), "wa_in"),
          event(at(2026, 10, 2), "wa_in", is_real=False, market=None))
    october = report(bid, "Africa/Kigali", "2026-10")
    assert [(k["traffic"], k["messages"]) for k in october["kinds"]] == [("real", 2), ("simulated", 1)]
    assert {(line["traffic"], line["market"], line["events"]) for line in october["lines"]} == {
        ("real", "250", 2), ("simulated", None, 1)}


def _random_rows(rng: random.Random, n: int) -> list[dict]:
    """Rows of every kind, status, traffic, price and currency, many sharing a source (a message's attempts), spread
    over a window wider than October 2026 in both Kigali and UTC, plus rows on the boundaries themselves."""
    sources = [uuid.uuid4() for _ in range(12)]
    first, seconds = at(2026, 9, 29), int(timedelta(days=34).total_seconds())
    edges = [at(2026, 9, 30, 22), at(2026, 10, 1), at(2026, 10, 31, 21, 59, 59, 999999), at(2026, 10, 31, 22),
             at(2026, 11, 1)]
    rows = []
    for i in range(n):
        when = edges[i] if i < len(edges) else first + timedelta(seconds=rng.randrange(seconds))
        name = rng.choice(KINDS)
        if name in AI_KINDS:
            status = rng.choice(["success", "success", "error"])
            tokens = None if status == "error" or rng.random() < 0.15 else rng.randint(1, 900)
            fields = dict(status=status, model=rng.choice(["m1", "m2", None]), attempts=rng.randint(1, 3),
                          input_tokens=tokens, source_id=rng.choice(sources + [None]))
            if name == "llm_call":
                fields.update(source_type=rng.choice(["agent_run", "conversation"]), tool_calls=rng.randint(0, 3),
                              output_tokens=None if tokens is None else rng.randint(1, 90))
            else:
                fields.update(source_type=rng.choice(["product", "product_search"]), units=rng.randint(1, 6))
        elif name == "wa_in":
            fields = dict(is_real=rng.choice([True, False]), market=rng.choice(["250", "256", None]))
        else:
            status = rng.choice(["success", "success", "failed", "unknown", "late_failed"])
            message_kind = rng.choice(["free_form", "template", None])
            fields = dict(status=status, is_real=rng.choice([True, False] + ([None] if status == "unknown" else [])),
                          message_kind=message_kind, template_name="order_alert" if message_kind == "template" else None,
                          market=rng.choice(["250", None]), attempts=rng.randint(1, 3),
                          units=0 if status == "late_failed" else 1, source_id=rng.choice(sources))
        if rng.random() < 0.6:
            fields.update(cost_micros=rng.randint(0, 9000), currency=rng.choice(["USD", "RWF"]),
                          price_version=rng.choice(["v1", "v2"]))
        rows.append(event(when, name, **fields))
    return rows


def _cost(rows: list[dict], *, by_kind: bool = False) -> dict:
    priced = [r for r in rows if r.get("cost_micros") is not None]
    micros, versions = defaultdict(int), defaultdict(set)
    for r in priced:
        micros[r["currency"]] += r["cost_micros"]
        versions[r["currency"]].add(r["price_version"])
    unpriced = len(rows) - len(priced)
    out = {"pricing": ("no_usage" if not rows else "unpriced" if not priced else
                       "partially_priced" if unpriced else "priced"),
           "priced_events": len(priced), "unpriced_events": unpriced,
           "amounts": [{"currency": c, "micros": m, "amount": f"{m / 1_000_000:.6f}", "price_versions": sorted(versions[c])}
                       for c, m in sorted(micros.items())]}
    if by_kind:
        counts = defaultdict(int)
        for r in rows:
            if r.get("cost_micros") is None:
                counts[r["kind"]] += 1
        out["unpriced_events_by_kind"] = {k: counts[k] for k in KINDS if counts[k]}
    return out


def _expected(rows: list[dict], start: datetime, end: datetime) -> dict:
    """The report, folded row by row in Python: an oracle that shares no code with the report's SQL."""
    rows = [r for r in rows if start <= r["occurred_at"] < end]
    lines, kinds = defaultdict(list), defaultdict(list)
    for r in rows:
        traffic = None if r["kind"] in AI_KINDS else TRAFFIC[r.get("is_real")]
        lines[(r["kind"], traffic, r.get("provider"), r.get("model"), r.get("configured_model"), r.get("source_type"),
               r["status"], r.get("message_kind"), r.get("template_name"), r.get("market"))].append(r)
        kinds[(r["kind"], traffic)].append(r)

    def unreported(group):
        return sum(1 for r in group if r["status"] == "success" and (
            r.get("input_tokens") is None or (r["kind"] == "llm_call" and r.get("output_tokens") is None)))

    out_lines = {}
    for key, group in lines.items():
        name = key[0]
        ai, llm = name in AI_KINDS, name == "llm_call"
        out_lines[key] = {
            "events": len(group), "units": sum(r["units"] for r in group),
            "http_attempts": sum(r["attempts"] for r in group) if ai else None,
            "input_tokens": sum(r.get("input_tokens") or 0 for r in group) if ai else None,
            "output_tokens": sum(r.get("output_tokens") or 0 for r in group) if llm else None,
            "tool_calls": sum(r["tool_calls"] for r in group) if llm else None,
            "unreported_usage": unreported(group) if ai else None,
            "messages": len({r["source_id"] for r in group if r["source_id"]}) if name in SEND_KINDS else None,
            "cost": _cost(group)}
    out_kinds = {}
    for (name, traffic), group in kinds.items():
        by_status = defaultdict(int)
        for r in group:
            by_status[r["status"]] += 1
        k = {"kind": name, "traffic": traffic, "events": len(group), "by_status": dict(sorted(by_status.items()))}
        if name in AI_KINDS:
            k.update(units=sum(r["units"] for r in group), http_attempts=sum(r["attempts"] for r in group),
                     input_tokens=sum(r.get("input_tokens") or 0 for r in group), unreported_usage=unreported(group))
            if name == "llm_call":
                k.update(output_tokens=sum(r.get("output_tokens") or 0 for r in group),
                         tool_calls=sum(r["tool_calls"] for r in group))
        elif name == "wa_in":
            k["messages"] = len(group)
        else:
            def distinct(statuses):
                return len({r["source_id"] for r in group if r["status"] in statuses})
            k.update(send_attempts=sum(1 for r in group if r["status"] in ATTEMPT_STATUSES),
                     messages_attempted=distinct(ATTEMPT_STATUSES), messages_accepted=distinct(("success",)),
                     messages_late_failed=distinct(("late_failed",)))
        k["cost"] = _cost(group)
        out_kinds[(name, traffic)] = k
    return {"lines": out_lines, "kinds": out_kinds, "cost": _cost(rows, by_kind=True)}


def _line_key(line: dict) -> tuple:
    return tuple(line[d] for d in ("kind", "traffic", "provider", "model", "configured_model", "source_type", "status",
                                   "message_kind", "template_name", "market"))


def _measures(line: dict) -> dict:
    return {f: line[f] for f in ("events", "units", "http_attempts", "input_tokens", "output_tokens", "tool_calls",
                                 "unreported_usage", "messages", "cost")}


def _check(rep: dict, expected: dict) -> None:
    assert {_line_key(line): _measures(line) for line in rep["lines"]} == expected["lines"]
    assert len(rep["lines"]) == len(expected["lines"])  # no line twice
    assert {(k["kind"], k["traffic"]): k for k in rep["kinds"]} == expected["kinds"]
    assert rep["cost"] == expected["cost"]


def test_every_figure_matches_a_row_by_row_count_of_the_ledger(shop):
    rng = random.Random(20261010)
    a, b = shop("Africa/Kigali", "Alpha"), shop("Africa/Kigali", "Beta")
    rows_a, rows_b = write(a, *_random_rows(rng, 260)), write(b, *_random_rows(rng, 140))
    kigali = (at(2026, 9, 30, 22), at(2026, 10, 31, 22))
    utc = (at(2026, 10, 1), at(2026, 11, 1))
    expected_a = _expected(rows_a, *kigali)
    assert len(expected_a["lines"]) > 20 and expected_a["cost"]["pricing"] == "partially_priced"  # a rich sample
    _check(report(a, "Africa/Kigali", "2026-10"), expected_a)
    every_shop = usage_report.operator_month(engine, "2026-10")
    _check(shop_in(every_shop, a), _expected(rows_a, *utc))
    _check(shop_in(every_shop, b), _expected(rows_b, *utc))
    platform = _expected(rows_a + rows_b, *utc)
    assert {(k["kind"], k["traffic"]): k for k in every_shop["kinds"]} == platform["kinds"]
    assert every_shop["cost"] == platform["cost"]


# ---------------------------------------------------------------- tenant isolation and interfaces
def test_the_api_reports_the_signed_in_shop_only(fashion, electronics):
    a, b = uuid.UUID(fashion.business_id), uuid.UUID(electronics.business_id)
    now = datetime.now(UTC)
    write(a, event(now))
    write(b, event(now), event(now, "wa_in"), event(now, "wa_out", cost_micros=7, currency="RWF", price_version="x"))
    month = usage_report.Month.current(usage_report.shop_zone("Africa/Kigali")[0], now)
    mine = fashion.get("/api/usage/monthly", params={"month": str(month)}).json()
    # A business id passed by the caller is ignored: the shop comes from the token.
    spoofed = fashion.get("/api/usage/monthly", params={"month": str(month), "business_id": str(b)}).json()
    theirs = electronics.get("/api/usage/monthly").json()
    assert mine == spoofed and mine["business_id"] == str(a) and mine["timezone"] == "Africa/Kigali"
    assert [(k["kind"], k["events"]) for k in mine["kinds"]] == [("llm_call", 1)]
    assert theirs["business_id"] == str(b) and theirs["month"] == str(month)
    assert sum(k["events"] for k in theirs["kinds"]) == 3 and theirs["cost"]["amounts"][0]["currency"] == "RWF"
    assert fashion.client.get("/api/usage/monthly").status_code == 401
    bad = fashion.get("/api/usage/monthly", params={"month": "2026-13"})
    assert bad.status_code == 422 and "YYYY-MM" in bad.json()["detail"]


def test_the_operator_command_reports_one_shop_in_its_time_zone_or_every_shop_in_utc(shop, capsys):
    a, b = shop("Africa/Kigali", "Alpha"), shop("Europe/Paris", "Beta")
    write(a, event(at(2026, 10, 31, 22, 30)))  # 1 November in Kigali, October in UTC
    write(b, event(at(2026, 10, 10), "wa_in"))
    assert main(["usage-report", "--month", "2026-10", "--json"]) == 0
    every_shop = json.loads(capsys.readouterr().out)
    assert every_shop["timezone"] == "UTC" and {s["business_id"] for s in every_shop["shops"]} == {str(a), str(b)}
    assert main(["usage-report", "--month", "2026-10", "--business", str(a), "--json"]) == 0
    one = json.loads(capsys.readouterr().out)
    assert (one["timezone"], one["business_id"], one["kinds"]) == ("Africa/Kigali", str(a), [])
    assert main(["usage-report", "--month", "2026-11", "--business", str(a), "--json"]) == 0
    assert calls(json.loads(capsys.readouterr().out)) == 1
    assert main(["usage-report", "--month", "2026-10"]) == 0
    out = capsys.readouterr().out
    assert "Usage report 2026-10: all shops, time zone UTC" in out and "Shops with usage: 2" in out
    assert "Not priced: 2 event(s) (llm_call 1, wa_in 1)" in out
    assert main(["usage-report", "--month", "2026-1"]) == 1 and "YYYY-MM" in capsys.readouterr().err
    assert main(["usage-report", "--business", "not-an-id"]) == 1 and "business id" in capsys.readouterr().err
    assert main(["usage-report", "--business", str(uuid.uuid4())]) == 1
    assert "no business" in capsys.readouterr().err


class _MustNotBeCalled(LLMProvider):
    name = "must-not-be-called"

    def complete(self, *a, **kw):
        raise AssertionError("a report called a model")


class _MustNotSend(WhatsAppAdapter):
    mode = "test"

    def send_text(self, to, body):
        raise AssertionError("a report sent a message")


def _ledger_fingerprint() -> str:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT * FROM usage_events ORDER BY id")).all()
    return hashlib.sha256(repr([tuple(r) for r in rows]).encode()).hexdigest()


def test_reports_only_read(fashion, monkeypatch, capsys):
    bid = uuid.UUID(fashion.business_id)
    write(bid, event(datetime.now(UTC)), event(datetime.now(UTC), "wa_out", status="failed"))
    set_provider_override(_MustNotBeCalled())
    set_adapter_override(_MustNotSend())
    monkeypatch.setattr(embeddings, "get_embedder", lambda: pytest.fail("a report embedded a text"))
    before = _ledger_fingerprint()
    assert fashion.get("/api/usage/monthly").status_code == 200
    assert main(["usage-report", "--json"]) == 0 and main(["usage-report", "--business", str(bid)]) == 0
    capsys.readouterr()
    assert _ledger_fingerprint() == before
    with usage_report.snapshot(engine) as db:  # every report runs in such a transaction: PostgreSQL refuses writes
        assert db.execute(text("SHOW transaction_read_only")).scalar() == "on"
        assert db.execute(text("SHOW transaction_isolation")).scalar() == "repeatable read"
        with pytest.raises(InternalError, match="read-only transaction"):
            db.execute(update(Business).where(Business.id == bid).values(name="changed by a report"))


def test_the_shop_query_uses_the_ledger_index_on_shop_and_time(shop):
    """A shop with a year of history next to a busier shop, with fresh statistics: one month of the shop's report reads
    that month of its own rows through the (business_id, occurred_at) index, not its whole history (the idempotency
    index, which also starts with business_id) and not every shop's month (the occurred_at index)."""
    bid, busy = shop(), shop()
    bulk = text("INSERT INTO usage_events (id, business_id, occurred_at, kind, idempotency_key, status, units, attempts, "
                "tool_calls) SELECT gen_random_uuid(), :b, :t + i * :step, 'llm_call', 'bulk:' || i, 'success', 1, 1, 0 "
                "FROM generate_series(1, :n) AS i")
    with engine.begin() as conn:
        conn.execute(bulk, {"b": bid, "t": at(2026, 1, 1), "step": timedelta(hours=3, minutes=39), "n": 2400})
        conn.execute(bulk, {"b": busy, "t": at(2026, 10, 1), "step": timedelta(minutes=20), "n": 2000})
        conn.execute(text("ANALYZE usage_events"))
    with SessionLocal() as s:
        stmt = usage_report.lines_query(UsageEventRepo(s, bid), at(2026, 10, 1), at(2026, 11, 1))
    compiled = stmt.compile(dialect=engine.dialect)
    with engine.begin() as conn:  # SET LOCAL ends with this transaction
        conn.exec_driver_sql("SET LOCAL enable_seqscan = off")
        plan = "\n".join(r[0] for r in conn.exec_driver_sql("EXPLAIN " + str(compiled), compiled.params))
    assert "ix_usage_events_business_occurred" in plan, plan
