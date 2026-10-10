# Duka — multi-tenant WhatsApp AI commerce platform

**Build the engine once, configure it per business.** One backend, one agent, one toolset. A new business is
just data: profile, WhatsApp number, products (CSV), delivery zones, knowledge, agent tone/rules and payment
provider. No chatbot code is written per business. `Kigali Fashion` and `Mama's Electronics` run on the same code.

```
 WhatsApp Cloud API ──► POST /webhooks/whatsapp ── verify X-Hub-Signature-256
                              │  persist to webhook_events, commit, then 200 (5xx -> Meta redelivers)
                              │  worker threads claim events (SKIP LOCKED, per-customer FIFO, lease, retry)
                              ▼
            phone_number_id ─► whatsapp_accounts ─► business_id (tenant)   ✗ unknown → dropped
                              ▼
          customer upsert (per business) ─► conversation ─► idempotent message insert (wamid)
                              │                         └─ status=human → stop, flag for staff
                              ▼
   ┌──────────────── AgentEngine (same for every tenant) ─────────────────┐
   │ context = tenant prompt + state snapshot + summary + last N msgs     │
   │ LLMProvider (OpenAI-compatible | rules) ⇄ 18 typed tools (SAVEPOINT) │
   └──────────────────────────────┬───────────────────────────────────────┘
                                  ▼
        services (deterministic: search, cart, totals, delivery, orders, payments)
                                  ▼
          tenant-scoped repositories ─► PostgreSQL + pgvector (one shared DB)
                                  │
  reply queued in the same transaction (outbox) ─► sent after commit ─► WhatsApp adapter (cloud | dev)
  checkout: summary ─► customer's explicit YES ─► order (pending review) ─► owner alerted ─► owner accepts
  payment: manual (owner records MoMo ref / cash, audited) | provider (MoMo / mock) callback ─► re-verify ─► paid
```

It's a modular monolith: FastAPI, SQLAlchemy 2, Alembic, PostgreSQL 16 + pgvector, and a Next.js 15 dashboard.
There's no Redis, queue, vector DB or microservice, so you pay for one Postgres and one small container: the
inbound queue (`webhook_events`) and the outbound outbox are Postgres tables worked by in-process threads.

---

> **Production:** see `docs/DEPLOYMENT.md` (`deploy/docker-compose.prod.yml`: Caddy HTTPS, nothing else exposed)
> and `docs/OPERATIONS.md`. The quick start below is the development stack.

## Quick start (Docker)

```bash
cp .env.example .env            # defaults run fully offline: rules agent, hash embeddings, dev WhatsApp, mock payments
docker compose up --build       # postgres(pgvector) + backend (runs `alembic upgrade head`) + frontend
make seed                       # Demo Store, Kigali Fashion, Mama's Electronics (+ sample customers/orders)
```

- Dashboard: http://localhost:3000. Log in as `fashion@duka.dev`, `electronics@duka.dev` or `demo@duka.dev`. The password is `password123`.
- API docs (OpenAPI): http://localhost:8000/docs
- To try it: open **WhatsApp → Customer simulator** and type
  `Hi, I'm looking for black sneakers under 100,000 RWF.` → `Add the second one.` → `Place the order.` (it asks for
  your address) → `Deliver to Remera, KG 11 Ave` (exact summary) → `yes` (order placed, owner alerted). Then in
  **Orders**: *Accept order* (customer gets the payment instructions), reply `I paid, transaction id MP123` in the
  simulator, and *Confirm payment received*. Click **Debug** in the conversation to see every step.
- Or from a terminal: `make demo`

## Local without Docker
Requires PostgreSQL 16 with the `vector` extension available (`apt install postgresql-16-pgvector`).
```bash
cd backend && pip install -r requirements-dev.txt
alembic upgrade head && python -m seed.seed
uvicorn app.main:app --reload --port 8000
cd ../frontend && npm install && BACKEND_URL=http://localhost:8000 npm run dev
```

