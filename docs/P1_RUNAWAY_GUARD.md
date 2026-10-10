# P1 — Runaway Conversation Guard: design

Status: design reviewed and decisions D1–D6 approved 2026-10-10 (§10). B1–B3 implemented (§9); B4–B5 in
progress; B6 (fairness) deferred (D5).
Roadmap context: `docs/ROADMAP.md` Phase B. Code references are to commit `e1a18c2`.

## 1. Goal

Bound the model calls, provider attempts and tokens one inbound message, one customer and one tenant can cause, in
every process and instance, without ever blocking a deterministic commerce step (order confirmation, handoff,
order updates, payments) or making a reply claim something that did not happen.

## 2. Current execution path (verified in code)

```
webhook → ingest(): webhook_events row committed before the 200 (duplicate wamid dropped: no model call)
worker (BACKGROUND_WORKERS=2 per process) → claim(): SKIP LOCKED, global ORDER BY seq, per-sender FIFO, lease
  process_event(): attempts > WEBHOOK_MAX_ATTEMPTS (5) → dead                     inbound.py:216
  ONE transaction for the turn (commit inbound.py:231):
    process_message → conversation row FOR UPDATE (inbound.py:299)
      in-process limiter 30 msgs/min per customer (inbound.py:321, ratelimit.py:91)
      AgentEngine.run (engine.py:250)
        deterministic YES / handoff (engine.py:447), greeting (engine.py:275)          no model call
        summary: ≤ 1 model call, timeout 10 s (engine.py:193)
        loop ≤ 5 iterations (engine.py:287); deadline checked before model calls only (engine.py:290)
          _complete (engine.py:138) → provider: ≤ 3 HTTP attempts (openai_compat.py:53); ledger row
          tools: ≤ 8 per turn (engine.py:310), SAVEPOINT each, no deadline; search may call the embeddings
          API: timeout 20 s × 3 tries, no backoff (embeddings.py:80)
  any failure outside the agent → rollback → retry → the whole turn again (new model calls)
```

`AgentEngine._complete` is the only path to a metered model call (the other `.complete()` caller is the unmetered
operator command `app/cli.py:96`).

## 3. Findings

Probes ran against a disposable database with a model that never stops asking for tools.

| # | Severity | Finding | Evidence | Confidence |
|---|---|---|---|---|
| F1 | High | A turn that fails after its model calls repeats them on every retry | Probe: 25 model calls and 25 ledger rows for one message, event `dead`, 0 replies | Demonstrated |
| F2 | High | No tenant ceiling | Probe: 40 customers → 200 calls, nothing stopped | Demonstrated |
| F3 | Medium | Per-customer limit is in-process, counts messages not model calls, minute window only | `ratelimit.py`, `inbound.py:321` | Verified |
| F4 | Medium | Tool time is outside the turn deadline; embeddings retry without backoff or deadline | Probe: 3 s budget took 6.1 s; `embeddings.py:80` | Demonstrated (embeddings path: code only) |
| F5 | Low | Budget exhaustion drops the facts tools already returned (fallback apology) | Probe status `error`; `engine.py:336` vs `:335` | Verified |
| F6 | Medium at scale | No per-tenant fairness in the worker claim | `inbound.py:164-177` | Verified (code) |
| F7 | Medium | Money cannot be enforced: no prices; a timed-out call is priced 0 though it may be billed | `pricing.py`, `usage_service.py` | Verified |
| F8 | Info | Ambiguous outcomes are retried inside the adapter; every attempt is counted in `attempts` | `openai_compat.py:74` | Verified |
| F9 | Low | A graceful shutdown hands an attempt back, so repeated shutdowns can reprocess a message beyond 5 attempts | `inbound.py:193-196` | Theoretical |
| OK | — | Duplicate webhooks, outbox retries and status webhooks make no model calls; evals and the rules engine are unmetered; deterministic paths never call the model | `test_durability`, `test_whatsapp_metering`, `engine.py:269-278, 447` | Verified |

