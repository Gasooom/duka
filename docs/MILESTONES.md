# Duka — Milestone Status

Last updated: 2026-10-10 (adds production hardening phases 1–3, Render preparation and Phase 4 P0 usage metering;
M3's key status reconciled with `docs/VALIDATION_REPORT.md`; the remaining Phase 4 slices are recorded as a proposal).
Previous update: 2026-10-04 (M2, M4–M8, M10 complete; M3 and M9 ready but blocked on external accounts).

Status is based on code, tests and a running stack — not on README claims.
Legend: **COMPLETE** · **IN PROGRESS** · **BLOCKED** (needs an external dependency) · **NOT STARTED**

> CLAUDE.md does not define a milestone list. The production milestones below (M1–M12) come from
> the production-MVP brief (phases 0–12). The foundation work already in Git history is listed first.

## Evidence gathered in this audit

| Check | Command | Result |
|---|---|---|
| Docker stack | `docker compose up -d --build` | db, backend and frontend all build and start; backend becomes healthy (first time `compose up` has actually been run — README said it was not) |
| Backend tests | `docker compose exec -e TEST_DATABASE_URL=…/commerce_test backend pytest -q` | **65 passed**, 2 warnings, 50 s |
| Lint | `docker compose exec backend ruff check app tests seed` | All checks passed |
| Migrations vs models | `docker compose exec backend alembic check` | No new upgrade operations detected |
| Frontend build | `next build` inside the frontend Dockerfile | Succeeded (type-checked) |
| Health | `GET /health` → 200, `GET /healthz` → **404** | |
| Live demo (simulator, rules provider) | 9 messages via `/api/dev/simulate` | English happy path works; failures listed under M3/M5/M6 |

## Foundation (already built, from Git history)

| # | Commit | Area | Status | Evidence / caveat |
|---|---|---|---|---|
| F1 | `77510d6` | Multi-tenant core (repos, JWT tenant, `business_id NOT NULL`) | COMPLETE | Proven in M2. |
| F2 | `8d7232c` | Catalog, CSV import, inventory ledger, hybrid search | COMPLETE (English) | `test_products.py`. Search tokenizer is ASCII-only `[a-z0-9]+`; French query "baskets noires" returned boots/jeans. |
| F3 | `6ed050b` | Customers, conversations, idempotent inbound insert | COMPLETE (sequential) | Duplicate `wamid` test passes. No unique constraint on active conversation or active cart → concurrent first messages can create duplicates (not yet proven by test). |
| F4 | `9a76aae` | Agent engine + 18 tools, provider abstraction | COMPLETE (framework) | `test_agent.py`. Default provider is `rules` (not AI). |
| F5 | `cc21723` | WhatsApp webhook, signature check, Cloud + dev adapters | IN PROGRESS | Tested against mocked HTTP only; never exercised against Meta. |
| F6 | `d3e4956` | Cart, checkout, order snapshots, mock + MoMo payments | IN PROGRESS | Correct totals and stock locking; gaps under M5. |
| F7 | `612f14e` | Knowledge RAG (pgvector) | COMPLETE (lexical) | Default `hash` embedder is lexical, not semantic. |
| F8 | `dcefb1e`, `ebe2c08` | E2E test, dashboard, simulator | IN PROGRESS | Dashboard works; gaps under M7. |

## Production MVP milestones

### M1 — Phase 0 audit · COMPLETE
This document.

### M2 — Multi-tenant security proven · COMPLETE
Commit: `test(security): prove tenant isolation ...` (see `git log`).

What proves it (`backend/tests/test_tenant_isolation.py`, 21 tests):
- **IDOR matrix over every `/api` route with an id** (18 method/path pairs). Store A calls each route with Store B's
  real ids (product, order, payment, customer, conversation, delivery zone, WhatsApp account, knowledge doc)
  → 404, and the response is byte-identical to the one for a random UUID (no existence oracle).
  `test_idor_matrix_covers_every_id_route` enumerates the OpenAPI schema and **fails when a new id route is added
  without a matrix entry**.
- **Full before/after snapshot of Store B** (business, agent config, settings, zones, WhatsApp accounts, products,
  categories, inventory ledgers, orders + payments, customers, conversations + messages + agent runs, knowledge,
  stats, usage) is unchanged after all attacks, and no WhatsApp message was sent to B's customer.
  B's conversation is in human mode during the attack so `reply` is only blocked by ownership.
- Lists, searches and aggregates (`stats`, new `usage`) show only own rows; text search by B's product name,
  customer number or knowledge content returns nothing.
- Singletons (business profile, agent config, settings, default delivery zone) changed by A leave B untouched.
- CSV import with B's SKU creates A's own product and does not update B's.
- Webhooks: traffic on A's number writes zero rows to any B table (counted per tenant table); a status webhook on
  A's number carrying B's `wamid` cannot change B's message.
- Customer chat: a customer of A asking for B's order number, SKU, policy text or to pay B's order gets nothing of B's.
- Agent tools: 10 tools called with B's product id/SKU/name and order number all fail or return empty.
- **Database level** (new migration `0003`): a trigger on every tenant→tenant foreign key rejects a row that
  references another tenant's row, and `business_id` is immutable on all 19 tenant tables. Tested by writing
  cross-tenant cart items, messages and carts through the repositories (simulating a buggy service) → rejected.
  `test_every_tenant_foreign_key_is_guarded_in_the_database` **fails when a new tenant FK lacks a trigger**.
  The migration refuses to install over data that already has cross-tenant links.
- Identity: forged signature, valid signature with mismatched business, `alg=none`, expired token, deactivated
  user, deactivated business. Staff cannot change business/agent/settings/zones/WhatsApp.
- **Concurrency**: 8 customers (4 per store) shop and check out simultaneously; each store gets exactly
  `PREFIX-00001..00004`, no order item references another tenant's product, no reply contains the other store's products.

Fixed during M2:
- **Checkout deadlock (pre-existing, production-impacting):** concurrent checkouts in one store failed with
  "Internal error" — the order-number lock (`FOR UPDATE` on the business row) deadlocked against the
  `FOR KEY SHARE` locks every insert takes through its `business_id` FK. Reproduced (`DeadlockDetected`),
  fixed with `FOR NO KEY UPDATE`, covered by the concurrency test.
- **Public registration closed in production** (always) and closable elsewhere (`ALLOW_PUBLIC_REGISTRATION=false`).
  Tenants are created with `python -m app.cli create-business` on the server (generates a password if omitted).
- Added `GET /api/dashboard/usage` (agent runs, LLM calls, tokens, errors, latency, messages in/out) so usage is
  visible and covered by the isolation tests.

Accepted, documented inference channels (not tenant data): connecting a WhatsApp `phone_number_id` that another
business uses returns 409 (the id is Meta-issued and ownership is proven by its token); user emails are globally
unique (registration is closed in production; login gives the same error for unknown email and wrong password).

Remaining risks: isolation is enforced by repositories + DB triggers, not Postgres RLS (reads are not DB-enforced).
Rate limits are per process.

### M3 — Real AI · code COMPLETE · live verification BLOCKED (production LLM credential)

Not COMPLETE until a real provider has been called successfully: run `python -m app.cli llm-check` with
`LLM_PROVIDER=openai_compat`, `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` (exit 0 = the model called the tool).
At this audit (2026-10-04) it reported `key=MISSING` / exit 2.

Reconciled 2026-10-10: during the engineering validation (2026-10-05/06) `llm-check` passed on a developer machine
with a local key: `provider=openai_compat model=gpt-4o-mini key=set ok`, tool call `search_products`
(`docs/VALIDATION_REPORT.md` §2, which also records the real-model eval and live scenarios). That proves the adapter
against the real API; it is not the production credential. M3 stays BLOCKED until `llm-check` passes on the
production host with the production account and a pinned, dated model, and the real-model evaluation in the pilot
languages has been reviewed (`docs/EXTERNAL_VALIDATION.md` §3).

Production LLM path (existing `openai_compat` adapter, works with OpenAI, Gemini, Groq, OpenRouter, DeepSeek):
- **Time budget:** a whole turn is capped (`AGENT_TURN_TIMEOUT_SECONDS`, 45 s). Each HTTP attempt's timeout is
  capped by what is left; retries are bounded (`LLM_MAX_ATTEMPTS`) and only on 408/409/429/5xx/network,
  honouring `Retry-After` when it fits. Previously a turn could take ~7 minutes inside a DB transaction.
- **Malformed output** (non-JSON, no choices, content parts, non-object/invalid tool args) is handled; tool
  argument models now reject unknown fields, so `{"__invalid_json__": ...}` or invented arguments are an error the
  model must fix instead of being silently ignored. Tool calls per turn are capped (`AGENT_MAX_TOOL_CALLS`).
- **Missing/invalid key behaves like an outage** (fallback + error recorded), not a crashed turn that would be
  dead-lettered without a reply; production refuses to start without a real LLM configured.
- **No fabricated commerce facts** — layered, not a single regex:
  1. the highest-stakes texts are never model-written (summary/confirmation, payment and status messages, M5),
     and no tool can change prices, orders or payments;
  2. `agents/grounding.py` checks every final reply against a typed ledger of this turn's tool results and server
     state: money must equal a tool money value and, next to a single product, that product's own price; order
     numbers and order/payment status claims must come from tools; "order placed" needs an order fact;
     availability needs returned products, must name one of them (unless exactly one was returned) and must not
     contradict stock; cart-change claims need a cart tool; any other number must come from tool data or the
     customer's own words (their budget may be repeated, a price they suggest may not); prompt-leak markers;
  3. on any violation the customer gets the deterministic server rendering of the same tool results (or a
     clarifying question if there are none), and the violation is recorded in the agent run for the debugger.
- **Handoff on uncertainty:** two consecutive turns that fail (LLM error, or nothing verifiable to say) hand the
  conversation to a person with an owner alert; a good turn resets the counter.
- **Prompt injection:** customer text and tool results are declared data in the prompt, but the guarantees do
  not depend on the model obeying: tests simulate a fully compromised model (calls non-existent `update_price` /
  `mark_paid`, claims RWF 1 and "paid", obeys an injected "50% off" from a knowledge document, leaks its rules,
  asks for another store's product) and the price, payment status and replies stay correct.
- **PII minimisation:** the LLM vendor no longer receives the customer's phone number (first name only); the
  payer phone in tool results is masked (`***222`). Asserted on the actual HTTP payload.
- **Multilingual:** the prompt asks for replies in the customer's language (EN/RW/FR/SW, mixed); search tool
  arguments ask the model to translate queries into the catalog's language; grounding works identically for
  Kinyarwanda, French and mixed replies (tested). Real multilingual *quality* is NOT claimed: it needs the real
  model and native-speaker review (M10).

Evidence: `tests/test_ai_safety.py` (33 cases): invented price / wrong product's price / stock / delivery fee /
payment status / order status / fake order number / fake product (alone and next to real results) / claimed cart
change / claimed order placement / unverifiable answer without tools; 4 prompt-injection cases; cross-tenant
request; PII on the wire; retry budget, Retry-After, no retry on 4xx; 3 malformed outputs; invalid tool args;
slow-model cutoff; tool-call cap; repeated failures -> handoff; streak reset; 3 multilingual grounding cases;
grounding unit cases. `test_hardening.py`: production refuses unsafe config; missing key = outage.
`pytest -q` -> 195 passed; ruff clean. Live: `APP_ENV=production` with the dev `.env` refuses to start and lists
all six problems.

Remaining risks:
- No real model has been called. Model quality, false-positive rate of the grounding check (safe but less
  natural replies) and multilingual quality must be measured with the real key (M10).
- A product name invented *alongside* real ones without any price/stock/availability claim is not detected.
- Server-rendered messages (summary, statuses) are English-only.

### M4 — Durable WhatsApp processing · COMPLETE (code) · live Meta verification BLOCKED (Meta app, number, token, public HTTPS URL)

Architecture (PostgreSQL only — no Redis/queue added):
- `POST /webhooks/whatsapp` verifies the signature, then writes one `webhook_events` row per message
  (unique on business + wamid, tenant resolved from `phone_number_id`) and **commits before returning 200**.
  If persisting fails the endpoint returns 5xx so Meta redelivers. `BackgroundTasks` is no longer used.
- In-process worker threads (`BACKGROUND_WORKERS`, default 2) claim events with `FOR UPDATE SKIP LOCKED`
  (safe across threads and instances), oldest first, **one at a time per customer** (a later message never
  overtakes an unfinished earlier one), under a lease (`WEBHOOK_LEASE_SECONDS`) so a crashed worker's event is
  reclaimed. Failures go to `retry` with backoff (2s, 10s, 30s, 120s), then `dead` after `WEBHOOK_MAX_ATTEMPTS`
  (logged at ERROR; later messages from that customer are then unblocked).
- One transaction per event: customer, conversation, inbound message, agent run, cart/order changes, the
  **queued reply** and `status=done`. **Replies are sent only after that commit** (transactional outbox in
  `messages`: queued -> sending -> sent/simulated, or retry -> failed). Human replies and payment notifications
  use the same outbox. A transient send failure (429/5xx/network) is retried by the worker with backoff; a
  permanent one is marked failed and flags the conversation. A send interrupted mid-request (outcome unknown) is
  flagged, not blindly re-sent (WhatsApp has no idempotency key).
- At most one open conversation and one active cart per customer (partial unique indexes + race-safe inserts).
- Status webhooks never move a message backwards (read stays read).

Evidence:
- `tests/test_durability.py` (17 tests): persisted-before-ack + recovery after a crash; ingest failure -> 500 ->
  redelivery works; failure after the agent created an order -> order, message and reply all rolled back, retried
  once -> exactly one order and one reply; commit failure -> nothing sent; expired lease reclaimed; 6 concurrent +
  2 later redeliveries of one wamid -> one event, one message, one customer, one conversation, one reply; retry ->
  dead-letter with per-customer ordering; parallel senders vs serialized same sender; transient/permanent send
  failures; committed-but-unsent reply delivered by the worker exactly once; interrupted send not re-sent;
  6 concurrent first contacts -> one customer/conversation/cart; real background threads process a webhook;
  monotonic statuses; human reply reports real delivery status; a rolled-back queued message is never sent.
- **Live cross-process crash test** on the Docker stack: an ingest-only backend (`BACKGROUND_WORKERS=0`)
  acknowledged a webhook and was `SIGKILL`ed; the event was `pending` in Postgres; the restarted main backend's
  workers processed it (`done`, 1 attempt, `replied`) and recorded the reply.

Fixed during M4:
- **Security: `.env.example` inline comments became secret values under docker compose.**
  `WHATSAPP_APP_SECRET=   # (CREDENTIAL)...` was parsed as the value `"# (CREDENTIAL)..."` — a publicly known HMAC
  secret if left blank on a deployment (also `MOMO_CALLBACK_HOST` and others). Comments moved to their own lines,
  and the app now refuses to start if any setting's value starts with `#` (regression test).

Remaining risks / not in scope:
- Not yet exercised against Meta (needs the Meta app, number, permanent token and a public HTTPS URL).
- The real MoMo `request_payment` call (not enabled) would run inside the processing transaction; before
  enabling MoMo it must move behind the outbox pattern too.
- `webhook_events.payload` holds message text (PII); a retention/purge job is planned in M8.
- 24-hour customer-service window / template messages are not handled (owner replies after 24 h fail at Meta;
  they show as `failed` and flag the conversation).
- Voice/media still get the "text only" reply (M6 routes them to a human).

### M5 — Safe orders + human control · COMPLETE

Order lifecycle (enforced server-side, the LLM cannot bypass it):
`conversation -> cart -> prepare_checkout (needs a real address in a delivery zone; nothing assumed) ->
server-rendered summary sent verbatim -> customer's NEXT message is an explicit YES -> order 'pending'
(confirmation message id stored as evidence) -> owner notified -> owner accepts/rejects -> payment ->
ready / out_for_delivery / delivered`.
- The model has **no tool that creates orders or confirms payments** (`create_order` removed; test asserts no
  such tool exists). `prepare_checkout` only fingerprints the cart and returns the summary.
- The order is created by a deterministic step before any LLM call, only if: a summary is pending, it was
  actually delivered (`sent/delivered/read/simulated`) before the YES, it is < 30 min old, and the cart, prices,
  stock, zone and address are unchanged (fingerprint). Otherwise a fresh summary is sent. The YES classifier is
  strict and multilingual (EN/RW/FR/SW + 👍); "yes but…" is not a confirmation.
- No assumed delivery zone anywhere: totals without a location say "Total before delivery".
- Fulfilment status and **payment status are separate** (`pending -> accepted -> ready -> out_for_delivery ->
  delivered | cancelled` × `unpaid | pending | paid`) so an owner can accept a cash-on-delivery order and record
  payment later. Existing rows were migrated.
- **Manual payments** (default provider for new businesses): the owner records method + evidence (MoMo/bank
  transaction id required, cash needs a note), stored with `confirmation_source='owner'`, `confirmed_by_user_id`,
  `confirmed_at`; provider confirmations are `confirmation_source='provider'`. The same reference cannot settle
  two orders of a store (unique index). Mistakes are voided with a reason, never deleted. Customers can send a
  transaction id (agent tool) — recorded as *pending* for the owner to verify; it can never mark an order paid.
  Owner-only API (staff get 403). The mock provider is refused in production; MoMo only when configured.
- **Audit trail** (`audit_events`, UPDATE rejected by a DB trigger): customer confirmation, every status change
  (with reason), reported references, manual payments, voids, takeovers, returns to AI, money received for a
  cancelled order.
- **Owner notifications** (`notifications`, same outbox discipline): new order, handoff, reported payment, sent
  to the configured owner WhatsApp number (optional approved template for outside the 24 h window); recorded as
  `skipped` when no number is set. Listed at `GET /api/dashboard/notifications`.
- Customer is told on WhatsApp when the owner accepts (with the shop's payment instructions), rejects (with the
  reason; refund note if paid), dispatches and delivers.

Human control:
- "Talk to a person" is detected deterministically before the LLM in English, Kinyarwanda, French and Swahili
  (request phrasing, not nouns: "human hair wigs" stays a product search) -> handoff + owner alert.
- Voice notes, photos, videos, documents, locations go to a person (no speech-to-text/vision is faked);
  reactions are ignored.
- Owner takeover pauses the AI completely (no agent run at all); customer messages stay visible and flagged;
  staff replies go through the outbox. **Only an explicit "return to AI" resumes the assistant** (optional
  message to the customer). A handoff discards any pending summary so the customer re-confirms afterwards.

Evidence:
- `tests/test_orders_handoff.py` (52 cases incl. parametrized), `tests/test_commerce.py`, `tests/test_payments.py`,
  `tests/test_e2e.py` (full journey: no assumed zone -> address -> summary -> YES -> owner alert -> accept with
  payment instructions -> customer-reported reference -> owner records it -> dispatched -> delivered).
  Misbehaving-LLM test: the model calls a non-existent `create_order`, then claims "placed and paid, total RWF 1"
  -> the customer receives the server summary, no order exists; the later YES places it without an LLM call.
- `pytest -q` -> 162 passed; ruff clean; migration 0005 down/up/check clean.
- **Live on the Docker stack** (simulator, rules provider): "Place the order." -> asks for the address;
  address -> exact summary; "yes" -> `KF-00003` pending/unpaid with confirmation evidence; owner notification
  delivered (dev adapter); owner accepts; customer reports `MPLIVE…` -> payment pending; owner records it ->
  paid, `confirmation_source=owner`, 4 audit events; "Nshaka kuvugana n'umuntu" -> human mode; next message ->
  AI silent; staff reply sent; return to AI -> assistant answers again.

Remaining risks:
- The summary and status messages are English-only (the YES classifier is multilingual). Localised templates
  are part of the AI-quality work (M3/M10).
- Delivery zones match on area names in the address text; an address naming no configured area is refused
  (safe, but the owner must list areas well).
- Owner alerts outside WhatsApp's 24 h window need an approved Meta template (configurable, not live-tested).
- Pickup when delivery is enabled is not offered as a customer choice yet.

### M6 — Human control · COMPLETE (live WhatsApp delivery shares the M4 Meta blocker)

Rules (all explicit, tested):
- Customer asks for a person (EN/RW/FR/SW, request phrasing) -> handoff + owner alert, before any LLM call (M5).
- Low confidence: two consecutive turns the assistant cannot answer reliably (LLM failure, or a reply that fails
  the grounding check with no tool data to fall back on) -> handoff + owner alert (M3).
- Unsupported requests: the model is told to hand off; anything it says is grounding-checked (M3).
- Voice notes / photos / videos / documents / locations -> a person (no speech-to-text or vision is faked) (M5).
- Owner takeover: the AI is fully paused for that conversation (no agent run at all, no order confirmation),
  messages stay visible and flagged, staff replies go through the outbox, and **only "Return to AI" resumes**
  (optional message to the customer; audited; a pending order summary is discarded) (M5).
- **After hours** (new): business hours are parsed (`Mon-Sat: 08:00-20:00`, `Sun: closed`, `Daily: 7am-9pm`,
  split shifts, overnight `18:00-02:00`, `24h`) in the business timezone; unreadable hours or an unknown timezone
  are rejected on save. The assistant keeps answering 24/7, but handoffs, voice notes and new orders tell the
  customer when the team is back ("closed right now… when we open (Mon at 08:00)") instead of "shortly".
  `open_now` / `next_opening` are tool facts, so "are you open?" answers are grounded.
- **Business-wide AI pause** (new, `Settings`): nothing automated happens (no answers, no orders); each waiting
  conversation gets one acknowledgement (with the opening time after hours) and the owner one alert.

Evidence: `tests/test_human_control.py` (18 cases: open/closed in Africa/Kigali, next opening across a closed
Sunday, 5 hour formats incl. overnight, rejected hours/timezone, after-hours handoff/voice note/order wording,
open_now tool fact, pause = no agent runs + one ack + one alert + messages kept + resume, pause blocks order
confirmation), plus the M5 handoff/takeover tests. `pytest -q` -> 213 passed; ruff clean; migration 0006
down/up/check clean. Live on Docker: invalid hours -> 422 with an explanation; pause -> one acknowledgement,
owner alert recorded, AI silent; resume -> the assistant answers again.

Remaining: the dashboard does not refresh by itself yet, so the owner sees new handoffs on reload (M7).

### M7 — Merchant dashboard · COMPLETE

A merchant can operate without developer or database access:
- **Inbox:** conversations (all / needs attention), customer, full message history incl. AI trace, AI/human
  status, take over, reply, return to AI (optional message). Refreshes every 10 s (list) / 5 s (open chat).
- **Orders:** list with fulfilment + payment status, details (customer, items, totals, address, confirmation
  evidence), accept / reject with reason, ready / out for delivery / delivered, record manual payment (MoMo ref,
  cash note, bank), void with reason, audited history. Refreshes every 15 s.
- **Products:** create, edit price/stock/details, deactivate, delete, CSV import with per-row errors (existing).
- **Knowledge:** add FAQ/policy text, upload PDF/TXT/MD, search, delete (existing).
- **Settings:** business profile and validated hours, delivery zones, WhatsApp numbers, AI on/paused, handoff
  on/off, how customers pay + the exact payment instructions, owner alert number + optional Meta template.
- **New in M7:** nav badges (orders to review, conversations needing a person) refreshed every 20 s; an "AI
  paused" banner; a **setup checklist** on the overview (real WhatsApp number connected, products, delivery
  zones, payment instructions, alert number, hours, AI on, and — honestly — whether the platform LLM is
  configured); an **alerts feed** showing whether each owner alert reached WhatsApp; an **Account** page to change
  the password (signs out other devices via a token version); operator CLI `reset-password` for forgotten
  passwords; the dev simulator is hidden when dev tools are off (production).

Evidence:
- `tests/test_dashboard_ops.py`: setup checklist reflects real state; change password (wrong current -> 403,
  too short -> 422, old sessions -> 401, new token works, old password rejected); operator reset signs out every
  session; dev-tools flag off in production. `pytest -q` -> 217 passed; ruff clean; migration 0007 clean.
- **Headless browser walk-through** (`scripts/ui-smoke`, Playwright/Chromium against the Docker stack), 20/20:
  login; checklist + honest platform-AI warning; alert for the new order; nav badges; order shows confirmation
  evidence; Accept; record a cash payment -> "Confirmed by the shop (manual record)" + audited history (API
  agrees: accepted, paid, confirmation_source=owner); inbox -> handed-off chat -> staff reply delivered; settings
  controls; account page; no horizontal scroll at 375 px on overview/orders/inbox/settings; zero console errors.
  Live refresh: an order placed on WhatsApp appeared on the open orders page after 15.1 s without a reload.

Remaining: no staff invitations/roles UI (single owner login per shop for the pilot); polling, not push
(fine at pilot scale); JWT in localStorage (httpOnly cookies are a later hardening step).

### M8 — Reliability · COMPLETE (in-repo) · external monitor + off-site backup storage are deployment dependencies (M9)

- **Probes:** `/healthz` (liveness, used by the Docker healthcheck) and `/readyz` (database, migrations at head,
  workers alive, inbound backlog, dead letters, failed sends, agent error rate, failed owner alerts) returning 503
  when customers are affected; details behind `OPS_TOKEN` in production. `/health` kept as an alias.
- **Metrics:** Prometheus text at `/metrics` (ops token in production): inbound queue by status and oldest age,
  outbox by status, agent runs by outcome (incl. `ungrounded`), p50/p95 latency, LLM tokens, orders, owner alerts.
  Derived from PostgreSQL, so they survive restarts and agree across instances.
- **Logs:** access log (method, path, status, duration; never query strings — Meta's verify token travels there);
  the JSON formatter scrubs SQL statements/parameters from database errors, bearer tokens, `access_token=`,
  and phone numbers on every log line; persisted error texts use the same scrubbing.
- **Fixed during M8:** `alembic/env.py` reconfigured logging on every in-process migration (root level WARN +
  all existing loggers disabled), silently removing application logs whenever migrations ran inside the process
  (the test suite; any future in-process migration). Now only the alembic CLI configures logging.
- **Retention:** processed webhook payloads (message text) are purged after 30 days by the workers.
- **Production hardening:** OpenAPI `/docs` disabled in production.
- **Backups:** `scripts/backup.sh` (consistent `pg_dump`, row-count manifest taken around the dump, sha256,
  retention), `scripts/restore.sh` (new database by default; `--replace-live` keeps the old database renamed),
  `scripts/verify_restore.sh` (restore into a scratch DB, compare every table with the manifest, run readiness
  and a real login on the restored copy, drop the scratch DB).
- **Operator tooling:** `python -m app.cli requeue-dead` (retry dead-lettered messages after a fix);
  runbook in `docs/OPERATIONS.md`.

Evidence:
- `tests/test_reliability.py` (13): liveness; readiness ok/degraded (dead letter)/down (stuck backlog, workers
  not running, migrations behind); metrics exposition; ops token required in production; secrets and phone
  numbers redacted; a real IntegrityError's customer number scrubbed; access log has no query string; money
  amounts not mistaken for phone numbers; retention purges only finished old events; requeue-dead recovers a
  dead-lettered message. `pytest -q` -> 230 passed; ruff clean.
- **Actual restore performed** on the Docker stack: `scripts/backup.sh` -> 152 KB dump at revision 0007;
  `scripts/verify_restore.sh` -> checksum ok, 24 tables' counts match the manifest, migrations at head, readiness
  ok, `fashion@duka.dev` logs in with its real password on the restored copy -> `RESTORE VERIFIED`; scratch DB
  dropped. Negative checks: a byte-flipped dump fails on checksum; a manifest/count mismatch fails verification.

Remaining (need accounts, configured in M9): an external uptime monitor polling `/readyz` with SMS/e-mail
alerts; off-site copy of `backups/` (S3-compatible/B2 bucket); scheduling backups with cron on the server.

### M9 — Production deployment · READY, rehearsed locally · BLOCKED on a server, a domain and accounts

Delivered (`deploy/`, `docs/DEPLOYMENT.md`):
- `deploy/docker-compose.prod.yml`: Caddy (automatic Let's Encrypt HTTPS, HTTP->HTTPS, HSTS and security
  headers, access log without query strings/credentials) is the **only service publishing ports**; PostgreSQL
  (persistent volume), API and dashboard are internal only; `restart: unless-stopped` everywhere; healthchecks;
  json-file log rotation (20 MB × 5). `/api/*` goes straight to the backend so rate limits see real client IPs.
- Production backend image without test tooling (`INSTALL_DEV=false`), non-root, `--proxy-headers`.
- Separate configuration per environment: `.env` (dev), `deploy/.env.staging`, `deploy/.env.production`
  (template with secret-generation commands; real files are git-ignored). The backend refuses to start in
  production with weak/missing secrets, an invalid Fernet key, no real LLM, the default DB password or a non-https
  public URL. OpenAPI docs, the simulator, mock payments, public registration and **simulated WhatsApp numbers**
  are off in production.
- `deploy/deploy.sh` (build, start, wait for HTTPS readiness) and `deploy/verify_deployment.sh` (24 checks).

Fixed during M9:
- Production accepted a simulated ("dev") WhatsApp number: a shop could look live while no customer ever received
  a reply. Now refused when connecting and when sending (regression test).
- The operator CLI could create an owner whose e-mail the login API rejects (reserved domains such as `.test`),
  i.e. an account that can never sign in. Account creation now uses the same validator (regression test).

Evidence — **local production rehearsal** (the real production compose file, `DUKA_DOMAIN=localhost` with
Caddy's internal CA, freshly generated secrets, LLM/Meta URLs pointed at an unreachable port so nothing external
was called):
- `deploy/verify_deployment.sh` -> **24/24**: HTTP->HTTPS (308), liveness/readiness over HTTPS, dashboard served,
  HSTS / nosniff / frame-deny, no Server header, docs disabled, metrics and readiness details only with the ops
  token, registration closed, Meta verify handshake (right/wrong token), unsigned webhook 401 / signed 200,
  PostgreSQL / API / dashboard not published, restart policies, log rotation, no pytest in the image, non-root,
  verify token absent from all logs.
- Production-mode scenario: pilot shop created with the CLI -> owner logs in over HTTPS -> simulated number
  refused (422) -> cloud number connected -> signed inbound WhatsApp message through Caddy is persisted and
  processed (`done`) -> LLM unreachable: agent run `error`, customer reply is the fallback -> Meta unreachable: the
  reply waits in `retry` instead of being lost -> readiness `ok`.
- Crash recovery: the API's PID 1 killed -> container restarted automatically (RestartCount 1), readiness ok.
- Backup + verified restore on the production topology: `RESTORE VERIFIED` (24 tables, real login on the copy).
- `pytest -q` -> 232 passed; ruff clean. The rehearsal stack and its volumes were removed afterwards.

Blocked — what you need to provide (then follow `docs/DEPLOYMENT.md`):
1. A server (Ubuntu 24.04, 2 vCPU / 4 GB) and SSH access.
2. A domain with an A record to that server.
3. LLM provider key (+ base URL/model) — also unblocks M3.
4. Meta WhatsApp: app secret, verify token, the pilot shop's phone number id and permanent access token — also
   unblocks M4's live verification.
5. An uptime-monitor account (alerts) and an S3-compatible/B2 bucket (off-site backups).

### M10 — AI evaluation suite · COMPLETE (offline + adversarial) · real-model run BLOCKED (LLM credential)

- `backend/evals/cases_v1.json` (suite v1.0.0): **40 representative conversations** across product selection,
  price, availability, tool usage, order totals, no fabrication, confirmation, handoff, multilingual (EN/RW/FR/SW/
  mixed) and prompt injection; 29 marked critical. Two stores seeded from the real seed catalogs.
- `evals/harness.py`: runs each case through the **real pipeline** (signed-webhook payload -> durable inbox ->
  agent -> tools -> grounding check -> outbox) on a scratch database and checks every turn against system state:
  tools and arguments, facts that must / must not reach the customer (prices resolved from the DB), number of
  orders and totals, pending summary, cart, handoff, payment status, DB prices after injection attempts, and
  whether the model's own reply passed grounding. Wording is free; facts are not.
- Providers: `rules` (offline engine), **`adversarial`** (a simulated model that lies in every reply — wrong prices,
  invented iPhones, "paid", "delivered", "order placed", free delivery, 50 % off — while choosing realistic tools),
  `openai_compat` (the real model, incl. the 7 `requires_llm` cases).
- Reports record suite version, git SHA, provider/model, a **prompt fingerprint** (system prompt + tool schemas),
  pass rate per category, critical failures, ungrounded rate, latency, and full transcripts for human review.
- **Regression gate** (`tests/test_evals.py`, part of `pytest`): fails if any case that passes in the committed
  baseline (`evals/baselines/{rules,adversarial}.json`) stops passing, or any critical case fails. A meta-test proves
  the gate works: with the grounding check disabled, the adversarial run produces critical failures and regressions.

Results (offline): rules 33/33 pass (7 skipped: need a real LLM); adversarial 33/33 pass with 51 of 59 model-written
replies rejected (86 %) — the other 8 are server-rendered checkout summaries; every customer-facing fact was correct.

Fixed during M10 (found by the adversarial run): when a lying reply was rejected and the turn's only tool result
was a business-rule error ("Only 3 in stock", "Please share your delivery address"), the customer got a generic
"could you clarify" (and two such turns would hand off). Customer-facing tool errors are now part of the
deterministic fallback; internal errors are still hidden. Regression test added.

Evidence: `python -m evals.run --provider rules|adversarial` (both exit 0); `pytest -q` -> 237 passed; ruff clean.

Blocked: `python -m evals.run --provider openai_compat --include-llm-cases --out evals/reports/<model>.json`
needs the LLM key. That run (plus native-speaker review of the Kinyarwanda/French/Swahili transcripts) is what
measures real model quality and the grounding false-positive rate; keep its report as the real-model baseline.

### Feature — Conversation language detection + persistent language state · COMPLETE (offline) · real-model quality BLOCKED (LLM key) · translations need native review

- **State:** `conversations.language_code` (en | rw | fr | sw | ar | ar-SD), `language_confidence`,
  `language_updated_at` (migration 0008); each inbound message keeps its own detection in its metadata; the inbox
  shows the language.
- **Detection** (`app/agents/language.py`): offline and deterministic — nothing is sent to any provider. Product,
  category, shop and customer names, numbers, SKUs and order numbers are ignored. Arabic script selects the
  Arabic family; `ar` vs `ar-SD` is decided by lexical markers (Sudanese داير، متين، شنو، ده، زول، عايز… vs MSA
  أريد، هل، هذا، لديكم، يتوفر…), never by script alone.
- **Switching rule:** the last *confident* detection wins (confidence ≥ 0.5 and ≥ 2 distinct signals); weak
  detections, isolated words and code-switching (the message still contains a distinctive word of the current
  language) keep the current language; an Arabic message without dialect markers keeps the current Arabic variant
  (neither variant is forced). No detection → existing language → business language.
- **AI:** the system prompt states the conversation language explicitly, with a rule per language (Sudanese:
  keep the dialect, never switch to MSA) and an instruction to copy names, SKUs, order numbers and prices exactly.
- **Server-written messages in the conversation language** (`app/i18n.py`, 6 languages, English unchanged):
  tool renders (also the grounding fallback), checkout summary and YES prompt, order confirmation, owner status
  updates (accepted + payment instructions, ready, on the way, delivered, cancelled + refund note), manual and
  provider payment messages, handoff (incl. after hours, with localised opening times), voice-note/media replies,
  AI-paused acknowledgement, greeting, fallback, clarifying question, and user-facing errors (by error code).
  Facts are inserted unchanged; a test enforces that every message exists in every language with identical
  placeholders, and that summaries in all six languages carry identical prices, quantities, names and address.
- **Arabic made safe end to end:** Arabic YES/NO (نعم، ايوه، تمام… / لا، الغي…; "ايوه بس غير المقاس" is not a
  confirmation) and Arabic "talk to a person" now work (the normaliser used to strip non-Latin text); the grounding
  check converts Arabic-Indic digits (٩٥٬٠٠٠) before verifying — previously such a price bypassed it — and knows
  Arabic money/total/paid/status/availability phrasing.
- Evidence: `tests/test_language.py` (50 cases, A–M: 15 detection examples incl. all given Sudanese and MSA
  sentences, variant decided by words, weak dialect does not flip, 9 isolated-word cases, product names, 5
  code-switching cases, Sudanese→English only on the confident message, English→Sudanese, business-language
  fallback, language passed to the model, Sudanese order summary + confirmation + owner updates + payment message,
  after-hours handoff in rw/ar-SD/fr/sw, voice note and AI pause in Swahili, Arabic yes/no on a summary, facts
  identical in six languages, completeness of translations, Arabic-digit fabricated price caught, correct Arabic
  reply passes). Eval suite v1.1.0: +11 cases (7 language-state, 2 Arabic handoff, 2 real-model) — rules 42/42,
  adversarial 42/42 (9 need a real LLM). Live on the Docker stack: ar-SD kept on "ok", switch to English on a
  confident sentence, Sudanese handoff reply.
- Not claimed: real-model multilingual quality (run `python -m evals.run --provider openai_compat` when a key
  exists) and translation quality — rw, sw and ar-SD wording must be reviewed by native speakers before the pilot.
  The offline rules engine cannot search with Arabic/Kinyarwanda queries (it browses); a real model translates them.

### Validation program — real-model, end to end · COMPLETE · see `docs/VALIDATION_REPORT.md`

- Trigger: with the real model (gpt-4o-mini), "phone under 300,000 RWF" in the grocery demo store returned "Rwandan
  Tea 250g" — hash-embedding collisions (0.46 > 0.30) admitted vector-only hits. Search now needs word evidence on
  what a product IS; vector similarity only ranks (`tests/test_search.py`, CLAUDE.md rule 11).
- 21 defects found and fixed by running the real model through the real pipeline, a real browser and a production
  rehearsal — among them: 12/42 real-model turns wrongly rejected by grounding (now 0–2 of ~46, all correct
  catches), rw/fr/sw shoppers told "no black shoes" (untranslated searches), a YES that answered another question
  ordering an old summary, the model imitating the server's summary, dashboard double-taps sending a staff WhatsApp
  message twice, and `verify_restore.sh` checking a stale dump.
- Evidence: backend tests 289 -> 348 passed; eval suite v1.2.0 (71 cases): offline 56/56 with the rules engine
  and with the lying model, real model 67/71; live real-model scenarios 82–83/83; 36/36 cross-tenant attacks
  blocked; production rehearsal 24/24; crash recovery, duplicate webhooks and outages verified live.
- Verdict: READY WITH EXTERNAL DEPENDENCIES (WhatsApp number, server/domain, merchant, native-speaker review,
  privacy/legal review).

### Render preparation · prepared, never deployed

`6f439b9` (`feat(deploy): prepare FastAPI for Render`) added `render.yaml`; `2c482f8` changed it and `fb79e5d` added
the `duka-dashboard` service. It has been checked locally against Render's published schema and has never been deployed
(`docs/EXTERNAL_VALIDATION.md` §1, "where the dashboard runs"). No CI run exists for this commit: CI was added by the
next one (`2c482f8`).

### Production hardening phases 1–3 · COMPLETE (in-repo, green CI) · external validation outstanding

| Phase | Commit | CI (GitHub Actions run, all jobs green) | Added, with the tests that prove it |
|---|---|---|---|
| 1 | `2c482f8` | `37651903982` (#1, 4 jobs) | CI itself (`.github/workflows/ci.yml`); security headers and the 10 MB body limit (`test_http_security.py`); login throttling (`test_login_throttle.py`); database statement, lock and idle-transaction timeouts (`test_db_limits.py`); model allow-list (`test_model_allowlist.py`); hostile uploads (`test_uploads.py`); audit of sensitive settings (`test_audit_events.py`); browser checks for double submits and an API outage (`scripts/ui-smoke/double_submit.js`, `outage.js`) |
| 2 | `01445b5` | `37679363561` (#2, 5 jobs) | WhatsApp 24-hour window and late Meta failures (`test_whatsapp_window.py`); owner reminders for orders waiting for review or accepted but unpaid (`test_order_reminders.py`); the production-image CI job (runtime files only, Trivy gate on fixable critical vulnerabilities) |
| 3 | `fb79e5d` | `37768899926` (#3, 5 jobs) | Kinyarwanda, French and Swahili order/payment/status/cart claims in the grounding check (`test_grounding_multilingual.py`, `docs/MULTILINGUAL_GROUNDING_REVIEW.md`); per-customer rate limit made visible to the owner (`test_rate_limit_visibility.py`); dashboard proxy test (`frontend/tests/proxy.test.mjs`); the pilot runbook `docs/EXTERNAL_VALIDATION.md` |

The runbook's two LOCAL checks were run on 2026-10-10 against `415b9b0` and pass (recorded in
`docs/EXTERNAL_VALIDATION.md` §1). Everything it tags META, LLM, HOST or DECISION is still open.

### Phase 4 P0 — Usage metering · COMPLETE (code, CI, development database) · real WhatsApp traffic and real prices not validated

One insert-only ledger, `usage_events` (`docs/OPERATIONS.md` › Usage metering):
- **AI calls** — `2f77a51` (`feat(usage): add durable AI usage metering`), migration `0009`; CI run `37812248485`
  (#4, 5 jobs green). One row per real model call, written in its own transaction so it survives a rolled-back turn;
  UPDATE and DELETE are refused by a trigger; the only foreign key is to `businesses` (RESTRICT).
- **WhatsApp traffic** — `415b9b0` (`feat(usage): meter WhatsApp traffic in the usage ledger (migration 0010)`),
  migration `0010`; CI run `38033943478` (#5, 5 jobs green). Inbound customer messages (`wa_in`), each send attempt
  to a customer (`wa_out`) or to the owner (`wa_alert`) under the outbox claim's attempt number, interrupted sends as
  `unknown`, late Meta failures as a new row with `units` 0; `is_real`, `message_kind`, `template_name` and `market`
  (a country calling code, never a phone number).
- **Prices:** none are shipped. Costs come only from the operator's `USAGE_PRICING_FILE`; without it every event is
  recorded unpriced.
- **Tests:** `test_usage_metering.py`, `test_whatsapp_metering.py`, `test_migration_0010.py`; 640 backend tests
  collected at `415b9b0`.
- **Development database:** `commerce` migrated `0008 → 0009 → 0010` on 2026-10-10, after a fresh backup was verified
  (checksum, and a restore whose row counts matched its manifest). `alembic check` is clean and the ledger is empty.

Not validated: real Meta sends, signed webhooks and status webhooks have only been exercised against mocks (blocked
with M4); there is no real price list, so no cost has been computed; the calling-code table matches ITU's list as of
15 December 2016; nothing has been deployed.

### Phase 4 — remaining slices · Proposed — awaiting product approval

Recorded on 2026-10-10 as a proposed product plan. It is not a record of work done, none of these slices exists in
the code, and the order may change when it is approved.

| Slice | Proposed scope |
|---|---|
| P0 | Completed: AI and WhatsApp usage metering (section above). |
| P1 — Runaway conversation guard | Tenant-scoped limits that stop runaway agent loops and bound AI spend. Define safe defaults, explicit failure behaviour and tests before implementation. |
| P2 — Embedding usage metering | Measure embedding usage per tenant and avoid double-counting. |
| P3 — Monthly usage reporting | Aggregate actual usage by tenant, month, provider/event type, and real versus simulated traffic. Unknown and unpriced events stay explicitly distinguishable. |
| P4 — Tenant cost model | Known provider costs and a clearly documented infrastructure allocation per tenant. Never invent missing prices or present estimates as exact costs. |
| P5 — Quotas and spend alerts | Tenant-level thresholds and actionable alerts, built on validated usage and cost data. |
| P6 — Dashboard usage UI | Monthly usage, known costs, unpriced usage, quotas and alerts, with strict tenant isolation. |
| P7 — Billing | Deferred until usage, cost allocation, pricing and tenant isolation have been validated. |

### M11 — Real pilot (one Rwandan merchant) · NOT STARTED · BLOCKED on M2–M10 and a merchant

### M12 — Multi-store validation (2–5 stores) · NOT STARTED · BLOCKED on M11

## Definition of Done — current state

| Item | State |
|---|---|
| Real LLM works | 🟡 production path + safety built (M3); `app.cli llm-check` passed with a developer's local key (`docs/VALIDATION_REPORT.md` §2); BLOCKED until it passes with the production account on the production host |
| Real WhatsApp works / real webhook works | ❌ never connected (Meta setup) |
| Tenant isolation is proven | ✅ M2 (API, tools, webhooks, DB triggers, concurrency) |
| Real products/prices are used | ✅ from DB (no real merchant catalog yet) |
| AI does not invent commerce facts | 🟡 server-rendered critical texts + grounding check with deterministic fallback (M3); to be measured with the real model (M10) |
| Cart works | ✅ |
| Explicit order confirmation works | ✅ M5 (server-enforced, tested incl. misbehaving LLM) |
| Owner receives new-order notification | 🟡 M5 (dashboard + WhatsApp outbox; live Meta delivery untested) |
| Owner can review/accept orders | ✅ M5 |
| Human takeover works | ✅ M5/M6 (detection EN/RW/FR/SW, low confidence, voice notes, after hours, takeover, AI pause, explicit return) |
| Merchant dashboard works | ✅ M7 (headless browser walk-through 20/20, live refresh) |
| Production HTTPS works | 🟡 Caddy/HTTPS rehearsed locally (M9); needs the real domain |
| Secrets are protected | ✅ separate env files, startup refusal on weak secrets, encrypted tokens, scrubbed logs (M8/M9) |
| PostgreSQL is not publicly exposed | ✅ production compose publishes no DB port (verified in rehearsal); dev compose still does, for local use |
| Backups exist / restore tested | 🟡 scripts + verified restore locally (M8); server cron + off-site copy pending (M9) / ✅ restore verified (M8) |
| Monitoring exists | 🟡 /readyz, /metrics, scrubbed JSON logs (M8); external monitor needs an account (M9) |
| AI evaluation exists | ✅ M10 (40 cases, rules + adversarial gate in pytest); real-model run blocked on key |
| Critical failure scenarios handled | 🟡 LLM/tool failures, crashes, redeliveries, send failures (M4), order safety (M5); real-LLM output checks pending (M3) |
| One real merchant used it / real traffic tested | ❌ |
| No critical data leakage | 🟡 none found; not yet proven exhaustively |
| 2–5 stores operate safely | ❌ |
