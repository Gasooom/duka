# CLAUDE.md — engineering guide for this repo

Multi-tenant WhatsApp AI commerce platform. **Build the engine once, configure it per business.**

## Non-negotiables
1. **Tenant isolation.** Every business-owned table has `business_id NOT NULL`. Access tenant data only through
   `app/repositories` (`TenantRepository` subclasses bound to a `business_id`). Never write unscoped
   `select(Model)` against tenant tables in services/routes. The tenant comes from the JWT (admin API) or from
   `whatsapp_accounts.phone_number_id` (webhooks) or from the payment row (payment callbacks) — never from input.
   `tests/test_tenant_isolation.py` must stay green; add a case for every new tenant-owned entity. New id routes
   must be added to `IDOR_MATRIX`; new tenant->tenant foreign keys need a `duka_enforce_same_tenant` trigger in a
   migration (both are enforced by meta-tests). Public registration is closed in production; tenants are created
   with `python -m app.cli create-business`.
2. **No business-specific code.** No `if business == ...`. Differences live in `businesses`, `agent_configs`,
   `business_settings`, `delivery_zones`, products and knowledge.
3. **The LLM never decides facts.** Prices, stock, totals, delivery fees, order/payment status come from tools →
   services → DB. The agent never touches the DB directly; tools run inside a SAVEPOINT.
4. **Payments are confirmed only by the provider** (signed mock callback, or MoMo status re-query) **or by an
   owner's audited manual record** (`POST /api/orders/{id}/payments`: method + evidence, `confirmation_source=owner`).
   The agent can never set `paid`; it can only report a customer's reference as *pending*. `Order.payment_status`
   is changed only in `PaymentService`.
5. **Webhooks never crash** on LLM/tool failure: fallback message + `agent_runs.status=error`.
6. **Durable in, outbox out.** Inbound messages are committed to `webhook_events` before the webhook returns 200
   and processed by `workflows/worker.py`. Customer-facing messages go through `send_to_customer` (queues in the
   current transaction) and are sent after commit (`commit_and_deliver` or the worker). Never call an adapter
   directly, and never send before the state the message describes is committed.
7. **Orders need an explicit confirmation.** The LLM can only `prepare_checkout`; an order is created only by
   `CheckoutService.confirm` when a later customer message is an explicit YES to the delivered, unchanged summary
   and nothing else was sent after it (a YES after the conversation moved on re-sends the summary instead). Only the
   server writes the summary and asks for that YES. Never assume a delivery zone or address (the customer's own
   address from earlier in the conversation may be reused; the summary shows it). Never add a tool that creates
   orders or confirms payments.
8. **Sensitive actions are audited** (`audit_service.record`): manual payments, voids, order status changes,
   takeovers. `audit_events` is append-only.
9. **Model text is checked before it is sent.** `agents/grounding.py` verifies every LLM reply against this turn's
   tool results; on a violation the deterministic render is sent instead. Don't bypass it, and add a regression
   case to `tests/test_ai_safety.py` for every new kind of fact the agent can state. Order/payment/status/cart
   claims in Kinyarwanda, French and Swahili are `LOCAL_CLAIMS`: change them through the sentence lists in
   `tests/test_grounding_multilingual.py`; their wording awaits native review (`docs/MULTILINGUAL_GROUNDING_REVIEW.md`).
10. **Customer-facing text is localised, facts are not.** Every server-written customer message goes through
   `app/i18n.py` in the conversation language (`conversation_language(conv, business)`); add new texts there in
   all six languages (a test enforces it) and never translate names, prices, SKUs, order numbers or owner text.
11. **Search returns real matches or nothing.** `ProductService.search` returns a product only when a query word
   matches what it IS (name, category, SKU, attributes) — not a colour alone, not a description mention. Vector
   similarity ranks but never admits (the default hash embedding collides: "phone" scored 0.46 against a tea).
   Never add a fallback that fills an empty result. `tests/test_search.py` holds the regressions.

## Layout
```
backend/app
  api/routes      thin HTTP layer (validation, auth, commit)
  services        business logic (deterministic)
  repositories    tenant-scoped data access
  agents          engine.py (context + tool loop), providers/ (openai_compat, rules), render.py
  tools           registry.py + commerce_tools.py (the 18 agent tools)
  integrations    whatsapp/ (parser, cloud+dev adapters), payments/ (base, mock, momo)
  workflows       inbound.py (ingest/claim/process), worker.py (threads), orders.py (checkout confirmation, owner
                  decisions, manual payments), handoff.py (human control rules), payments.py (provider confirmation)
frontend/         Next.js admin dashboard (proxies /api via BACKEND_URL; no secrets in browser)
```

## Commands
- `docker compose up --build` → db + backend (auto-migrates) + frontend; `make seed`
- Local: `cd backend && alembic upgrade head && uvicorn app.main:app --reload` ; `python -m seed.seed`
- Tests: `cd backend && pytest -q` (needs Postgres+pgvector; uses `commerce_test` DB, rebuilt via Alembic)
- Lint: `cd backend && ruff check app tests seed`
- Demo: `scripts/demo_chat.sh fashion@duka.dev 250788555666 "black sneakers under 100k" "add 2" ...`

