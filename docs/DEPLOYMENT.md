# Deploying Duka (pilot production)

Two supported targets: **Render** (managed: one web service + one PostgreSQL, section R below) or **one Linux
server with Docker Compose** (sections 1–8). Both use the same backend image and the same production guards.
Local development is different: `docker compose up` (README) runs a development image with test tooling, publishes Postgres on 5432 with a development password, migrates on start and serves on port 8000.

One small Linux server runs everything with Docker Compose: Caddy (automatic HTTPS) → FastAPI backend +
Next.js dashboard → PostgreSQL. Only Caddy is reachable from the internet. No Kubernetes, Redis or queue.

## What you need (external — cannot be created from the repository)

| Item | Notes |
|---|---|
| A server | Ubuntu 24.04, 2 vCPU / 4 GB RAM / 40 GB disk is plenty for the pilot (e.g. a VPS in a nearby region) |
| A domain | e.g. `duka.example.rw`, with a DNS **A record** pointing to the server's IP |
| LLM API key | any OpenAI-compatible provider (`LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`) |
| Meta app | WhatsApp Business: app secret, a verify token you choose, the shop's phone number id and a permanent access token |
| Uptime monitor | any service that can poll `https://<domain>/readyz` and alert you by SMS/e-mail |
| Off-site storage | an S3-compatible / Backblaze B2 bucket for backup copies |

## 1. Server

```bash
# as root on a fresh Ubuntu server
apt-get update && apt-get install -y ca-certificates curl git ufw openssl
curl -fsSL https://get.docker.com | sh
ufw allow OpenSSH && ufw allow 80/tcp && ufw allow 443/tcp && ufw allow 443/udp && ufw --force enable
git clone <your repository> /opt/duka && cd /opt/duka
```

PostgreSQL, the API and the dashboard publish no ports; the firewall only needs SSH, 80 and 443.

## 2. Configuration (separate per environment)

```bash
cp deploy/.env.production.example deploy/.env.production
chmod 600 deploy/.env.production
```

Fill in every value (generation commands are in the file). Rules:
- **Development** uses `/.env` (from `.env.example`), **staging** `deploy/.env.staging`, **production**
  `deploy/.env.production`. Never share secrets, databases or WhatsApp numbers between them.
- **Never copy production customer data into development or staging.** Restore drills use a scratch database on
  the production server (`scripts/verify_restore.sh`), which is dropped afterwards.
- The backend refuses to start in production with a weak `JWT_SECRET`, missing/invalid `ENCRYPTION_KEY`, no real
  LLM, missing WhatsApp secrets, the default database password or a non-https public URL — the error lists them.

## 3. Deploy and verify

```bash
deploy/deploy.sh                 # build, start, wait for https://<domain>/readyz
deploy/verify_deployment.sh      # 24 black-box checks (HTTPS, headers, closed registration, ports, secrets in logs…)
docker compose --env-file deploy/.env.production -f deploy/docker-compose.prod.yml exec backend python -m app.cli llm-check
```

`llm-check` must print `tool_call: search_products(...)` (exit 0) before the AI milestone counts as done.

## 4. Onboard the pilot shop

```bash
C="docker compose --env-file deploy/.env.production -f deploy/docker-compose.prod.yml"
$C exec backend python -m app.cli create-business --name "Shop name" --email owner@shop.rw   # prints a password once
```

The owner signs in at `https://<domain>`, follows the **setup checklist** on the overview (WhatsApp number,
products, delivery zones, payment instructions, alert number, hours), and changes the password under Account.

## 5. Meta webhook

Meta App → WhatsApp → Configuration:
- Callback URL: `https://<domain>/webhooks/whatsapp`
- Verify token: the value of `WHATSAPP_VERIFY_TOKEN`
- Subscribe to the `messages` field.

Then in the dashboard → WhatsApp: connect the shop's `phone_number_id` with the permanent access token (mode
`cloud`; simulated numbers are refused in production). Send a WhatsApp message from a phone to the shop's number
and watch it appear in the inbox.

## 6. Monitoring and backups

- Uptime monitor: `GET https://<domain>/readyz` every minute; alert on non-200 (503 = customers affected).
  Optionally poll `/readyz?details=1` with `Authorization: Bearer $OPS_TOKEN` and alert on `degraded`.
- Backups (host crontab, `crontab -e`):

