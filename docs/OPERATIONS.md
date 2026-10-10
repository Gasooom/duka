# Duka — Operations runbook

For whoever runs the platform. Everything below runs on the server with `docker compose`.

## Health and monitoring

| Endpoint | Use | Response |
|---|---|---|
| `GET /healthz` | Liveness (Docker healthcheck, restarts) | 200 while the process serves requests |
| `GET /readyz` | Readiness / uptime monitor | 200 `ok` or `degraded`, **503 `down`** when customers are affected |
| `GET /readyz?details=1` | What is wrong | per-check detail (needs `Authorization: Bearer $OPS_TOKEN` in production) |
| `GET /metrics` | Prometheus scrape | gauges below (needs the ops token in production) |

Readiness checks: database reachable · migrations at head · workers alive · inbound backlog (degraded > 2 min,
**down > 10 min**) · dead-lettered messages in 24 h · failed outbound messages in 1 h · agent error rate in 1 h
(> 20 % with ≥ 5 runs) · failed owner alerts in 24 h.

Metrics: `duka_up`, `duka_workers_running`, `duka_webhook_events{status}`, `duka_inbound_oldest_pending_seconds`,
`duka_outbox_messages{status}`, `duka_agent_runs_1h{status}` (success / error / ungrounded / handoff /
order_confirmed / …), `duka_agent_latency_ms_1h{quantile}`, `duka_llm_tokens_1h{type}`, `duka_orders_created_24h`,
`duka_owner_alerts_24h{status}`. Only aggregate counts — no tenant data.

**Minimum monitoring for the pilot:** an external uptime monitor (e.g. UptimeRobot, Better Stack, Healthchecks)
polling `https://<api-domain>/readyz` every minute and alerting the operator by SMS/e-mail on a non-200 or
timeout. Optionally poll `/readyz?details=1` with the ops token and alert on `"degraded"` as well.

## Logs

`docker compose logs -f backend` — one JSON object per line with `request_id`, `business_id`, `customer_id`,
`conversation_id`, `operation`, `status`, `duration_ms`. Useful queries (`jq`):

```bash
docker compose logs backend --no-log-prefix | jq -c 'select(.level=="ERROR")'
docker compose logs backend --no-log-prefix | jq -c 'select(.msg=="webhook.dead" or .msg=="outbox.failed")'
docker compose logs backend --no-log-prefix | jq -c 'select(.msg=="agent.ungrounded")'     # model replies replaced
docker compose logs backend --no-log-prefix | jq -c 'select(.msg=="http.request" and .status>=500)'
```

Never logged: passwords, tokens, API keys (key-based redaction), bearer tokens, `access_token=` URL parameters,
SQL statements and parameters from database errors, phone numbers (masked to the last 3 digits), query strings
(access log records the path only), customer message text. Customer messages are stored only in the database
(`messages`); processed webhook payloads are purged after `WEBHOOK_EVENT_RETENTION_DAYS` (30).

## Usage metering

`usage_events` is the one usage ledger. Rows are inserted, never changed or removed: a database trigger refuses
UPDATE and DELETE, a shop that has usage cannot be deleted, and a correction is a new row. Each row is for the shop
whose conversation, webhook account or outbox row it came from, never from anything a customer or caller supplies.

**AI** (`llm_call`): every call the assistant makes to a real AI model (reply turns and conversation summaries): the
model the provider served and the one Duka asked for, input/output tokens, attempts (provider retries), success or
error, and an estimated cost. Each row is written in its own transaction right after the call, so it is kept when the
turn that made the call fails and is retried, and when conversations, customers or processed webhook events are
deleted later. `agent_runs.llm_calls` is not a usage figure: it also counts the offline rules engine. Nothing is
recorded for the rules engine, evaluation runs (`evals/`) or `python -m app.cli llm-check`.