## 4. Architecture

- **Enforcement point:** `AgentEngine._complete`, before `provider.complete`, only for providers that are
  `is_llm and metered`. The summary call goes through it too; a denied summary falls back to the existing
  deterministic summary (`engine.py:216`).
- **Store:** a mutable operational table `ai_usage_counters`, separate from the insert-only `usage_events` ledger.
  The ledger stays the history and the source for choosing thresholds and reconciliation; it is never updated and
  never used for enforcement (it is written after the call, best effort, and cannot hold an in-flight reservation).
- **No new infrastructure.** PostgreSQL gives atomicity across threads, processes and instances. Redis would only be
  reconsidered if counter-row contention shows in metrics.

## 5. Design resolutions

### 5.1 Transaction and checkout safety
- The reservation runs in its **own short transaction on its own connection** (`bind.engine`, as the ledger does),
  committed before the model is called. It survives a rollback of the turn (the spend is real) and holds its row
  lock only for one statement, never for the turn.
- Lock compatibility: the counter row's foreign key takes `FOR KEY SHARE` on the business row, compatible with the
  `FOR NO KEY UPDATE` that order-number allocation holds; it never touches the conversation row the turn locked.
  No lock-order cycle is possible (the reservation transaction touches only counter rows, in a fixed order, §5.3).
- The guard sits only on the model-call path. The deterministic YES that places an order, handoff requests, the
  greeting, owner order updates and payment messages never reach it, so a quota can never stop or alter them.
- If the guard stops a turn after `prepare_checkout` ran, the server-rendered summary is still sent (existing
  `engine.py:341`); the order is still placed only by a later YES. If the turn later rolls back, the checkout rolls
  back with it; only the reservation (real spend) remains.

### 5.2 Durable message identity
- The per-message budget is keyed by the **webhook event id**: unique per (business, wamid), stable across retries,
  lease reclaims and graceful-shutdown releases (closes F9). The inbound `Message` id is not usable: it is
  re-inserted on each attempt after a rollback.
- Plumbing: `process_event` passes its event id to `process_message`, which passes it to `AgentEngine`. Engine runs
  without an event (direct calls in tests) skip the per-message scope; customer and tenant scopes still apply.
- `python -m app.cli requeue-dead` re-processes a dead event on purpose, after a fix: it must also clear that
  event's message-scope counter (an operational row, not the ledger), logged, or the retry would have no budget.

### 5.3 Atomic counter correctness
- One conditional upsert per scope:
  `INSERT … VALUES (…, calls = 1) ON CONFLICT (business_id, scope, subject_id, window, window_start)
   DO UPDATE SET calls = ai_usage_counters.calls + 1 WHERE ai_usage_counters.calls < :limit RETURNING calls`.
  No row returned = limit reached. A new key inserts `1`; a racing insert of the same key takes the update path
  (`ON CONFLICT` handles it). A limit of 0 is refused before SQL.
- Several scopes (message, customer, tenant) are reserved **in one transaction, in a fixed order** (scope, then
  key). Any refusal rolls the whole transaction back: never a partial reservation. The fixed order means two
  concurrent reservations cannot deadlock.
- After the call, the same row gets the actual HTTP attempts and tokens added (a second short transaction). A crash
  between reservation and call over-counts by one call: the safe direction.
- Windows are fixed buckets (`window_start` truncated); the exact windows are decision D2.

### 5.4 Retries
- Inside one model call: one reservation per call; its attempts are reconciled afterwards and may have their own
  limit (D6). The adapter's existing retry rules (only 408/409/429/5xx/network, Retry-After ≤ 10 s, within the
  deadline) stay.
- Across webhook retries and lease reclaims: the per-message budget (§5.2), default = what one attempt may use
  (1 summary + `AGENT_MAX_TOOL_ITERATIONS`), derived from existing settings rather than invented.
