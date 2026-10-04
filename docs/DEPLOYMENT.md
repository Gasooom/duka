# Deploying Duka (pilot production)

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