**Embeddings** (`embedding`, migration 0012): every request to a paid embeddings provider
(`EMBEDDING_PROVIDER=openai_compat`), with `source_type`: `product` (a product created, or an edit that sets its
name, description or category), `product_import` (one request for all the new products of a CSV import),
`knowledge_document` (one request for all of a document's chunks), `product_search` and `knowledge_search` (the
assistant's search tools and the dashboard's searches; a product search with a price limit that finds nothing
searches again without it, a second request). One row per request, never per text: `units` is the number of texts
in it, `input_tokens` what the provider reported (NULL when it reports nothing), `attempts` the HTTP attempts of that
one request (its retries are not new rows), the served and the configured model, `success` or `error`; key
`emb:<uuid>`. Each row is written in its own transaction right after the request, so a rolled-back import, document
or turn keeps it. A failed request is recorded, and then the import or document fails as before and a search ranks
by words alone. Nothing is recorded for the default `hash` embedder (offline, free), for evaluation runs (as for
their model calls), or for a request that was never sent (too little of the AI turn left). A CSV import re-embeds
every product it updates (an existing SKU), one `product` request each, even when the product's text did not change
(`docs/P2_EMBEDDING_METERING.md` §6). Migration 0012 cannot be downgraded once embedding rows exist; keep it.

**WhatsApp** (migration 0010):

| kind | one row per | key (unique per shop) | status |
|---|---|---|---|
| `wa_in` | inbound customer message that was stored: text, interactive, button, image, audio, video, document, sticker, location, contacts. Not counted: reactions, WhatsApp system notices, anything else | `wa_in:<wamid>` | `received` |
| `wa_out` | attempt to send a message to a customer | `wa_out:<message id>:<send attempt>` | `success`, `failed`, `unknown` |
| `wa_alert` | attempt to send an alert to the owner | `wa_alert:<notification id>:<send attempt>` | `success`, `failed`, `unknown` |
| `wa_out` / `wa_alert` | late failure: WhatsApp accepted a message and then reported it failed | `wa_late_fail:<message or notification id>` | `late_failed` (`units` 0: a correction, never another send) |

- **Attempts.** The attempt number is the outbox send attempt (`send_attempts` / `attempts`), handed out by the same
  atomic claim that decides which worker sends, so a transient failure and its retry are two rows (`…:1` failed, `…:2`
  success) and two workers can never record the same attempt twice. One attempt may hold several HTTP requests inside
  the Cloud adapter (up to 3); they are logged as `http_attempts` on `whatsapp.send`, not stored. Nothing is recorded
  when nothing was attempted: the 24-hour window found closed, no active WhatsApp account, a Cloud account without a
  token (those outcomes are logged and shown on the message as before).
- **Interrupted sends.** A send found stuck in `sending` (the process died) is recorded as `unknown` under its own
  attempt key, so a result that attempt had already written is kept. Whether the attempt was real is not stored
  anywhere: it is read from the account only if the account was not changed since the attempt was claimed, otherwise
  `is_real` is NULL (the only case where a WhatsApp row may lack it). An interrupted alert does not claim to know
  whether it was a template. A result that arrives after recovery already recorded `unknown` is not stored (rows are
  never updated); the window is the recovery timeout (120 s) against an adapter call of at most about 30 s.
- **Late failures.** The original success row is never edited. The late failure repeats what that attempt recorded
  (real or simulated, template, market); when that row is missing (for example a message sent before 0010) it uses
  what is known now. Reporting should treat a message with a late failure as not delivered.
- **Real or simulated** (`is_real`). A send is real when it went through the Cloud API adapter; the development
  adapter, test adapters and `WHATSAPP_FORCE_DEV` are not. An inbound message is real when its webhook signature was
  verified with `WHATSAPP_APP_SECRET`: production requires the secret, so there every inbound message is real; the dev
  simulator, tests and an unsigned development webhook are not. For an inbound message the account mode never decides
  it.
