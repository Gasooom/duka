# Duka — Milestone Status

Last updated: 2026-10-04 (M2, M4, M5 complete).

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

### M3 — Real AI · IN PROGRESS · live verification BLOCKED (LLM credential)
Done: OpenAI-compatible provider with tool calling, retries, timeout, malformed-response handling, invalid
JSON args, unknown tools, iteration cap, fallback message, token/latency capture (all stub-tested).

Gaps:
- `LLM_PROVIDER=rules` is the default; no real model has ever been called.
- The model's final text is not checked: if it writes a price or total that no tool returned, it is still sent.
- `create_order` relies on the prompt alone ("only when the customer confirms"), not on server-side state.
- Worst-case latency is unbounded for WhatsApp: 5 iterations × 30 s timeout × 3 attempts, all inside one DB
  transaction holding the conversation row lock.
- Multilingual: zero tests. Live (rules): Kinyarwanda → "couldn't find anything"; French → wrong products.
- Customer WhatsApp number is sent to the LLM vendor in the context snapshot (unnecessary PII).

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

### M6 — Human control · IN PROGRESS
Done in M5: deterministic multilingual "talk to a person", handoff tool, voice notes/media routed to a person,
owner alert, takeover pauses the AI, staff replies via the outbox, explicit return-to-AI rule, audit.
Remaining: after-hours behaviour (business hours are stored but unused), low-confidence handoff, a business-wide
"AI paused" switch, and dashboard live refresh so handoffs are seen without reloading (M7).

### M7 — Merchant dashboard · IN PROGRESS
Present: inbox with takeover/reply/return, conversation debugger, orders list/details/status changes,
products CRUD + deactivate + stock + CSV import, knowledge text/PDF/TXT/MD, business profile, AI config,
delivery zones, payment provider, WhatsApp connect, stats.

Gaps: no live refresh; no order accept/reject; no manual payment; "Simulate payment" buttons are the main
payment action; business hours editing UX unverified; no handoff/after-hours settings; no AI pause per
business; JWT stored in localStorage; public self-registration is open.

### M8 — Reliability · NOT STARTED (partial building blocks)
Present: JSON logs with request/tenant context and key-based redaction; `agent_runs` records latency,
tokens, errors; `/health` checks DB.
Missing: `/healthz`; metrics/alerting for LLM errors, webhook failures, send failures, order failures;
error tracking; backups; restore procedure; restore verification.
Note: unhandled-error logs include `repr(exc)` which can contain SQL parameters (possible PII).

### M9 — Production deployment · NOT STARTED
Current compose is dev-only: Postgres published on `0.0.0.0:5432` with `commerce/commerce`; no TLS/reverse
proxy; no restart policies; dev dependencies in the backend image; `/docs` exposed; no prod/staging config
split; `ENCRYPTION_KEY` only enforced on first use (app boots without it); no domain.

### M10 — AI evaluation suite · NOT STARTED
No eval set, no versioning, no prompt-injection or multilingual cases.

### M11 — Real pilot (one Rwandan merchant) · NOT STARTED · BLOCKED on M2–M10 and a merchant

### M12 — Multi-store validation (2–5 stores) · NOT STARTED · BLOCKED on M11

## Definition of Done — current state

| Item | State |
|---|---|
| Real LLM works | ❌ never called (credential) |
| Real WhatsApp works / real webhook works | ❌ never connected (Meta setup) |
| Tenant isolation is proven | ✅ M2 (API, tools, webhooks, DB triggers, concurrency) |
| Real products/prices are used | ✅ from DB (no real merchant catalog yet) |
| AI does not invent commerce facts | 🟡 tools are DB-backed; model output unchecked |
| Cart works | ✅ |
| Explicit order confirmation works | ✅ M5 (server-enforced, tested incl. misbehaving LLM) |
| Owner receives new-order notification | 🟡 M5 (dashboard + WhatsApp outbox; live Meta delivery untested) |
| Owner can review/accept orders | ✅ M5 |
| Human takeover works | ✅ M5 (detection EN/RW/FR/SW, alerts, AI pause, explicit return); after-hours pending (M6) |
| Merchant dashboard works | 🟡 |
| Production HTTPS works | ❌ |
| Secrets are protected | 🟡 env-based, encrypted tokens; no prod secret handling |
| PostgreSQL is not publicly exposed | ❌ published on 0.0.0.0:5432 |
| Backups exist / restore tested | ❌ / ❌ |
| Monitoring exists | ❌ |
| AI evaluation exists | ❌ |
| Critical failure scenarios handled | 🟡 LLM/tool failures, crashes, redeliveries, send failures (M4), order safety (M5); real-LLM output checks pending (M3) |
| One real merchant used it / real traffic tested | ❌ |
| No critical data leakage | 🟡 none found; not yet proven exhaustively |
| 2–5 stores operate safely | ❌ |