## Tests
```bash
cd backend && createdb commerce_test && pytest -q        # or: make test-docker
```
There are 696 tests (including parametrized cases). They run against real Postgres + pgvector: the schema is dropped and rebuilt with `alembic upgrade head`
on every run, which also proves the migrations work on a clean database. External HTTP (Meta, MoMo, the LLM) goes through
`httpx.MockTransport`, so the request shape, headers and retries of the real clients are tested.

| Suite | What it proves |
|---|---|
| `test_tenant_isolation.py` (**mandatory**) | Business A can't read, modify or infer B's data through any id-bearing API route (the matrix fails if a new route isn't covered), lists/search/stats/usage, the agent tools, customer chat, webhooks, the repositories or forged/stale JWTs. The database itself rejects cross-tenant references and `business_id` changes (the test fails if a new tenant FK isn't guarded). Concurrent checkouts in two stores stay isolated. |
| `test_e2e.py` | The section-37 demo, automated: signed webhook → search → "add the second one" → exact total incl. delivery → order → pay → signed provider callback → paid → WhatsApp confirmation. Store 2 then runs on the same engine with a completely different catalog. |
| `test_products.py` | CRUD, CSV validation (row/column errors, all-or-nothing by default, SKU upsert), search precision + price filter, inventory ledger |
| `test_search.py` | Catalog retrieval returns real matches or nothing: "phone" in a grocery is empty (the tea regression), colour-only and description-only matches rejected, strict price/category filters, exact/partial/plural/compound words, partial matches marked, positions survive an empty search, knowledge search needs a shared word, the phone question end to end |
| `test_grounding_real.py` | Grounding precision on replies the real model wrote (list markers, numbers in names, shop rules, listing headers, conditional wording, follow-ups checked against current facts), the address/checkout flow, priced misses, repeated YES, summary imitation, a YES after the conversation moved on |
| `test_simulator.py` | The dev simulator returns the reply even when a background worker claimed the message first |
| `test_commerce.py` | Cart math, no assumed zone, checkout needs a real address, confirmation needs a delivered + unchanged + fresh summary, snapshots, stock races, state machine |
| `test_orders_handoff.py` | Strict multilingual YES/NO, one order per confirmation, misbehaving LLM can't place orders or alter the summary, owner alerts (+ template), review/reject, handoff in EN/RW/FR/SW without false positives, takeover pauses the AI, explicit return |
| `test_payments.py` | Manual payments (evidence, owner-only, reference reuse blocked, void, audit append-only), customer-reported refs stay pending, the agent can never mark paid, mock refused in production, provider callbacks, MoMo re-verification |
| `test_whatsapp.py` | Verify handshake, signature check, duplicate delivery, unknown tenant, non-text, handoff stops the AI, encrypted tokens, Cloud adapter retries, message ordering |
| `test_ai_safety.py` | Invented prices/stock/fees/statuses/products never reach the customer, prompt injection with a fully compromised model, cross-tenant requests, no phone number sent to the LLM, retry/time budget, malformed output, handoff after repeated failures, multilingual grounding |
| `test_agent.py` | OpenAI-compatible tool loop, token/latency capture, invalid/unknown tool calls contained, iteration cap, LLM outage → fallback, greeting fast path, bounded context, summaries |
| `test_durability.py` | Persist-before-ack, crash recovery (lease), redeliveries have one effect, rollback means no reply, retries/dead-letter, per-customer ordering, outbox retry/failure, background threads |
| `test_human_control.py` | Business hours parsing in the shop's timezone, after-hours expectations for handoffs/voice notes/orders, `open_now` as a tool fact, business-wide AI pause |
| `test_dashboard_ops.py` | Setup checklist, password change signs out other devices, operator password reset, dev tools hidden in production |
| `test_reliability.py` | `/healthz`, `/readyz` ok/degraded/down, `/metrics`, ops token, log scrubbing (secrets, SQL parameters, phone numbers, query strings), retention, dead-letter requeue |
| `test_evals.py` | The versioned agent evaluation suite (`backend/evals`, v1.3.0: 80 conversations, three stores) must not regress against its baselines, with the offline engine and with a model that lies in every reply (in Kinyarwanda, French and Swahili conversations, in that language) |
| `test_grounding_multilingual.py` | False order, payment, delivery/status and cart claims in Kinyarwanda, French and Swahili are rejected, and pass once the tools returned the fact; negations, conditions, future forms, offers, questions, policies and the server's own texts are not claims; English and Arabic results unchanged; through the pipeline the customer gets the real facts in their language (wording pending native review: `docs/MULTILINGUAL_GROUNDING_REVIEW.md`) |
| `test_language.py` | Language detection (incl. Sudanese vs standard Arabic), persistence, confident switches only, code-switching, every server message in the conversation language, facts identical across languages, Arabic digits in the grounding check |
| `test_hardening.py` | Production locks dev tools and unsigned webhooks, one bad message doesn't block a batch, per-customer rate limit, env comments can't become secrets, expired and unsigned (`alg: none`) tokens refused, the demo seed refuses production before touching the database |
| `test_http_security.py` | Security headers on every response (errors, preflights and health checks too), HSTS only in production, the 10 MB body limit with and without a Content-Length (webhooks and uploads: 413, nothing stored), a signed webhook streamed without a length still verifies, per-route limits still apply |
| `test_uploads.py` | Real PDFs are read; malformed, truncated and hostile PDFs get a quick 422; malformed multipart bodies (no boundary, oversized boundary or part header, no end) get a 4xx with or without a session; health checks are answered while a slow import or upload runs |
| `test_login_throttle.py` | Per-account lockout with growing waits whoever asks, unknown emails indistinguishable (same answers, same bcrypt work), the per-address limit uses the trusted proxy hop (a forged `X-Forwarded-For` changes nothing), bounded memory, failed sign-ins logged without password or email and audited, wrong current passwords count too |
| `test_audit_events.py` | Payment instructions, settings and the AI pause, prices (dashboard and CSV), WhatsApp numbers, password change/reset and the AI settings are audited with who and before/after; a sweep of the whole table finds no password, token, hash or key |
| `test_model_allowlist.py` | Only `LLM_MODEL` + `LLM_ALLOWED_MODELS` can be saved (422 otherwise), the assistant calls the chosen model, a stored model that is no longer allowed falls back to the default |
| `test_db_limits.py` | Statement, lock and idle-transaction timeouts on every connection and checked against the AI turn budget; a lock timeout becomes a retry (no lost message); an idle transaction is ended and the pool recovers; a graceful shutdown hands unfinished events back at once |
| `test_whatsapp_window.py` | The 24-hour window counts only the customer's own messages; a customer silent for 25 h is not messaged (never reaches the adapter), the message is marked `outside_24h_window`, the conversation flagged and the owner alerted once per silence; the 23.5 h margin; late Meta failures (131047 and others) recorded and alerted once; failed owner alerts visible; another shop cannot touch these rows |
| `test_order_reminders.py` | One owner reminder per order for pending review and for accepted-but-unpaid, never repeated; paid, cancelled and delivered orders get none; no stock, status or customer change; each shop hears only about its own orders; the worker runs the sweep |
| `test_rate_limit_visibility.py` | The per-customer limit (30 a minute) is unchanged and still unanswered beyond it, but no longer silent: the message is kept and marked, the conversation flagged and the owner alerted once per conversation per day; each shop has its own limit and alerts; normal conversations untouched; a redelivered webhook counts once |
| `test_turn_deadline.py` | The AI turn budget covers tools too: a slow tool stretches a turn by at most itself and the skipped calls are recorded; when time runs out the customer gets the facts the tools returned (or the fallback); model, summary and embedding requests get only the time left; embeddings back off between retries; when embeddings are unavailable searches find the same products by words and the customer never sees a technical error |
| `test_ai_guard.py` | Runaway Conversation Guard counters (observe mode): every metered model call and every provider HTTP attempt is reserved before it is made, per message (the webhook event, the same row across retries), customer and tenant in fixed UTC hour/day buckets; replies and calls are identical with the guard off; reservations survive a rolled-back turn and lose no update under 8 concurrent workers; tenants are counted apart; a counter-store or ledger failure blocks nothing; unmetered providers reserve nothing; retention and metrics; invalid settings refused |
| `test_ai_guard_enforce.py` | Runaway Conversation Guard enforcement: exactly N reservations then a refusal, also for provider attempts and for 8 racing workers; a failing message can no longer repeat its model calls on every retry (6 instead of 25); refused adapter retries are not sent; a limited turn sends the facts already found or a reply that claims nothing, flags the conversation and alerts the owner at most once per window; counters unavailable = no AI call (fail closed); orders, payments, owner updates and a person stay reachable; requeue gives a fresh budget; switching back to observe keeps the history; per-customer hour/day limits are exact under racing turns, survive a restart, reset on the UTC hour (the daily one does not) and never limit another customer |
| `test_migration_0011.py` | Migration 0011 adds and removes only the counters; the usage ledger is untouched both ways |
| `test_usage_metering.py` | Every real AI model call (reply turns and conversation summaries) is one `usage_events` row, committed on its own: it survives the rollback of its turn and the deletion of the conversation, and a retried turn is a new call; served and configured model, tokens, provider retries as attempts, errors; a repeated key or concurrent writers store one row; costs from `USAGE_PRICING_FILE` in exact decimals (served model first, exact before longest prefix, unlisted = unpriced, version kept), a broken price list stops the start; the rules engine, evaluation runs and `llm-check` record nothing; rows can only be inserted (UPDATE and DELETE are refused, and a shop with usage cannot be deleted) |
| `test_whatsapp_metering.py` | WhatsApp traffic in the same ledger: one `wa_in` per stored inbound customer message (duplicates and rolled-back turns once; reactions and system notices not counted; real only with a verified webhook signature), one `wa_out`/`wa_alert` per send attempt under the claim's attempt number (retry = two rows, permanent failure = one failed row, concurrent workers = one, interrupted = `unknown` under the same key, nothing recorded when nothing was attempted), real vs simulated, template name only when known, market = calling code (never a number), late failures as a new `units` 0 row; WhatsApp price rules (no prices shipped; unpriced when not knowable), tenant isolation and the ledger's CHECKs and insert-only rule |
| `test_migration_0010.py` | On scratch databases: 0010 keeps AI rows untouched, the ledger stays insert-only, downgrade is refused while WhatsApp usage exists and works (and can be redone) when there is none, models match the migrated schema (`alembic check`) |

