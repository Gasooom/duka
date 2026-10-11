# Pilot checklist (one supervised merchant, Render)

The order of work for the first pilot. Commands and pass conditions live in `docs/EXTERNAL_VALIDATION.md` (§ below),
`docs/DEPLOYMENT.md` › R (Render) and `docs/OPERATIONS.md`; this file only orders them. Record date, person and result
next to each item. An item is done when its pass condition holds, not when its command has run.

Status 2026-10-11: code at `f20d376`, CI run #20 green. Nothing external has been tested: no Render service, no
WhatsApp number, no production AI key, no merchant. The pilot is not production-ready.

**Decisions so far:** hosting on **Render** (`render.yaml`: `duka-api`, `duka-dashboard`, PostgreSQL with pgvector,
one instance each). Languages: **as the merchant needs**, among English, Arabic (MSA and Sudanese), Kinyarwanda,
French and Swahili. A language is pilot-ready only after the checks in step 6.

**Secrets** (tokens, app secret, API keys, `ENCRYPTION_KEY`, passwords) go only into a password manager and Render's
secret environment variables. Never into a chat, ticket, log, commit or the repository.

## Language readiness (what exists today)

| Language | Customer texts (`app/i18n.py`) | False-claim checks (grounding) | Native-speaker review | Real-model check |
|---|---|---|---|---|
| English | yes | yes | n/a | developer key only (2026-10-05/06) |
| Arabic (MSA, Sudanese) | yes | yes (incl. Arabic digits) | not done | developer key only |
| Kinyarwanda, Swahili, French | yes | yes (`LOCAL_CLAIMS`) | not done (`docs/MULTILINGUAL_GROUNDING_REVIEW.md`: not reviewed) | developer key only |

## Order of work

**1. Free, no accounts (start now)**
- [ ] **Merchant and languages:** which languages the merchant's customers write in; that list decides the reviews
  in step 6.
- [ ] **Merchant data:** catalog CSV (`name, price, category, sku, stock_quantity`), delivery zones and fees, the exact
  payment-instructions text, the owner's WhatsApp number, business hours. Optional free check: import the CSV into
  the local development stack and fix the rows it reports.
- [ ] **Operating procedure,** signed by merchant and operator (§1, §5): who watches the dashboard and handoffs, how
  the owner sees alerts before an alert template is approved, refunds and cancellations (manual).
- [ ] **Privacy and legal** (§5): privacy notice; processor terms for the AI provider and Meta, which receive customer
  message text; who has dashboard access.
- [ ] **Price list** for `USAGE_PRICING_FILE` from each provider's current price page (`docs/OPERATIONS.md` › Usage
  metering). Not needed to run the pilot; needed to measure cost per merchant.

**2. Accounts and credentials (free to open; usage may cost)**
- [ ] **Meta WhatsApp test number, first: it gates step 7.** <https://developers.facebook.com/apps> → create an app
  → add WhatsApp → *API Setup*; guide <https://developers.facebook.com/docs/whatsapp/cloud-api/get-started>.
  Keep in the password manager: the test number's Phone number ID and WhatsApp Business Account ID, the App secret,
  a verify token you generate, a permanent system-user access token (the *API Setup* token is temporary), the test
  phones registered as recipients, and the current Graph API version from
  <https://developers.facebook.com/docs/graph-api/changelog/versions>.
- [ ] **AI provider:** production account and key, a dated model id from the provider's model list, a monthly spend
  limit set in the provider's console.
- [ ] **Render account** with billing; read the plan prices before creating anything.
- [ ] **Uptime monitor** account (a free tier is enough to poll one URL).

**3. Configuration change (needs authorization to commit)**
- [ ] `render.yaml` fixes `LLM_MODEL=gpt-4o-mini` (a floating alias) and `WHATSAPP_API_VERSION=v21.0` (October 2024).
  Pin the dated model and the current Graph API version there before the first deploy.

**4. Render deploy (needs authorization; paid)**
- [ ] Create the Blueprint and enter the `sync: false` values (`docs/DEPLOYMENT.md` › R): the database's internal URL,
  `PUBLIC_BASE_URL`, `CORS_ORIGINS`, a Fernet `ENCRYPTION_KEY` (keep an offline copy), `LLM_API_KEY`, and a random
  `WHATSAPP_APP_SECRET` until Meta is connected. Production mode refuses to start without a real AI key.