- Outbox retries, duplicate webhooks and status webhooks make no model calls: no reservation.

### 5.5 Failure behaviour (when the guard is enforcing)
| Situation | Customer sees | System |
|---|---|---|
| Per-run limit (exists) | Reply rendered from the tool facts | Unchanged |
| Turn budget exhausted (B1 fix) | Reply rendered from the tool facts gathered so far; else the existing fallback | Run `error`, facts kept |
| Per-message budget used up on a retry | Tool facts if any, else "we'll get back to you" | Conversation flagged |
| Customer or tenant quota reached | Localised "our assistant is unavailable right now; the team will reply" | `needs_attention`; one owner alert per window |
| Counter store unavailable | Same as quota reached if fail-closed (D1) | Logged and counted |
| Provider timeout / unknown outcome | Unchanged (fallback, two-strike handoff) | Attempt counted |

No guard text may say an order, payment, reservation or refund happened. New texts exist in all six languages
(CLAUDE.md rule 10) and need native review before the pilot.

### 5.6 Rollout and rollback
`AI_GUARD_MODE=off|observe|enforce` (default `off` until B2 is reviewed, then `observe`). Observe computes every
decision, logs `ai_guard.decision` and counts it, but never changes a reply. Enforcement is switched on scope by
scope (message → customer → tenant). Rollback: set `off` (no code change); migration `0011` is additive and its
downgrade drops only the counter table (operational data, never the ledger).

## 6. Limits

All configurable; no number is chosen here. Initial values come from the distribution of model calls, attempts and
tokens per run, per customer-hour/day and per tenant-hour/day in `agent_runs` and `usage_events`: first from the
real-model eval and live scenarios already recorded, then from observe mode on the pilot. Limits sit above the
observed peaks with a margin the owner approves.

| Limit | Protects | Scope | Atomic | On reaching it |
|---|---|---|---|---|
| L1 iterations / tool calls / attempts per run | Loops in a turn | Run | No (exists) | Facts rendered |
| L2 model calls per message across retries | F1, F9 | Event | Yes | §5.5 |
| L3 model calls (and attempts) per tenant per window | F2 | Tenant | Yes | §5.5 |
| L4 tokens per tenant per window (D6) | Token-heavy loops | Tenant | Yes, reconciled after the call | §5.5 |
| L5 spend | Money | Tenant | — | Deferred to Phase C (F7) |
| L6 model calls per customer per window | One customer draining a tenant | Customer | Yes | §5.5 |
| L7 concurrency per tenant (D5) | Noisy neighbour | Tenant | Approximate, in the claim SQL | Waits, never lost |
| L8 deadline over tools and embeddings | F4, F5 | Run | No | Facts rendered |

## 7. Data model

Migration `0011` (B2): `ai_usage_counters(business_id NOT NULL → businesses, scope, subject_id, window,
window_start, calls, attempts, input_tokens, output_tokens, updated_at)`. `subject_id` is the webhook event id, the
customer id, or the business id itself for the tenant scope, so the unique key (business_id, scope, subject_id,
window, window_start) never holds a NULL; old buckets purged by the existing retention sweep;
added to the tenant-isolation meta-tests. No change to `usage_events`.

## 8. Tests (deterministic, PostgreSQL, CI)

Exact boundaries per limit; six threads with separate connections racing for the last unit (exactly one wins);
enforcement survives an in-process reset and a second engine/connection (and a subprocess); the probe-2 retry case
stops at the message budget; tenant A's quota never affects tenant B; with the quota exhausted a YES still places
the order, handoff and greeting still work, the checkout summary is still sent; a slow tool ends the turn at most one
tool past its budget with facts rendered; counter store down, provider timeout with retries, ledger write failure;
duplicate webhooks, outbox retries, evals and the rules engine consume nothing; the simulator with a real provider
counts. Metrics: `duka_ai_guard_decisions_total{scope,window,decision}`; log `ai_guard.decision` without message text
or phone numbers.