---

Dashboard unit tests (the API client: a double click never sends a write twice; the `/api` proxy reaches the API at
every `BACKEND_URL` form, including Render's private `host:port`) and type check:
```bash
cd frontend && npm test && npm run lint
```

Dashboard checks in a real browser (needs the Docker stack running and seeded): walk-through, live refresh, double
submits, and the API going down and coming back (`outage.js` stops and restarts the backend container):
```bash
cd scripts/ui-smoke && npm install && npm run setup && npm run smoke
```

CI (`.github/workflows/ci.yml`) runs all of the above on every pull request and push to `main`: ruff, the backend
suite on PostgreSQL 16 + pgvector, the offline evals, the dashboard type check, unit tests and production build,
the browser checks against the Docker stack, a dependency audit (`pip-audit`; `npm audit` fails on critical), and a
scan of the production image (no tests/evals/seed inside; Trivy fails on fixable critical vulnerabilities).

Agent evaluation (versioned cases, reports with transcripts):
```bash
cd backend && python -m evals.run --provider rules        # or adversarial; openai_compat with a real key
```

## Onboarding a new business (no code)
1. On the server: `python -m app.cli create-business --name "Shop" --email owner@shop.rw` (prints a generated
   password once). In development, `POST /api/auth/register` / the Register page also works; **public registration
   is always closed when `APP_ENV=production`** (and can be closed elsewhere with `ALLOW_PUBLIC_REGISTRATION=false`).
2. **Business & AI** sets the profile, hours (e.g. `Mon-Sat: 08:00-20:00`, `Sun: closed` — after hours customers
   are told when the team is back), currency, tone, greeting, business rules and toggles (delivery / payment /
   human handoff). **Settings** has the switch to pause the AI for the whole shop.
3. **Products** takes a CSV upload (`name,description,price,category,sku,stock_quantity`). Errors are reported per row and column.
4. **Settings** holds delivery zones (fee + the area names matched against the customer's address — list them
   well, an address naming no area is refused), how customers pay (manual by default, with the exact payment
   instructions the assistant shares) and the owner's WhatsApp number for new-order / handoff alerts.
5. **Knowledge** takes FAQs and policies as text, PDF, TXT or MD. They're chunked and embedded into pgvector.
6. **WhatsApp** connects the Cloud API `phone_number_id` + access token. The token is Fernet-encrypted at rest.

`backend/seed/seed.py` onboards three businesses this way. Each one is just a dict of config.

## Credentials: what's needed to go live
Everything below works in dev mode without credentials. The real integrations are fully implemented but have
**not** been exercised against the live services from this environment:

| Integration | Status | Where to put it |
|---|---|---|
| WhatsApp Cloud API (send + receive) | **BLOCKED BY EXTERNAL CREDENTIAL**. The code is implemented and tested against Meta's documented request/response shapes. | `.env`: `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_APP_SECRET`. Dashboard → WhatsApp: phone_number_id + permanent token (mode `cloud`). Meta webhook URL: `https://<backend>/webhooks/whatsapp`, field `messages`. |
| LLM (real language understanding) | **BLOCKED BY EXTERNAL CREDENTIAL**. Implemented for any OpenAI-compatible API with turn budget, bounded retries and a grounding check on every reply; tested with stubbed endpoints and simulated misbehaving models. Verify with `python -m app.cli llm-check`. | `LLM_PROVIDER=openai_compat`, `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL` (e.g. Gemini Flash / gpt-4o-mini / Groq Llama) |
| MTN MoMo Collections | **BLOCKED BY EXTERNAL CREDENTIAL**. Request-to-Pay, status polling and callback re-verification are implemented. | `MOMO_SUBSCRIPTION_KEY`, `MOMO_API_USER`, `MOMO_API_KEY`, `MOMO_CALLBACK_HOST`, `MOMO_TARGET_ENVIRONMENT` (sandbox: `MOMO_CURRENCY_OVERRIDE=EUR`). Then Settings → provider = MTN MoMo. |
| Semantic embeddings | Optional. The default `hash` embedder is free and offline (lexical). | `EMBEDDING_PROVIDER=openai_compat`, `EMBEDDING_API_KEY` (re-embed the catalog by re-importing it) |

> **About `LLM_PROVIDER=rules`:** this is a deterministic intent router, **not AI**. It drives the *same* tools, so
> the whole commerce pipeline can be run and tested at zero cost. It only understands common English commerce
> phrasing. Production should use `openai_compat`.

## Design decisions that keep cost low and answers correct
- **The LLM never produces facts.** Prices, stock, totals, delivery fees and order/payment status come from tools,
  then services, then the DB. Tools validate their arguments with Pydantic and run in a SAVEPOINT. Errors go back
  to the model as `ok:false`.
- **Model replies are checked against the tools.** Every number, price, order number, order/payment status and
  availability claim in an LLM reply must match this turn's tool results; otherwise the customer gets the server's
  own rendering of those results. Two unanswerable turns in a row hand the chat to a person.
- **Deterministic first, LLM when needed.** Totals, stock, delivery quotes, order status and payment confirmation
  are plain code. Bare greetings skip the LLM entirely (`fast_path`).
- **Small context.** The prompt has the tenant's prompt (~250 tokens), a one-line state snapshot (last products
  shown, cart, unpaid order), a rolling summary that's only built for long chats, and the last 8 messages (truncated).
  Tool results are only kept for the current turn. "Add the second one" works because the last product list is
  stored in `conversations.state`, so the full history doesn't have to be re-sent.
