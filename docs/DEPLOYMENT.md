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
`docs/VALIDATION_REPORT.md` (section 22) still apply.

**Architecture** (`render.yaml`, Frankfurt):

```
GitHub ─► Render Web Service "duka-api" (Docker: backend/Dockerfile, plan Starter, 1 instance)
              │ HTTPS https://duka-api.onrender.com (Render terminates TLS)
              ├─► Render PostgreSQL "duka-db" (PostgreSQL 16 + pgvector), private network only (ipAllowList: [])
              ├─► OpenAI API
              └─► Meta WhatsApp Cloud API (later)
```

The API and its WhatsApp/outbox workers (threads inside the API process) run in one service. That is enough for
a supervised one-merchant pilot. Keep **one instance**: the rate limiter is in-process. Nothing is written to
local disk (uploaded documents are stored in the database), so the ephemeral filesystem is fine. The dashboard is
not part of this step.

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
3. Create. The first deploy:
   - builds the image;
   - runs the pre-deploy command `alembic upgrade head` (once, before the new version starts);
   - starts uvicorn on `$PORT`;
   - routes traffic once `GET /healthz` answers.

`autoDeploy` is off: deploy deliberately after the eval suite passes.

**Environment variables:**

| Variable | How it gets its value |
|---|---|
| `DATABASE_URL` | Render: the database's *internal* connection string (`postgresql://…` is accepted; the app uses psycopg 3) |
| `JWT_SECRET`, `WHATSAPP_VERIFY_TOKEN`, `OPS_TOKEN`, `PAYMENT_WEBHOOK_SECRET` | generated by Render |
| `PUBLIC_BASE_URL` | **you**: `https://<service name>.onrender.com` (shown on the service page) or your own domain; must be https |
| `CORS_ORIGINS` | **you**: the dashboard's origin when it is deployed; until then the API URL |
| `ENCRYPTION_KEY` | **you**: a Fernet key, e.g. `python -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"` (Render's generated values are not Fernet keys). Keep a copy outside Render: tokens encrypted with it cannot be read without it |
| `LLM_API_KEY` | **you**: the OpenAI key |
| `WHATSAPP_APP_SECRET` | **you**: the Meta app secret. The app refuses to start without one. Until Meta is connected, use a random value (`python -c "import secrets;print(secrets.token_urlsafe(32))"`) and replace it with the real app secret when connecting Meta |
| everything else | fixed in `render.yaml`: `APP_ENV=production`, `RUN_MIGRATIONS_ON_START=false`, `FORWARDED_ALLOW_IPS=*`, `LLM_PROVIDER/BASE_URL/MODEL`, `EMBEDDING_PROVIDER=hash`, `ENABLE_DEV_TOOLS=false`, `ALLOW_PUBLIC_REGISTRATION=false`, … |

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
```

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