## Adding things
- **New tool:** add args model + function in `tools/commerce_tools.py`, `register(Tool(...))`, call services only,
  raise `DomainError` for user-facing failures, add render case in `agents/render.py`, add tests. Tools must not
  create orders, confirm payments or change order status.
- **New payment provider:** implement `PaymentProvider` (`request_payment`, `get_status`), register in
  `integrations/payments/__init__.py`, add a callback route that re-verifies status.
- **New LLM vendor:** usually just `LLM_BASE_URL`/`LLM_MODEL` (OpenAI-compatible). Otherwise implement `LLMProvider`.
- **Agent/prompt/tool change:** run `python -m evals.run --provider rules` and `--provider adversarial` (both are
  also in `pytest`); add cases to `evals/cases_v1.json` for new behaviour, bump its version and regenerate
  baselines with `--update-baseline` only for intended changes. Run `--provider openai_compat` when a key is set.
- **Schema change:** edit models → `alembic revision --autogenerate -m "..."` → review → test from clean DB.

## Production MVP Roadmap — Merchant Validation and Operational Readiness
Objective: a merchant runs products, inventory, conversations and orders through WhatsApp and the dashboard, with
safe automation, accurate commerce facts, and control over operations and costs. Detail, acceptance criteria and the
risk register: `docs/ROADMAP.md`. History and evidence: `docs/MILESTONES.md`. Pilot checklist:
`docs/EXTERNAL_VALIDATION.md`. Production-ready = evidence for all four questions: **Q1** a real merchant operates
independently · **Q2** AI answers are accurate and safe (approved thresholds, native review, production model) ·
**Q3** revenue per merchant exceeds real costs (no invented prices; unpriced ≠ free) · **Q4** safe under growth and
failure (measured targets).

Status (update with evidence; values: NOT STARTED · IN PROGRESS · LOCALLY VERIFIED · CI VERIFIED · EXTERNALLY
VALIDATED · BLOCKED · ACCEPTED). Last verified 2026-10-10 at `0f82acb`.

| Phase | Scope | Status |
|---|---|---|
| A | Baseline, risk register, existing guarantees kept | IN PROGRESS (register in `docs/ROADMAP.md` §6) |
| B | P1 Runaway Conversation Guard (`docs/P1_RUNAWAY_GUARD.md`) | CI VERIFIED — code complete: B1 deadlines (`0ac995e`), B2 counters + observe (`a3493d3`), B3 per-message budget enforced by default (`d11de7d`), B4 per-customer limits (`a12b72c`), B5 tenant limits + operator overrides (`f3fa3e4`), each CI-green; B6 deferred (D5). External validation BLOCKED: customer/tenant limits not chosen (observe data), no real provider or traffic |
| C | Usage/cost visibility: embedding metering, monthly aggregation, cost model, quotas, usage UI; billing last | IN PROGRESS — C1 embedding metering CI VERIFIED (`0f82acb`; no real provider yet); C2 monthly usage design proposed (`docs/P3_MONTHLY_USAGE.md`), decisions C2-D1–D3 pending; P0 metering CI VERIFIED; no prices |
| D | Merchant operations and inventory reliability (audit, then gaps) | NOT STARTED (existing workflows CI VERIFIED) |
| E | AI and multilingual validation | IN PROGRESS — offline/adversarial CI VERIFIED; real model LOCALLY VERIFIED (dev key); production model, thresholds, native review BLOCKED |
| F | Production infrastructure and security | LOCALLY VERIFIED (rehearsal); host, domain, monitor, off-site backup BLOCKED |
| G | External validation and supervised pilot | BLOCKED (Meta, LLM account, host, merchant, reviewers, legal) |
| H | Go/no-go on Q1–Q4 | NOT STARTED |

Release gates are separate and never interchangeable: CODE COMPLETE → LOCAL TESTS PASS → CI PASS → EXTERNAL
INTEGRATION VERIFIED → PILOT ACCEPTED → PRODUCTION READY. A mock is not an integration; an offline eval is not
production accuracy; CI is not recovery, Meta delivery, merchant usability, economics or multi-instance behaviour.

Execution loop per milestone: inspect → plan the smallest testable change → review integrity/security/isolation/
rollback risks → implement only that → focused tests, then the full suite, ruff, evals, `alembic check` as relevant →
challenge the result (bypasses, misleading success replies) → document evidence and limits → small isolated commit →
push after reviewing the diff → confirm CI on that commit → update the status above. Never claim a test passed
without observing it; never weaken a test, grounding rule, security control or eval baseline to get green.

Proceed without asking on routine, reversible, locally verifiable work. **Stop** for product decisions, destructive
operations, external spend or new paid services, deployments, live or production data, and changes that materially
alter merchant behaviour (ship those behind an off/observe/enforce switch). For an unavailable credential, merchant,
reviewer or environment: finish the local work, write the exact validation checklist, mark the gate BLOCKED, and
never fabricate external evidence.

## Conventions
- Money is `Numeric(12,2)`/`Decimal`; floats only at the JSON boundary.
- Structured JSON logs via `log_event`/`log_operation`; never log secrets (formatter redacts known keys).
- Commit per milestone with conventional commits (`feat(scope): ...`, `test(scope): ...`).