- **Orders need an explicit YES.** The model can only prepare a checkout; the server sends the exact summary
  (items, prices, delivery fee, total, address) and places the order only when the customer's next message
  confirms it — and only if nothing changed meanwhile. No delivery zone or address is ever assumed.
- **Payments are confirmed by the provider or by the owner, never by the AI.** Manual payments (the default) are
  recorded by the owner with evidence and audited; a customer's "I paid, ref X" is stored as pending for the owner
  to check. The mock provider never auto-confirms and is refused in production. MoMo callbacks are re-verified
  against the MoMo API.
- **Speaks the customer's language.** Each conversation keeps a detected language (English, Kinyarwanda, French,
  Swahili, Arabic, Sudanese Arabic); it changes only on a confident switch, the assistant is told to answer in it
  (Sudanese stays Sudanese), and every automatic message — summaries, confirmations, status and payment messages,
  handoff — uses it, with prices and names unchanged.
- **The owner stays in control.** New orders wait for the owner's review; the owner is alerted on WhatsApp; a
  customer can ask for a person in English, Kinyarwanda, French or Swahili; voice notes go to a person; a takeover
  pauses the AI until the owner explicitly returns the conversation to it.
- **Reliability.** A webhook is acknowledged only after its messages are committed to `webhook_events`; a crash
  after that loses nothing (the worker lease expires and another worker takes over). Each message is processed in
  one transaction together with its reply, which is sent only after commit (outbox), so a customer never hears
  about an order that was rolled back. Duplicate `wamid`s are ignored at ingest and at insert, a customer's
  messages are processed strictly in order, failures retry with backoff and are dead-lettered after
  `WEBHOOK_MAX_ATTEMPTS`. Sends retry on 429/5xx/timeouts, never on 4xx. If the LLM fails, the customer gets the
  tenant's fallback message.