- **Template and market.** `message_kind` is `free_form` or `template`; `template_name` is set for a template and never
  guessed (customer messages are always free-form; an owner alert is a template only when the shop configured one).
  Duka does not know Meta's template category and stores none. `market` is the recipient's country calling code, for
  example `250`, from the digits sent to or received from WhatsApp, by a longest match on the public calling-code table
  (`app/integrations/whatsapp/market.py`, checked against ITU's list of assigned codes as of 15 December 2016; a code
  assigned later resolves to NULL); it is NULL for a local number (leading 0), digits that start with no assigned code,
  and a `1` or `7` number that is not 11 digits long. It never comes from where the business is, and no phone number is
  stored (the column holds three characters at most). Several territories share some codes: `1` is the whole North
  American numbering plan (United States, Canada, the Caribbean), `7` Russia and Kazakhstan, `39` Italy and Vatican
  City, `599` Curaçao and the Caribbean Netherlands: a price rule for a code applies to all of it. Other digits typed
  without a country code and without a leading 0 (an owner typing a number without the country code) resolve to
  whatever code their first digits form.
- **Downgrade.** Migration 0010 cannot be downgraded once WhatsApp rows exist (they could only be removed by defeating
  the append-only trigger); keep it.

Accepted limitation (for now): metering is best effort, with no queue behind it. If the backend process dies in the
moment between an external call and the commit of its row, or the row cannot be written, that one call is missing from
`usage_events`. A failed write is logged as `usage.record_failed` with the row's fields (never a phone number), and the
customer's reply or the message still goes out. An inbound row and a late-failure row are written in the transaction of
the state they describe (inside a savepoint), so a turn that fails and is retried records its inbound row once.

The cost comes from the operator's price list, a JSON file named by `USAGE_PRICING_FILE`. Duka ships no prices:
copy them from the provider's pricing page, and change `version` whenever a price changes (each row keeps the cost
and the version it was recorded with; earlier rows are never re-priced).

```json
{"version": "2026-10-08", "currency": "USD",
 "llm": [{"provider": "openai_compat", "model": "<model>", "input_per_1m": "<price>", "output_per_1m": "<price>"},
         {"provider": "openai_compat", "model": "<model name prefix>", "match": "prefix",
          "input_per_1m": "<price>", "output_per_1m": "<price>"}],
 "embeddings": [{"provider": "openai_compat", "model": "<embedding model>", "input_per_1m": "<price>"}],
 "whatsapp": {"billable_statuses": ["success"],
              "templates": [{"name": "<approved template name>", "category": "<category>"}],
              "rules": [{"markets": ["<country calling code>"], "message_kind": "free_form",
                         "price_per_message": "<price>"},
                        {"markets": ["<country calling code>"], "message_kind": "template",
                         "category": "<category>", "price_per_message": "<price>"}]}}
```

AI prices are per 1M tokens (strings or numbers; both are read as exact decimals). A call is priced by the model the
provider says it served, else by the model Duka asked for; an exact entry beats a prefix entry and the longest prefix
wins. Without the file, or for a model it does not list, the call is still recorded, with `cost_micros` NULL
(unpriced). A failed call costs 0.

Embeddings prices (`embeddings`, optional) are per 1M input tokens and matched the same way (served model first,
exact before prefix, per provider; `llm` entries never price an embeddings request). A request whose provider
reported no tokens is unpriced; a failed request costs 0. Every cost is stored in millionths of the currency,
rounded half to even per row: a short request (a search query) can cost less than half a millionth and is then
stored as 0 while its tokens are kept, so price a total from the summed tokens per model and `price_version`, not
by adding rounded per-row costs.

WhatsApp prices are per message, in the file's `currency`, chosen by market, kind and (for a template) category. The
category is the operator's: declare each template's category under `templates`; a template that is not declared, a
market without a rule, or an unknown market stays unpriced. `billable_statuses` says which outcomes of a real send
incur the price (`success`, `failed`). Everything else is priced 0 or left unpriced on purpose: other outcomes of a real
send cost 0, a simulated send costs 0 (nothing reached WhatsApp), a late failure adds 0, an `unknown` send is unpriced
(whether it was delivered is not known) and an inbound message has no price.

A file that cannot be read or is not a valid price list (a typo in a key, a negative price, a template rule without a
category, the same market priced twice) stops the backend from starting, with the reason. If a row cannot be written,
the customer's reply still goes out.

## Runaway Conversation Guard