- [ ] **Pass:** every check in `docs/DEPLOYMENT.md` › R "Verify after the first deploy" (`/healthz` and `/readyz` 200,
  `/docs` 404, registration 403, unsigned webhook 401, security headers, dashboard `/login` 200, `/api/auth/me` 401
  through the dashboard) and the client-address check (`TRUSTED_PROXY_HOPS`).
- [ ] **Rollback:** redeploy the previous deploy in Render; across a migration, restore the database (step 5).

**5. Backups and monitoring, before anyone outside uses it (needs authorization)**
- [ ] Render managed backups active; record retention and point-in-time recovery. **Pass:** a restore into a separate
  Render database is complete (tables and row counts), and its duration is recorded (§4).
- [ ] Uptime monitor polls `https://<duka-api>/readyz` every minute. **Pass:** suspending the API raises an alert.
- [ ] Logs: `ERROR`, `webhook.dead` and `agent.error` reach the operator (`docs/OPERATIONS.md` › Logs).

**6. AI and languages (real AI calls: needs authorization; small cost)**
- [ ] `python -m app.cli llm-check` in the Render Shell. **Pass:** exit 0 and the pinned model served (§3).
- [ ] Real-model evaluation (§3). **Pass:** no critical failure, no regression against the baseline.
- [ ] For **each language in scope**: a person reads that language's transcripts (no false order, payment, delivery
  or cart claim; no correct reply rejected), and a native speaker reviews its checks and customer texts
  (`docs/MULTILINGUAL_GROUNDING_REVIEW.md`; Arabic texts in `app/i18n.py` have no review list yet). A language
  without both is not offered to the merchant's customers.

**7. WhatsApp with the test number and test phones only (real provider traffic: needs authorization)**
- [ ] Webhook: callback `https://<duka-api>/webhooks/whatsapp`, the verify token, `messages` subscribed. **Pass:**
  Meta's "Verify and save" succeeds.
- [ ] Signature: the real App secret replaces the placeholder. **Pass:** an unsigned POST gets 401; a test phone's
  message is processed with no `webhook.bad_signature` in the logs.
- [ ] Inbound, reply and status events: **pass** when the message appears in the inbox, the reply arrives, and the
  conversation shows sent → delivered → read.
- [ ] End-to-end order (§2): search, cart, address, YES; accept; manual payment. **Pass:** the order is in the
  dashboard, the owner alert arrives, the customer gets each update.

**8. Merchant onboarding (needs authorization; real merchant data)**
- [ ] `python -m app.cli create-business` in the Render Shell; the owner changes the generated password.
- [ ] Setup checklist all green; catalog imported; `python -m app.cli inventory-check` exits 0.
- [ ] Dry run on the merchant's own phone; the merchant accepts and records a payment unaided.
- [ ] Connect the merchant's real number only after steps 4–7 pass.

**9. Supervised pilot**
- [ ] Daily, weekly and monthly routine (§6), including `inventory-check` and `usage-report`.
- [ ] AI customer and shop limits: observe for the first days, then set them above the observed peaks (§5).
- [ ] On any false price, stock, order or payment claim: pause the AI in Settings, add a regression test, fix.
- [ ] Evidence log for the four pilot questions: routine tasks done without a developer; AI accuracy per language;
  real cost per merchant against what the merchant will pay; behaviour during retries, outages and busy hours.

## Open blockers

| Blocker | Who | Blocks the pilot |
|---|---|---|
| Meta app, test number and credentials | you, Meta | yes |
| AI provider key, dated model, spend limit | you, provider | yes (production mode needs it) |
| Render account and billing | you | yes |
| `render.yaml` model and Graph API version | commit, your authorization | yes |
| A merchant, their data and the signed operating procedure | merchant, you | yes |
| Privacy and legal sign-off | you | yes, for real customers |
| Native review and transcript reading per language in scope | reviewers | yes, for each non-English language offered |
| Uptime monitor | you | yes, before public use |
| Owner-alert template approval | Meta | no (operating procedure covers it) |
| Price list | you | no (needed to measure cost) |

## Known documentation nits
- `docs/DEPLOYMENT.md` §3 says 24 checks; `deploy/verify_deployment.sh` has 25 (self-hosted only).
- `docs/EXTERNAL_VALIDATION.md` §5: the dashboard's setup checklist has 8 checks (WhatsApp, products, delivery,
  payment instructions, owner alerts, hours, AI on, platform AI); the owner-alert template is a Settings field.
- Fixed 2026-10-11: the backup cron lines (`docs/OPERATIONS.md` › Backups, `docs/DEPLOYMENT.md` §6) now target the
  production stack and read the database settings from the env file, with no password in the crontab.