- **Operations.** `/healthz`, `/readyz` (503 when customers are affected), `/metrics`, backup / verified-restore
  scripts and a runbook: see `docs/OPERATIONS.md`.
- **Observability.** Logs are JSON with `request_id, business_id, customer_id, conversation_id, operation, status,
  duration_ms`, and secrets are redacted. `agent_runs` stores every decision, tool, argument, result, error,
  latency and token count. This feeds the dashboard's conversation debugger.

## API (summary; full contract at `/docs`)
```
POST /api/auth/register | /api/auth/login        GET /api/auth/me
GET|PATCH /api/business   GET|PATCH /api/business/agent-config   GET|PATCH /api/business/settings
GET|POST /api/delivery-zones   PATCH|DELETE /api/delivery-zones/{id}
GET|POST /api/whatsapp/accounts   DELETE /api/whatsapp/accounts/{id}
GET|POST /api/products   GET|PATCH|DELETE /api/products/{id}   POST /api/products/import (multipart CSV)
POST /api/products/{id}/stock   GET /api/products/{id}/inventory   GET /api/products/search   GET /api/categories
GET /api/orders[?status=]   GET|PATCH /api/orders/{id}   POST /api/orders/{id}/payments (owner, manual)
POST /api/payments/{id}/void (owner)   POST /api/payments/{id}/refresh   POST /api/payments/{id}/simulate (dev)
GET /api/customers   GET /api/customers/{id}
GET /api/conversations[?needs_attention=]   GET /api/conversations/{id} (messages + agent runs)
POST /api/conversations/{id}/reply | /handoff | /return-to-ai
GET|POST /api/knowledge   POST /api/knowledge/upload   GET /api/knowledge/search   DELETE /api/knowledge/{id}
GET /api/dashboard/stats | usage | notifications | audit      POST /api/dev/simulate (dev)
GET|POST /webhooks/whatsapp   POST /webhooks/payments/mock   PUT|POST /webhooks/payments/momo/{payment_id}
GET /healthz | /readyz | /metrics (ops token) | /health
```