Design and status: `docs/P1_RUNAWAY_GUARD.md`. Every real model call and every provider HTTP attempt is reserved in
`ai_usage_counters` before it is made: per inbound message (the webhook event, across its retries), per customer and
per tenant, in fixed UTC hour and day buckets. The table is operational (mutable, purged); the usage history stays
in `usage_events`.

| Setting | Default | Meaning |
|---|---|---|
| `AI_GUARD_MESSAGE_MODE` | `enforce` | the per-message budget: `off` = not counted; `observe` = counted, a reservation past the limit logged (`ai_guard.decision`, `would_block`) but never refused; `enforce` = refused |
| `AI_GUARD_CUSTOMER_MODE` | `observe` | per-customer limits: switch to `enforce` once limits are chosen |
| `AI_GUARD_TENANT_MODE` | `observe` | per-tenant limits: switch to `enforce` once limits are chosen |
| `AI_GUARD_TENANT_OVERRIDES` | empty | operator-set per-shop limits, JSON: `{"<business id>": {"calls_per_hour": N, "calls_per_day": N, "attempts_per_hour": N, "attempts_per_day": N}}`; a key left out keeps the default, 0 lifts that limit for the shop; an invalid value stops the start; change it and restart |
| `AI_GUARD_MESSAGE_CALLS`, `AI_GUARD_MESSAGE_ATTEMPTS` | `0` = one processing attempt | model calls / HTTP attempts one inbound message may use across all its retries (default: 1 summary + `AGENT_MAX_TOOL_ITERATIONS` calls, × `LLM_MAX_ATTEMPTS`) |
| `AI_GUARD_{CUSTOMER,TENANT}_{CALLS,ATTEMPTS}_PER_{HOUR,DAY}` | `0` = no limit | per customer / per tenant and UTC hour / day. Choose them from observe-mode data (below), never by guess |

Choosing limits from observe mode (read-only queries; `ai_usage_counters` keeps hour buckets 2 days, day buckets 8):

```sql
-- busiest tenant-hours and customer-hours in the window kept
SELECT scope, period, period_start, max(calls) AS calls, max(attempts) AS attempts
  FROM ai_usage_counters WHERE scope IN ('tenant', 'customer') GROUP BY 1, 2, 3 ORDER BY calls DESC LIMIT 20;
-- reservations that went past a configured limit (observe) or were refused (enforce)
SELECT business_id, scope, period, period_start, over_limit, denied FROM ai_usage_counters
 WHERE over_limit > 0 OR denied > 0 ORDER BY updated_at DESC;
```

Reconciling with the ledger: for a tenant and UTC hour, `ai_usage_counters.calls` is at least the number of
`usage_events` rows of kind `llm_call` in that hour (reservations are made before calls, and a ledger write can fail);
`attempts` is at least the sum of their `attempts`. A difference means calls that never returned a ledger row
(a crash between reservation and call, or a failed ledger write: `usage.record_failed` in the logs).

When a reservation is refused (enforce mode) the turn ends `limited`: the customer gets the facts the tools
already returned or a short reply that claims nothing and offers a person, the conversation is flagged, and the
owner gets one `assistant_limited` alert per tenant and UTC window. If the counters cannot be used, an enforcing
scope refuses (fail closed): customers then get that reply until the database is healthy again. Rolling back is a
setting: `AI_GUARD_MESSAGE_MODE=observe` (or `off`); the counters and the ledger keep their history.

Metrics: `duka_ai_guard_reserved_current_hour{unit}`, `duka_ai_guard_busiest_tenant_calls_current_hour`,
`duka_ai_guard_over_limit_24h{scope}`, `duka_ai_guard_denied_24h{scope}`. Logs: `ai_guard.decision`,
`ai_guard.store_error` (in observe mode the call goes ahead), `ai_usage_counters.purged`.

## Backups

```bash
scripts/backup.sh                       # pg_dump (custom format) + .manifest (row counts) + .sha256, 14-day retention
VERIFY_EMAIL=owner@shop.rw VERIFY_PASSWORD=... scripts/verify_restore.sh   # prove the newest backup restores
```

Schedule on the server (crontab):

