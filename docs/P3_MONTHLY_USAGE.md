# P3 — Monthly usage reporting: design

Roadmap: `docs/ROADMAP.md` Phase C2 (Phase 4 P3). Status: decisions approved by the product owner on 2026-10-10
(§6); implemented, tested and CI-verified (`db0bc29`, GitHub Actions run 38075375616; §7). It builds only on the usage
ledger (P0, P2) and needs no prices.

## 1. Goal

Per tenant and month: what was used. AI model calls, paid embeddings requests, inbound WhatsApp messages and send
attempts, with real and simulated traffic, unknown outcomes and unpriced events kept visibly apart. Every figure is
read from `usage_events`, the insert-only ledger, never from `agent_runs` or `messages`.

## 2. What exists (verified in code at `0f82acb`)

- `usage_events`: kinds `llm_call`, `embedding`, `wa_in`, `wa_out`, `wa_alert`; indexes `(business_id, occurred_at)`
  and `(occurred_at)`; cost per row in millionths of `currency` with its `price_version`, NULL = unpriced.
- `GET /api/dashboard/usage?days=N` computes "usage" from `agent_runs` and `messages`. Those are not usage figures:
  `llm_calls` there also counts the offline rules engine, the calls of a rolled-back turn are lost with its run, and
  it counts messages, not send attempts. No dashboard page calls it (nothing in `frontend/`); the tenant-isolation test
  covers it.
- No monthly aggregation and no operator report.

## 3. Design

- A read-only service, `app/services/usage_report.py`, reads `usage_events` for `[start of month, start of next
  month)` in the chosen time zone through `UsageEventRepo` (tenant-scoped), in three grouped queries per shop (§7).
  No new table, no migration, nothing written.
- Groups: `kind`; for `llm_call` and `embedding` also provider, served model (null when the provider reported none),
  configured model, source and status; for WhatsApp also real / simulated / unknown (`is_real` true, false, NULL),
  `message_kind`, template name, status and market.
- Measures per group: events, units, input and output tokens, attempts; for cost, the sum of `cost_micros` per
  currency over priced rows, the number of unpriced rows, and the price versions involved.
- Never: an unpriced row counted as 0, amounts in different currencies added together, real and simulated WhatsApp
  traffic added together, or a late failure counted as a send (it is its own line, `units` 0).
- Surfaces: an operator CLI, `python -m app.cli usage-report --month YYYY-MM [--business <id>] [--json]` (every
  tenant when no id is given), and a tenant API, `GET /api/usage/monthly?month=YYYY-MM` (the tenant from the JWT
  only; added to the tenant-isolation matrix), read-only, for the usage UI of C5. No UI in C2.
- Volume is unknown (no real traffic yet). A test seeds a month of synthetic rows for two tenants and checks the
  tenant query plan uses the `(business_id, occurred_at)` index.

## 4. Known limits carried in

- Embeddings costs are rounded per row (P2 finding 2). The report shows embeddings token totals next to the recorded
  cost; pricing a total from tokens needs the price of each recorded `price_version`, which the operator's current
  file may no longer hold, so it is left to the cost model (C3).
- Totals are only as complete as the ledger: metering is best effort (`docs/OPERATIONS.md` › Usage metering); a
  failed ledger write is in the logs (`usage.record_failed`), not in the report.

## 5. Tests

Tenant isolation (API and CLI per business); month boundaries (an event at 23:30 UTC on the last day of a month is in
the next month for a shop in Africa/Kigali, UTC+2, and in the same month in UTC); real, simulated and unknown kept
apart; unpriced counts; no sum across currencies; late failures as their own line; an empty month; an invalid month;
nothing written.

## 6. Decisions (approved by the product owner, 2026-10-10)

- **C2-D1 — month boundary.** A merchant's (tenant's) report uses the shop's configured time zone; the cross-shop
  operator report uses UTC. Calendar-month boundaries are defined explicitly and tested at month and year boundaries
  and across daylight-saving changes.
- **C2-D2 — interfaces.** A read-only operator command for the monthly report and a read-only tenant API whose shop
  is enforced by the server. No dashboard UI: that is C5.
- **C2-D3 — the existing endpoint.** `/api/dashboard/usage` stays unchanged, documented as an activity metric, not a
  complete provider-usage or cost report; its semantics and future are revisited in C5.
- Safeguards required with them: `usage_events` stays the one insert-only ledger; model, paid-embedding and
  WhatsApp usage are aggregated without double-counting retries or evaluation runs; an unknown price is never 0 and
  unpriced or partially priced usage is named; reporting makes no provider call, sends nothing and changes no usage
  record.

## 7. As built (2026-10-10)

- `app/services/usage_report.py`: `tenant_month` (one shop, its time zone), `operator_month` (every shop, UTC, with
  platform totals) and `format_text` (the command's view). Three grouped queries per shop through `UsageEventRepo`
  (tenant-scoped): lines, cost amounts per currency, messages counted once.
- Month: from the first instant of local day 1 to the first instant of local day 1 of the next month (end
  excluded), computed with `zoneinfo`. A local midnight that a daylight-saving change skips is read with the offset
  in force before the change, which for a change at midnight is the change itself (tested with America/Asuncion,
  1 October 2023). A configured zone that cannot be used is replaced by UTC, which needs no time zone data, and the
  report says so (`timezone_note`).
- Read-only by construction: every report runs in one REPEATABLE READ, READ ONLY transaction on a connection of its
  own, so all its figures come from one snapshot and PostgreSQL refuses writes (tested).
- Report shape: `lines` (one per kind and detail: provider, served and configured model, source, status; for
  WhatsApp real / simulated / unknown, message kind, template, market), `kinds` (per kind, and per traffic for
  WhatsApp) and `cost`. A measure that does not apply to a kind is null, never 0. `cost.pricing` is `priced`,
  `partially_priced`, `unpriced` or `no_usage`; amounts are per currency over priced events only, with their price
  versions; `unpriced_events_by_kind` names what has no price. Inbound messages are never priced (P0).
- Interfaces: `GET /api/usage/monthly?month=YYYY-MM` (shop from the token; this month when omitted) and
  `python -m app.cli usage-report --month YYYY-MM [--business <id>] [--json]`.
- `/api/dashboard/usage`: response unchanged; its description now says it is an activity metric (C2-D3).
- Tests: `tests/test_usage_report.py` (month, year and daylight-saving boundaries; empty months; unpriced, partially
  priced and multi-currency costs; retries and messages; real/simulated/unknown; every figure against a row-by-row
  Python count of random ledger rows for two shops; API and command; read-only; the index used) and the monthly
  report added to the tenant-isolation snapshot (`tests/test_tenant_isolation.py`). Three deliberate defects (month
  end included, query not scoped to the shop, WhatsApp attempt numbers added up) were each caught by the tests.

Findings:
1. **Time zone data comes from the operating system image.** `tzdata` is not a Python requirement (on Windows it is
   installed as a dependency of psycopg); the backend image (`python:3.11-slim`, Debian 13) carries Debian's tzdata
   (checked: 2026c), CI's Ubuntu runner its own. A base image without it would break business hours (`hours.py`) and
   shop-time-zone reports (they would fall back to UTC and say so; the UTC report needs no time zone data). Proposed
   (Phase F): pin `tzdata` in `requirements.txt`, or check a zone in the image build.
2. Embeddings costs stay rounded per row (P2 finding 2); the report shows embeddings token totals so a total can be
   priced from tokens later (C3).

Not validated: no real traffic has been reported (all figures in tests come from rows written by the tests); no
real price list exists, so every real report would show its usage as unpriced; nothing has been deployed.