## Verification status (honest)
- ✅ 696 backend tests pass against PostgreSQL 16 + pgvector (CI runs
  the suite on every push to `main`). Ruff is clean.
- ✅ Migrations go up and down cleanly from an empty DB. `alembic check` reports the models are in sync.
- ✅ The backend and the production Next.js build ran locally. Playwright drove the dashboard: login, the simulator
  flow, the debugger trace, simulating payment → paid, the CSV error report, and the mobile layout (no horizontal
  scroll). There were no console errors.
- ✅ `docker compose up --build` (development) and the production compose stack (local HTTPS rehearsal,
  `deploy/verify_deployment.sh` 24/24) both run. Status per milestone: `docs/MILESTONES.md`.
- ⚠️ Live WhatsApp, LLM and MoMo are blocked by external credentials (see the table above).

## Security notes
- bcrypt password hashing and HS256 JWTs (tenant + user re-checked against the DB on every request).
- Tenant scoping lives in the repository layer, and a spoofed `business_id` is ignored. Underneath, database
  triggers reject any row that references another tenant's row and make `business_id` immutable.
- Meta signature verification (required in production). The mock payment callback uses HMAC, and MoMo statuses
  are re-verified.
- WhatsApp tokens are encrypted at rest (`ENCRYPTION_KEY` is required in production) and never returned by the API.
- Production refuses to start without `JWT_SECRET`, and turns off the simulator and the mock callbacks.
- All queries go through the ORM or bound parameters. Validation uses Pydantic and size limits apply to CSV and
  uploads. Inbound messages are rate limited per customer, in process.