```
15 2 * * *  cd /opt/duka && COMPOSE="docker compose --env-file deploy/.env.production -f deploy/docker-compose.prod.yml" POSTGRES_USER=duka POSTGRES_DB=duka scripts/backup.sh >> /var/log/duka-backup.log 2>&1
45 2 * * 0  cd /opt/duka && COMPOSE="docker compose --env-file deploy/.env.production -f deploy/docker-compose.prod.yml" POSTGRES_USER=duka POSTGRES_DB=duka POSTGRES_PASSWORD=<db password> scripts/verify_restore.sh >> /var/log/duka-backup.log 2>&1
30 2 * * *  rclone copy /opt/duka/backups remote:duka-backups >> /var/log/duka-backup.log 2>&1
```

See `docs/OPERATIONS.md` for logs, metrics, restore and incident handling.

## 7. Staging

Same files with `deploy/.env.staging` (its own domain, e.g. `staging.duka.example.rw`, own secrets, a Meta
**test** number, `COMPOSE_PROJECT_NAME=duka-staging`), ideally on a separate server:

```bash
DUKA_ENV_FILE=.env.staging deploy/deploy.sh && DUKA_ENV_FILE=.env.staging deploy/verify_deployment.sh
```

## 8. Upgrades and rollback

```bash
scripts/backup.sh                      # (with COMPOSE=... as above) before any upgrade
git fetch && git checkout <release>    # e.g. a tag
deploy/deploy.sh && deploy/verify_deployment.sh
```

Migrations run automatically on backend start and are forward-only. To roll back code across a migration,
check out the previous release and restore the pre-upgrade backup (`scripts/restore.sh <dump> --replace-live`).

## R. Render (managed alternative)

Deploying is not the same as being production ready: the pilot preconditions in
`docs/VALIDATION_REPORT.md` (section 22) and the checklist in `docs/EXTERNAL_VALIDATION.md` still apply.

**Architecture** (`render.yaml`, Frankfurt):

```
shop owner's browser ─► Render Web Service "duka-dashboard" (Docker: frontend/Dockerfile, plan Starter, 1 instance)
                            │ HTTPS https://duka-dashboard.onrender.com
                            └─► /api/* forwarded over Render's private network (BACKEND_URL = duka-api's host:port)
GitHub, Meta ─► Render Web Service "duka-api" (Docker: backend/Dockerfile, plan Starter, 1 instance)
                    │ HTTPS https://duka-api.onrender.com (Render terminates TLS)
                    ├─► Render PostgreSQL "duka-db" (PostgreSQL 16 + pgvector), private network only (ipAllowList: [])
                    ├─► OpenAI API
                    └─► Meta WhatsApp Cloud API (later)
```

The API and its WhatsApp/outbox workers (threads inside the API process) run in one service. That is enough for
a supervised one-merchant pilot. Keep **one instance**: the rate limiter is in-process. Nothing is written to
local disk (uploaded documents are stored in the database), so the ephemeral filesystem is fine.

**The dashboard** is a second web service built from `frontend/Dockerfile`. Browsers talk only to it. Its
server-side proxy (`frontend/app/api/[...path]/route.ts`) forwards `/api/*` to the API:
- **Address:** the API's private address comes from the Blueprint (`BACKEND_URL` = duka-api's `host:port`; the
  proxy adds `http://`). No API address or key reaches the browser.
- **Placement:** both services must stay in the same region, and the API must not be on the free plan. Free
  services cannot receive private-network traffic.
- **Sign-in throttling:** browsers reach the API through this proxy, which does not pass on the browser's address.
  The API therefore sees the dashboard's internal address for every dashboard request. The 20-per-minute sign-in
  limit is shared by everyone who signs in through the dashboard, and `auth.login_failed` logs show the dashboard's
  address. The per-account lock (5 wrong passwords) works as before. The self-hosted stack does not have this
  limitation, because Caddy sends `/api/*` straight to the backend.

**Not the free plan:**
- **Free web services sleep when idle.** The in-process workers would stop, and the first WhatsApp webhook would
  hit a cold start.
- **Free services have no pre-deploy command**, which runs the migrations.
- **Free databases expire.**

