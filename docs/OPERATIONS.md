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

Every call the assistant makes to a real AI model (reply turns and conversation summaries) is one row in
`usage_events`, for the shop whose conversation made it: the model the provider served and the one Duka asked for,
input/output tokens, attempts (provider retries), success or error, and an estimated cost. Each row is written in
its own transaction right after the call, so it is kept when the turn that made the call fails and is retried, and
when conversations, customers or processed webhook events are deleted later. Rows can only be inserted: a database
trigger refuses UPDATE and DELETE, and a shop that has usage cannot be deleted. `agent_runs.llm_calls` is not a
usage figure: it also counts the offline rules engine. Nothing is recorded for the rules engine, evaluation runs
(`evals/`) or `python -m app.cli llm-check`. WhatsApp messages and embeddings are not metered yet.

Accepted limitation (for now): metering is best effort, with no queue behind it. If the backend process dies in the
moment between the provider's answer and the commit of its row, or the row cannot be written, that one call is
missing from `usage_events`. A failed write is logged as `usage.record_failed` with the row's fields. A turn that
fails and is retried never removes a row that was written.

The cost comes from the operator's price list, a JSON file named by `USAGE_PRICING_FILE`. Duka ships no prices:
copy them from the provider's pricing page, and change `version` whenever a price changes (each row keeps the cost
and the version it was recorded with; earlier rows are never re-priced).

```json
{"version": "2026-10-08", "currency": "USD",
 "llm": [{"provider": "openai_compat", "model": "<model>", "input_per_1m": "<price>", "output_per_1m": "<price>"},
         {"provider": "openai_compat", "model": "<model name prefix>", "match": "prefix",
          "input_per_1m": "<price>", "output_per_1m": "<price>"}]}
```

Prices are per 1M tokens (strings or numbers; both are read as exact decimals). A call is priced by the model the
provider says it served, else by the model Duka asked for; an exact entry beats a prefix entry and the longest prefix
wins. Without the file, or for a model it does not list, the call is still recorded, with `cost_micros` NULL
(unpriced). A failed call costs 0. A file that cannot be read or is not a valid price list stops the backend from
starting, with the reason. If a row cannot be written, the customer's reply still goes out.

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
| Many `agent.ungrounded` | agent runs in the conversation debugger | the model states facts the tools didn't return; customers still get correct server-rendered answers. Review the prompt/model choice (weekly review: `docs/EXTERNAL_VALIDATION.md` › 6) |
| `customer_rate_limited` alert, logs `inbound.rate_limited` | the flagged conversation (extra messages are marked `rate_limited`) | a customer sent more than 30 messages a minute; the extra ones got no automatic answer. Reply by hand if it is a real customer; if it is spam or a bot loop, use **Take over** so the assistant stops replying. One alert per conversation per day |
| Owner forgot password | — | `docker compose exec backend python -m app.cli reset-password --email owner@shop.rw` (signs out every session) |
| New pilot shop | — | `docker compose exec backend python -m app.cli create-business --name "Shop" --email owner@shop.rw` |