- Sign-in is throttled per client address and per account (growing lockout after 5 wrong passwords, unknown emails
  treated the same). The client address comes from `TRUSTED_PROXY_HOPS`, never from what a client writes in
  `X-Forwarded-For`.
- The app itself sends security headers (nosniff, frame denial, CSP, HSTS in production) and refuses request bodies
  over 10 MB, so this holds without a reverse proxy.
- Payment instructions, prices, WhatsApp numbers, passwords and AI settings changes go to the append-only audit
  table (never a secret). Businesses can only pick models the platform allows (`LLM_ALLOWED_MODELS`).
- Every database connection has statement, lock and idle-transaction timeouts sized around the AI turn budget.
- The frontend proxies `/api` on the server side. No API keys ever reach the browser.

## Future work (deliberately out of V1)
- Staff invitations and roles UI. The `staff` role exists, but there's no invite flow yet. Forgotten passwords are
  reset by an operator: `python -m app.cli reset-password --email owner@shop.rw`.
- A shared (e.g. Redis-backed) rate limiter before running more than one API instance: today's limiters are
  in-process. Background work does not use `BackgroundTasks`: the webhook inbox and the outbox are PostgreSQL tables
  that in-process worker threads claim with `FOR UPDATE SKIP LOCKED`. A multi-instance deployment has not been
  validated.
- Postgres row-level security as defence in depth on top of repository scoping.
- WhatsApp interactive messages (lists/buttons, product images), template messages outside the 24h window, voice notes.
- Per-tenant MoMo credentials (today they're platform-level env vars), more providers (Airtel Money, Flutterwave,
  Stripe), refunds.
- Unpaid orders expiring and releasing stock, discounts/coupons, multi-language replies for the rules engine.
- A platform super-admin console and billing. Per-tenant usage metering already exists: real AI calls and WhatsApp
  traffic are recorded in `usage_events` (`docs/OPERATIONS.md` › Usage metering). Monthly usage reports, a tenant
  cost model, quotas and spend alerts, a usage dashboard and billing are proposed Phase 4 slices awaiting product
  approval (`docs/MILESTONES.md`).
- Auth cookies (httpOnly) instead of localStorage for the dashboard token.