## 9. Sequence

B1 deadlines + facts kept (no schema) → B2 table + observe mode → B3 enforce per message → B4 per customer → B5 per
tenant → B6 fairness (optional). One reviewed, CI-green commit per step.

**B1 — implemented, CI VERIFIED** (`0ac995e`, GitHub Actions run 38043387589; `tests/test_turn_deadline.py`):
- The turn deadline lives in a context variable (`app/core/deadline.py`) set by `AgentEngine.run`, so work a tool
  starts sees it too. The deadline is checked before every tool call, not only before model calls; skipped calls are
  recorded as a `deadline` step. The summary call takes at most `min(10 s, time left)` and is skipped (deterministic
  summary) with under 2 s left.
- When the time runs out, the customer gets the facts the tools already returned (as at the iteration limit), else
  the fallback; the run is `error` ("Turn time budget … exhausted") and `agent.turn_budget_exhausted` is logged. The
  two-strike handoff is unchanged.
- Embeddings: each request's timeout is `min(20 s, time left in the turn)`; retries back off 0.5 s then 1 s and stop
  when the turn could not fit another request.
- Found while challenging B1: an embeddings failure inside a search reached the customer as the raw English text
  "Embedding request failed: …". Searches now fall back to ranking by words alone (`query_vector`, log
  `embeddings.unavailable`); admission is unchanged (CLAUDE.md rule 11), so the same products are found.
- Not covered: other external calls inside tools (MoMo `request_payment`, not enabled) are bounded only by their own
  timeouts.

**B2 — counters and observe mode** (migration `0011`, `app/services/ai_guard.py`, `tests/test_ai_guard.py`,
`tests/test_migration_0011.py`):
- `ai_usage_counters` holds, per tenant, rows for `message` (subject = the webhook event id, period `lifetime`),
  `customer` and `tenant` (periods `hour`, `day`), with `calls`, `attempts`, `over_limit`, `denied`, `alerted_at`.
- `AgentEngine._complete` reserves one call and its first attempt before every metered model call; the provider's
  own retries are reserved through an attempt gate (`providers/base.py` `may_send_attempt`, called by
  `OpenAICompatProvider` before each attempt after the first), so adapter retries are counted like the call itself.
- The webhook event id is passed from `process_event` through `process_message` to the engine, so a message's
  budget is the same row on every retry, lease reclaim and graceful-shutdown release.
- Each reservation is its own transaction on its own connection, rows in a fixed order; it survives a rollback of
  the turn (tested) and loses no update under concurrent workers (tested with 8 threads).
- Observe mode (the default for every scope) counts, increments `over_limit` and logs `ai_guard.decision`
  `would_block` past a configured limit, and never refuses: replies and model calls are identical with the guard off
  (tested). A counter-store failure is logged as `ai_guard.store_error` and the call goes ahead in observe mode.
- Limits: per message, default = one processing attempt (1 summary + `AGENT_MAX_TOOL_ITERATIONS` calls, each up to
  `LLM_MAX_ATTEMPTS` attempts); per customer and tenant, unset (0) by default: counted only, to be chosen from
  observe-mode data. `enforce` is refused at startup until the scope's enforcement step ships.
- Retention: hour buckets 2 days, day buckets 8 days, message rows `WEBHOOK_EVENT_RETENTION_DAYS`, in the existing
  hourly sweep (`ops.purge_ai_usage_counters`); the ledger is never purged. Metrics:
  `duka_ai_guard_reserved_current_hour`, `duka_ai_guard_busiest_tenant_calls_current_hour`,
  `duka_ai_guard_over_limit_24h`, `duka_ai_guard_denied_24h`.