Use Starter (web) and a paid PostgreSQL plan (the Blueprint says `basic-256mb`; check the plan names and backup
retention in Render's pricing page before creating it).

**Create it:**
1. Render → New → Blueprint → this repository (`render.yaml`).
2. Enter the `sync: false` values (table below).
3. Create. The API's first deploy:
   - builds the image;
   - runs the pre-deploy command `alembic upgrade head` (once, before the new version starts);
   - starts uvicorn on `$PORT`;
   - routes traffic once `GET /healthz` answers.

   The dashboard is built from `frontend/Dockerfile` and receives traffic once `GET /login` answers.

`autoDeploy` is off for both services: deploy them deliberately, together, after the eval suite passes.

**Environment variables:**

| Variable | How it gets its value |
|---|---|
| `DATABASE_URL` | Render: the database's *internal* connection string (`postgresql://…` is accepted; the app uses psycopg 3) |
| `JWT_SECRET`, `WHATSAPP_VERIFY_TOKEN`, `OPS_TOKEN`, `PAYMENT_WEBHOOK_SECRET` | generated by Render |
| `PUBLIC_BASE_URL` | **you**: `https://<service name>.onrender.com` (shown on the service page) or your own domain; must be https |
| `CORS_ORIGINS` | **you**: the dashboard's URL, `https://duka-dashboard.onrender.com` (shown on its service page) or your own domain |
| `BACKEND_URL` (dashboard) | Render: duka-api's `host:port` on the private network (`fromService` … `hostport`) |
| `ENCRYPTION_KEY` | **you**: a Fernet key, e.g. `python -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"` (Render's generated values are not Fernet keys). Keep a copy outside Render: tokens encrypted with it cannot be read without it |
| `LLM_API_KEY` | **you**: the OpenAI key |
| `WHATSAPP_APP_SECRET` | **you**: the Meta app secret. The app refuses to start without one. Until Meta is connected, use a random value (`python -c "import secrets;print(secrets.token_urlsafe(32))"`) and replace it with the real app secret when connecting Meta |
| everything else | fixed in `render.yaml`: `APP_ENV=production`, `RUN_MIGRATIONS_ON_START=false`, `FORWARDED_ALLOW_IPS=*`, `TRUSTED_PROXY_HOPS=1`, `LLM_PROVIDER/BASE_URL/MODEL`, `EMBEDDING_PROVIDER=hash`, `ENABLE_DEV_TOOLS=false`, `ALLOW_PUBLIC_REGISTRATION=false`, … |

Never commit any of these values. `.env` and `deploy/.env.*` are git-ignored, and nothing is baked into the image.
At startup the backend refuses production with a weak or missing secret, a non-https URL, no real LLM, or the
default database password.

**Migrations:**
- Only the pre-deploy command migrates. A failed migration stops the deploy, and the previous version keeps
  serving.
- Never set `RUN_MIGRATIONS_ON_START=true` with more than one instance.
- Migration 0001 runs `CREATE EXTENSION IF NOT EXISTS vector`. If the first pre-deploy fails on that line, enable
  pgvector once for the database (Render supports pgvector) and redeploy.

**Health check:**
- **`/healthz`** (liveness: the process serves requests) is Render's health check.
- **`/readyz`** is for an external uptime monitor. It reports `down` when, for example, the WhatsApp backlog is
  stuck behind an OpenAI outage, and restarting the service would not fix that.
- `/readyz?details=1` and `/metrics` need `Authorization: Bearer $OPS_TOKEN`.

**Protections in the app itself** (Render has no Caddy in front; on the self-hosted stack Caddy sets the same
headers too):
- Security headers on every response (`nosniff`, `X-Frame-Options: DENY`, referrer and permissions policies, a
  `default-src 'none'` CSP, `Cache-Control: no-store` on `/api/*`, HSTS in production), no `Server` header, and a
  10 MB request-body limit (`MAX_REQUEST_BODY_BYTES`). Larger requests, webhooks included, get 413: at once when
  they declare their length, otherwise as soon as the limit is passed.
- Sign-in throttling: 20 attempts per minute per client address, and per account a lock after 5 wrong passwords
  (30 s, doubling up to 15 min) whoever is asking. Failed sign-ins are logged (email as a pseudonym, never the
  password) and audited. The client address is the entry `TRUSTED_PROXY_HOPS` from the right of
  `X-Forwarded-For`, so a forged header changes nothing.
- Database limits on every app connection: statements 60 s, lock waits 50 s, idle transactions 120 s
  (`DB_STATEMENT_TIMEOUT_MS`, `DB_LOCK_TIMEOUT_MS`, `DB_IDLE_IN_TRANSACTION_TIMEOUT_MS`). An inbound WhatsApp event
  whose worker dies is retried after 120 s (`WEBHOOK_LEASE_SECONDS`); on a normal shutdown or deploy, unfinished
  events are handed back at once. The app refuses to start if these limits do not fit the 45 s AI turn budget.
- Only the models in `LLM_MODEL` + `LLM_ALLOWED_MODELS` can be chosen per business.
- Changes to payment instructions, prices, WhatsApp numbers, passwords and the AI settings are recorded in the
  append-only `audit_events` table (who, when, before/after; never a password or token).
- WhatsApp's 24-hour window: normal messages are only delivered within 24 hours of the customer's last message
  (after that only Meta-approved templates, which Duka does not send yet). A message to a customer silent for longer
  than `WHATSAPP_WINDOW_HOURS` (23.5) — an order update, payment instructions, a staff reply — is not attempted: it
  is marked failed (`outside_24h_window`), the conversation is flagged and the owner gets one alert per silence.
  The same happens when Meta accepts a message and reports it failed later (error 131047 = window closed). Owner
  alerts that fail later show as failed, with the reason, in the dashboard.
- Orders keep their stock until the owner acts. The owner gets one reminder per order when it has waited
  `ORDER_REVIEW_REMINDER_HOURS` (2) for review, or was accepted `ORDER_PAYMENT_REMINDER_HOURS` (24) ago and is
  still unpaid. Nothing is cancelled or restocked automatically.
- Catalog imports and knowledge uploads run in worker threads: a large one never delays webhooks or health checks.
- Above 30 WhatsApp messages a minute from one customer, the extra messages get no automatic answer (a reply could
  feed a bot loop). They are kept and marked; the conversation is flagged and the owner gets one alert per
  conversation per day (`customer_rate_limited`).
- The AI's replies are checked for false order, payment, delivery and cart claims in Kinyarwanda, French and Swahili
  too, not only English and Arabic. That wording still needs a native speaker's review
  (`docs/MULTILINGUAL_GROUNDING_REVIEW.md`).
- The production image (`INSTALL_DEV=false`) holds only the app and its migrations: no tests, evals or demo seed
  (they are deleted in the build; an earlier image layer still contains them, nothing in them is secret), and the
  seed refuses `APP_ENV=production`. CI builds this image, checks its contents and scans it with Trivy: fixable
  critical vulnerabilities fail the build, everything else is reported (`.trivyignore` documents any exception).

**Verify after the first deploy** (`BASE=https://duka-api.onrender.com`):

```bash
curl -s $BASE/healthz                                   # {"status":"ok"}
curl -s $BASE/readyz                                    # {"status":"ok"}
curl -s -H "Authorization: Bearer $OPS_TOKEN" "$BASE/readyz?details=1"   # database, migrations, workers: ok
curl -s -o /dev/null -w '%{http_code}
' $BASE/docs    # 404 (docs off in production)
curl -s -o /dev/null -w '%{http_code}
' -X POST -H 'content-type: application/json'   -d '{"business_name":"X Y","email":"x@y.rw","password":"password123"}' $BASE/api/auth/register   # 403
curl -s -o /dev/null -w '%{http_code}
' -X POST -d '{}' $BASE/webhooks/whatsapp                  # 401 (unsigned)
curl -s -D - -o /dev/null $BASE/healthz | grep -iE 'nosniff|x-frame-options|strict-transport'   # 3 lines; no "server: uvicorn"
DASH=https://duka-dashboard.onrender.com
curl -s -o /dev/null -w '%{http_code}\n' $DASH/login    # 200
curl -s $DASH/api/auth/me                              # 401 from the API: the proxy reaches it (502 = it does not)
```

Check the client address once. Sign in with a wrong password from your machine directly against the API (`curl -X
POST -H 'content-type: application/json' -d '{"email":"x@y.rw","password":"wrong-password"}' $BASE/api/auth/login`,
not through the dashboard: see above). Then find the `auth.login_failed` line in the service logs. Its `client_ip`
must be your public IP address. If it shows any other address (an
internal 10.x, 172.16–31.x or 192.168.x one, or a proxy's), there is one more proxy hop: set
`TRUSTED_PROXY_HOPS=2` and check again. Never set it higher than the number of proxies, because the entries further
left are written by the client.

Then, in the service's Shell:
- `python -m app.cli llm-check` checks the OpenAI connection.
- `python -m app.cli create-business --name "…" --email …` onboards the pilot shop (prints a generated password
  once).

**Database access** stays private:
- The service reaches it over Render's private network (internal URL).
- For a manual `psql`, use the Shell, or temporarily add your IP to the database's access control. Remove it
  afterwards.

**Backups:** `scripts/backup.sh` targets the Docker Compose topology. On Render, use the database's managed
backups (check the plan's retention and point-in-time recovery), and test a restore into a separate Render
database before the pilot.
