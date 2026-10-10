# P3 — Monthly usage reporting: design (for review)

Roadmap: `docs/ROADMAP.md` Phase C2 (Phase 4 P3). Status: design proposed 2026-10-10; the decisions in §6 are needed
before implementation. It builds only on the usage ledger (P0, P2) and needs no prices.

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

## 3. Proposed design

- A read-only service, `app/services/usage_report.py`: `monthly(db, business_id, month, tz)` runs one grouped query
  over `usage_events` for `[start of month, start of next month)` in the chosen timezone, through `UsageEventRepo`
  (tenant-scoped). No new table, no migration, nothing written.
- Groups: `kind`; for `llm_call` and `embedding` also provider, served model (the configured one when the provider
  reported none, shown as such) and status; for WhatsApp also real / simulated / unknown (`is_real` true, false,
  NULL), `message_kind`, template name, status and market.
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

## 6. Decisions needed

- **C2-D1 — month boundary.** (a) UTC calendar months everywhere (the guard's buckets are UTC, decision D2); (b) the
  shop's timezone (`businesses.timezone`, default `Africa/Kigali`) for a tenant's figures and UTC for the operator's
  all-tenant report, each report stating its boundaries; (c) a timezone chosen per request. Recommendation: (b): "this
  month" for a merchant is their local month, as business hours already are, while the operator compares all tenants
  on one boundary. Whether provider invoices use UTC months has not been checked.
- **C2-D2 — surfaces in C2.** The operator CLI only, or the CLI and the read-only tenant API now. Recommendation:
  both; the API changes nothing a merchant sees until C5 builds the page.
- **C2-D3 — the existing `/api/dashboard/usage`.** (a) Keep it as it is, documented as activity rather than usage;
  (b) rebuild it on the ledger; (c) remove it. Recommendation: (a) for now and decide with C5: changing what an
  existing endpoint returns is a change for any client that reads it.