```
15 2 * * *  cd /opt/duka && scripts/backup.sh >> /var/log/duka-backup.log 2>&1
45 2 * * 0  cd /opt/duka && scripts/verify_restore.sh >> /var/log/duka-backup.log 2>&1
```

**Off-site copy (required for production):** a backup on the same server does not survive losing that server.
Copy `backups/` to storage outside the VPS after each run (e.g. `rclone copy backups/ remote:duka-backups` to
an S3-compatible bucket or Backblaze B2). This needs a storage account — see "External dependencies" in
`docs/MILESTONES.md`.

## Restore

```bash
scripts/restore.sh backups/duka-<ts>.dump                 # into a NEW database duka_restore_<ts> (inspect it)
scripts/restore.sh backups/duka-<ts>.dump --replace-live  # disaster recovery
```

`--replace-live` asks for confirmation, stops backend + frontend, **renames** (does not drop) the current database
to `<name>_before_restore_<ts>`, restores, and starts the services. Messages that arrived after the backup and
before the restore are in the renamed database. Run `scripts/verify_restore.sh` on a backup first.

## Common incidents

| Symptom | Check | Action |
|---|---|---|
| `/readyz` 503, `workers: not running` | `docker compose logs backend` | `docker compose restart backend` |
| `inbound_backlog` growing | `/metrics` `duka_inbound_oldest_pending_seconds`, logs `webhook.retry` | usually LLM or DB slowness; messages are safe in `webhook_events` and resume automatically |
| `dead_letters_24h` > 0 | logs `webhook.dead` (error is scrubbed) | fix the cause, deploy, then `docker compose exec backend python -m app.cli requeue-dead` |
| `send_failures_1h` > 0 | conversation in the inbox shows the error | expired/invalid WhatsApp token → owner reconnects the number (WhatsApp page); 24 h window closed → the owner must wait for the customer or use a template |
| `agent_errors_1h` high | logs `agent.error` | LLM provider outage or key problem: customers get the fallback; two failures in a row hand the chat to the owner. `python -m app.cli llm-check` |
| Logs `agent.turn_budget_exhausted` | the agent run in the conversation debugger (a `deadline` step lists skipped tool calls) | a turn used its whole `AGENT_TURN_TIMEOUT_SECONDS` (model calls and tools together); the customer got the facts the tools had already returned, or the fallback if none. Frequent: a slow model or slow embeddings |
| `assistant_limited` alert, logs `ai_guard.decision` with `refused` | `agent_runs` with status `limited` (the `guard` step names the scope and limit); `ai_usage_counters` (Runaway Conversation Guard) | a message, customer or the shop used up its AI allowance: reply to the flagged conversations by hand. A message budget is used up only by repeated processing failures (look for `webhook.retry`); after fixing the cause, `requeue-dead` gives requeued messages a fresh budget |
| Logs `ai_guard.store_error` with `fail_closed: true` | database health; `ai_usage_counters` | the guard could not use its counters and refused AI calls rather than spend without a limit; customers get the short reply. Fix the database; to keep AI running meanwhile, set `AI_GUARD_MESSAGE_MODE=observe` |
| Logs `embeddings.unavailable` | `EMBEDDING_PROVIDER` / its key; provider status | the embeddings service failed or the turn had no time left for it; searches still work, ranked by words only (vectors never admit results, so nothing wrong is shown) |
| Many `agent.ungrounded` | agent runs in the conversation debugger | the model states facts the tools didn't return; customers still get correct server-rendered answers. Review the prompt/model choice (weekly review: `docs/EXTERNAL_VALIDATION.md` › 6) |
| `customer_rate_limited` alert, logs `inbound.rate_limited` | the flagged conversation (extra messages are marked `rate_limited`) | a customer sent more than 30 messages a minute; the extra ones got no automatic answer. Reply by hand if it is a real customer; if it is spam or a bot loop, use **Take over** so the assistant stops replying. One alert per conversation per day |
| Owner forgot password | — | `docker compose exec backend python -m app.cli reset-password --email owner@shop.rw` (signs out every session) |
| New pilot shop | — | `docker compose exec backend python -m app.cli create-business --name "Shop" --email owner@shop.rw` |