**B3 — per-message budget, enforced by default** (`tests/test_ai_guard_enforce.py`):
- `AI_GUARD_MESSAGE_MODE=enforce` is the default: one inbound message (its webhook event) may reserve, across all its
  processing retries, what one processing attempt may use (1 summary + `AGENT_MAX_TOOL_ITERATIONS` calls, each with
  up to `LLM_MAX_ATTEMPTS` HTTP attempts). The limit never stops a first attempt, only retries. Measured with the
  failing-turn probe: 25 model calls for one message before, 6 now.
- In enforce mode the upsert only increments a row that is under its limits. A refusal rolls the whole reservation
  back (no call is made, nothing is counted), adds 1 to `denied` on the refusing row, logs `ai_guard.decision`
  `refused` with the limit that was reached (`calls` or `attempts`), and raises `AIGuardDenied`.
- A refused provider retry stops the adapter (`may_send_attempt` returns False); the ledger row records the attempts
  really sent, then the turn is treated as limited.
- A limited turn (`agent_runs.status = limited`, a `guard` step names scope, period and reason): the customer gets
  the facts the tools already returned, else `ai_limited` (plus `ask_person` when handoff is on, else the shop's
  phone); nothing is claimed. The conversation is flagged; the owner gets one `assistant_limited` alert per tenant
  and UTC window (the hour for a message budget or an hourly limit, the day for a daily limit), decided by one atomic
  claim on the tenant row (`alerted_at`). An alert claimed by a turn that then rolls back is not sent: at most once,
  never twice. A limited turn does not count toward the two-strike handoff, and grounding is not applied to its
  server text.
- Fail closed (D1): a reservation that involves an enforcing scope and cannot be made or trusted is refused
  (`store_unavailable`); a reservation with observe-only scopes goes ahead.
- Deterministic commerce stays as it was (tested with every model call refused): the YES to the delivered summary
  places exactly one order, a second YES places nothing, owner status updates reach the customer, the customer can
  reach a person, no payment is recorded; a checkout summary prepared before the limit is still sent and still needs
  the YES.
- `python -m app.cli requeue-dead` clears the requeued events' message counters: an operator retry after a fix gets
  a fresh budget, as it gets fresh attempts.
- New customer texts `ai_limited` and `ask_person` exist in all six languages and wait for native review like the
  rest of `app/i18n.py`.

## 10. Decisions (approved by the product owner, 2026-10-10)

- **D1 Failure policy.** Fail closed for new model calls when a reservation cannot be made or trusted (enforce mode).
  Deterministic commerce paths keep working only when their normal preconditions hold; nothing bypasses order
  confirmation, payment validation, grounding, authorization or tenant isolation.
- **D2 Windows.** UTC hour and UTC day, as **fixed buckets** (`date_trunc` in UTC on PostgreSQL's clock), not rolling
  windows: one row per bucket makes each reservation a single atomic upsert, with no per-event history to sum and no
  race between counting and inserting. Cost: a burst that straddles a boundary can use up to twice an hourly limit
  within 60 minutes; the daily bucket still bounds it. No monthly quota in P1.
- **D3 Configuration.** Environment defaults; operator-controlled per-tenant overrides only if safe in the existing
  architecture (planned for B5 as an operator-set environment value, no new storage); no dashboard controls in P1.
- **D4 Quota reached.** Flag the conversation, send a safe fallback with a way to reach a person, and alert the
  owner at most once per tenant and window. Never claim a handoff or commerce action that did not happen.
- **D5 Fairness.** Deferred unless measured starvation is shown; keep queue-delay observability.
- **D6 Units.** Model calls and provider HTTP attempts. No spend limits without reliable prices. **Tokens are
  deferred:** they are known only after a call returns, providers may not report them (`None`), and a failed or
  timed-out call has none, so they are not reliable at the point where a reservation must be decided.

## 11. Non-goals

Spend limits and prices (Phase C); embedding metering (C1; B1 only bounds embedding time); dashboard UI; billing;
Redis or a queue; changing the inbound 30/min limiter (it stays as a cheap first line); load testing beyond the
deterministic concurrency tests.
