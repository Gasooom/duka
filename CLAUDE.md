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
7. **Orders need an explicit confirmation.** The LLM can only `prepare_checkout`; an order is created only by
   `CheckoutService.confirm` when a later customer message is an explicit YES to the delivered, unchanged summary.
   Never assume a delivery zone or address. Never add a tool that creates orders or confirms payments.
8. **Sensitive actions are audited** (`audit_service.record`): manual payments, voids, order status changes,
   takeovers. `audit_events` is append-only.
5. **Webhooks never crash** on LLM/tool failure: fallback message + `agent_runs.status=error`.
6. **Durable in, outbox out.** Inbound messages are committed to `webhook_events` before the webhook returns 200
   and processed by `workflows/worker.py`. Customer-facing messages go through `send_to_customer` (queues in the
   current transaction) and are sent after commit (`commit_and_deliver` or the worker). Never call an adapter
   directly, and never send before the state the message describes is committed.

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
- **Schema change:** edit models → `alembic revision --autogenerate -m "..."` → review → test from clean DB.

## Conventions
- Money is `Numeric(12,2)`/`Decimal`; floats only at the JSON boundary.
- Structured JSON logs via `log_event`/`log_operation`; never log secrets (formatter redacts known keys).
- Commit per milestone with conventional commits (`feat(scope): ...`, `test(scope): ...`).
