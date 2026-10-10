# External validation runbook (pilot)

These are the checks Duka still needs before, and during, a supervised one-merchant pilot. Everything that can run
in the repository has been done. Phases 1–3 are verified in CI (GitHub Actions runs `37651903982`, `37679363561`
and `37768899926`, every job green). What remains depends on credentials, a production host, or decisions only the
merchant and operator can make. The list follows the Phase 3 audit's external-validation checklist.

**What each tag means:**

| Tag | Needs | Owner |
|---|---|---|
| **LOCAL** | the repository only | engineer |
| **META** | the Meta app, a WhatsApp Business number and Meta approvals | engineer + merchant |
| **LLM** | the LLM provider key | engineer |
| **HOST** | the production host (self-hosted or Render) | operator |
| **DECISION** | a merchant, legal or operator decision; no code | merchant / operator / legal |

Record the date, the person and the result next to each item. A step is done when its **pass** condition holds,
not when the command has run.

## 1. Before credentials

- [x] **LOCAL — multilingual grounding.**
  - **Do:** in `backend/`, run `pytest tests/test_grounding_multilingual.py` and
    `python -m evals.run --provider adversarial`.
  - **Pass:** all tests pass; the eval shows 0 critical failures; the `grounding-rw/fr/sw-*` cases pass.
  - **Result (2026-10-10, run by Claude Code for the engineer, on `415b9b0` against a disposable PostgreSQL):**
    24 tests passed. Eval suite v1.3.0: 65 passed, 0 failed, 15 skipped (need a real LLM), 0 critical failures;
    `grounding-rw-01..03`, `grounding-fr-01..03` and `grounding-sw-01..03` all pass. This proves the offline checks
    only; the native-speaker review below is still required.
- [ ] **DECISION — pilot languages.** Choose the languages the pilot serves. That choice decides which reviews in
  the next item are mandatory.
- [ ] **DECISION — native-speaker review.**
  - **Do:** for each pilot language, a native speaker goes through `docs/MULTILINGUAL_GROUNDING_REVIEW.md`.
    Do Kinyarwanda and Swahili first; French is lower risk.
  - **Pass:** reviewer and date are recorded in that file, and every correction is first added to the tests.
- [ ] **DECISION — where the dashboard runs.** Choose one:
  - **Self-hosted:** `deploy/docker-compose.prod.yml` with Caddy. The local rehearsal of
    `deploy/verify_deployment.sh` passed 25/25.
  - **Render:** `render.yaml`, which now defines `duka-dashboard`. It has been checked locally against Render's
    published schema and has never been deployed.

  The difference that matters: on Render, every dashboard sign-in reaches the API through the dashboard. They
  therefore share one per-address limit of 20 sign-ins a minute; the per-account lock is unaffected
  (`docs/DEPLOYMENT.md` › R). Record the choice.
- [x] **LOCAL — rate-limit visibility.**
  - **Do:** run `pytest tests/test_rate_limit_visibility.py`.
  - **Pass:** all tests pass.
  - **Result (2026-10-10, run by Claude Code for the engineer, on `415b9b0` against a disposable PostgreSQL):**
    7 tests passed.
  - **What it guarantees:** above 30 messages a minute from one customer, the extra messages are stored and
    marked but not answered automatically. The conversation is flagged, and the owner gets one
    `customer_rate_limited` alert per conversation per day.
- [ ] **DECISION — pilot operating procedure.** Write it down and have the merchant and operator sign it. It must
  cover:
  - who watches the dashboard (attention list, notifications) and how often;
  - who answers handoffs, and how fast;
  - what to do with a `customer_rate_limited` alert (open the conversation; **Take over** if it is spam or a bot
    loop);
  - whether staff may phone or text a customer whose WhatsApp window has closed;
  - how the owner sees alerts until an owner-alert template is approved. Without one, WhatsApp alerts stop once
    the owner has not written to the shop number for 24 hours, so the owner either messages the number daily or
    watches the dashboard at set times.

## 2. After Meta credentials

Use a test number and test phones first, on staging or before the merchant goes live.

- [ ] **META — Graph API version.**
  - **Do:** check Meta's current list of supported Graph API versions and set `WHATSAPP_API_VERSION` to a supported
    one (Render: `render.yaml`; self-hosted: the env file). Do not rely on the repository default (`v21.0`, from
    late 2024), and do not guess.
  - **Pass:** the version in use is on Meta's supported list. Re-run the end-to-end check below after any change.
- [ ] **META — webhook and verify token.**
  - **Do:** in the Meta app, set the callback URL to `https://<api-domain>/webhooks/whatsapp` and the verify token
    to the value of `WHATSAPP_VERIFY_TOKEN`. Subscribe to `messages`. Then connect the shop's number in the
    dashboard › WhatsApp, with the `phone_number_id` and a permanent token (`docs/DEPLOYMENT.md` › 5).
  - **Pass:** Meta's "Verify and save" succeeds.
- [ ] **META — signature verification.**
  - **Do:** replace the placeholder `WHATSAPP_APP_SECRET` with the real app secret.
  - **Pass:** real messages are processed, and the logs contain no `webhook.bad_signature`.
    `curl -X POST -d '{}' https://<api-domain>/webhooks/whatsapp` returns 401.
- [ ] **META — templates.** Submit them for approval. Duka sends no template until one is approved and
  configured.
  - **Owner alert:** a template whose body has **one** parameter, which receives the alert text. Once approved,
    set its name and language in the dashboard › Settings.
  - **Customer updates after 24 hours** (order updates, payment instructions): approval first. Sending them needs
    code that does not exist yet and is not to be written before approval.
- [ ] **META — end to end.**
  - **Do:** from a test phone:
    - ask for a product;
    - add it to the cart;
    - give an address;
    - answer YES to the summary.
  - **Pass:**
    - every reply arrives;
    - the order appears in the dashboard;
    - the owner receives the new-order alert;
    - the conversation view shows deliveries moving from sent to delivered to read.
- [ ] **META — observe error 131047** (window closed).
  - **Do:** Duka itself stops a message after 23.5 hours of customer silence (`WHATSAPP_WINDOW_HOURS`), so Meta's
    own behaviour only shows if you raise that value temporarily, **on staging only**. Message a test customer who
    has been silent for more than 24 hours. Restore the value afterwards.
  - **Record:** whether Meta rejects the message at once (an immediate 131047) or accepts it and reports it failed
    later in a status webhook. Both are handled:
    - the message is marked failed with `outside_24h_window`;
    - the conversation is flagged;
    - the owner is alerted once.

## 3. After LLM credentials

- [ ] **LLM — connection.**
  - **Do:** run `python -m app.cli llm-check` (from the API's Shell, or `docker compose exec backend …`).
  - **Pass:** exit code 0 (the model called the tool).
  - **Record:** the `model=` value it prints. That is the model the provider actually served.
- [ ] **LLM + DECISION — pin a dated model.**
  - **Do:** set `LLM_MODEL` to a dated model version from the provider instead of a floating alias, and keep
    `LLM_ALLOWED_MODELS` consistent with it. Do not guess the name; take it from the provider's model list.
  - **Pass:** `llm-check` reports the pinned model as the served model.
- [ ] **LLM — real-model evaluation in the pilot languages.**
  - **Do:** in `backend/`, run
    `python -m evals.run --provider openai_compat --out evals/reports/<model>-<date>.json`. It runs on the
    scratch eval database and includes the cases that need a real model.
  - **Pass:**
    - no critical failures;
    - no regression against `evals/baselines/openai_compat.json`. That baseline is from suite v1.2.0 (67/71);
      cases added since have no baseline yet.
    - a person has read the rw/fr/sw transcripts and found:
      - no correct reply rejected (look at the `ungrounded` turns);
      - no false order, payment, delivery or cart claim sent.
  - **Then:** update the baseline with `--update-baseline` only after that review.
- [ ] **LLM — embeddings usage** (only if the pilot sets `EMBEDDING_PROVIDER=openai_compat`).
  - **Do:** add one product in the dashboard and run one product search (a customer message, or the dev
    simulator), then read the shop's `usage_events` rows of kind `embedding` (`docs/OPERATIONS.md` › Usage metering).
  - **Pass:** one row per request (`product`, then `product_search`), `status` success, `model` the served model.
  - **Record:** whether the provider reports `input_tokens`. If it does not, embeddings rows stay unpriced.

## 4. Before deployment (production host)

- [ ] **HOST — deployment verification on the real host.**
  - **Self-hosted:** `deploy/verify_deployment.sh` must pass every check.
  - **Render:** run the verification list in `docs/DEPLOYMENT.md` › R, including the dashboard checks: `/login`
    returns 200, and `/api/auth/me` returns 401 from the API (502 means the proxy cannot reach it).
- [ ] **HOST — proxy hop count.** Make one wrong-password sign-in directly against the API
  (`docs/DEPLOYMENT.md`).
  - **Pass:** `client_ip` in `auth.login_failed` is your public address. Otherwise, adjust `TRUSTED_PROXY_HOPS`
    and check again.
- [ ] **HOST — backups.**
  - **Self-hosted:** `scripts/backup.sh` runs on a schedule, with an off-site copy (`docs/OPERATIONS.md` ›
    Backups).
  - **Render:** managed backups are active on the database plan. Note their retention.
- [ ] **HOST — restore drill.**
  - **Self-hosted:** `scripts/verify_restore.sh` on the newest backup.
  - **Render:** restore into a separate database and check that the data is complete.
  - **Pass:** the restore succeeds. Record how long it took.
- [ ] **HOST — monitoring.**
  - **Do:** an external uptime monitor polls `https://<api-domain>/readyz` every minute and alerts the operator on
    anything other than 200. Optionally poll `/readyz?details=1` with `OPS_TOKEN` and alert on `degraded` too.
  - **Pass:** a deliberate stop of the API raises an alert.
- [ ] **HOST — logs and alerts.**
  - **Do:** confirm the host's log retention, and route `ERROR`, `webhook.dead` and `agent.error` lines to the
    operator (queries in `docs/OPERATIONS.md` › Logs).
  - **Pass:** a test error reaches the operator.

## 5. Before the first merchant

- [ ] **HOST + DECISION — AI usage limits.** The per-message budget enforces by default; customer and tenant
  limits do not exist until they are chosen.
  - **Do:** with real traffic in observe mode (a staging run or the first supervised days), read the busiest
    customer-hours and tenant-hours (`docs/OPERATIONS.md` › Runaway Conversation Guard), set
    `AI_GUARD_CUSTOMER_*` and `AI_GUARD_TENANT_*` above those peaks with a margin the owner approves, then switch
    `AI_GUARD_CUSTOMER_MODE` and `AI_GUARD_TENANT_MODE` to `enforce`.
  - **Pass:** the limits and the data they came from are recorded here; `duka_ai_guard_over_limit_24h` was 0
    for normal traffic before switching.
- [ ] **HOST — onboarding.** Run `python -m app.cli create-business --name "…" --email …`. It prints a generated
  password once. Public registration stays closed.
- [ ] **DECISION — setup checklist.** The dashboard home shows these checks; every one must be green:

  | Setting | Where | Type |
  |---|---|---|
  | payment instructions (exact wording customers receive) | Settings | DECISION |
  | owner's WhatsApp number for alerts | Settings | DECISION |
  | owner-alert template, once approved | Settings | META |
  | delivery zones (fees and areas) | Settings | DECISION |
  | business hours | Business | DECISION |
  | real WhatsApp number connected | WhatsApp | META |
  | products imported | Products | — |
  | AI language model connected (platform) | — | LLM |
- [ ] **DECISION — owner operating procedure** (section 1) is signed by the merchant. It must cover:
  - reviewing orders: reminders come after 2 hours waiting for review and after 24 hours accepted but unpaid;
  - recording manual payments with evidence;
  - answering handoffs and window-closed alerts.
- [ ] **DECISION — privacy and legal sign-off.**
  - customer-data retention and erasure;
  - a processor agreement and privacy notice for the LLM provider, which receives customer message text and first
    names;
  - refund, cancellation and partial-payment policies, all handled manually;
  - who has dashboard access.

## 6. During the pilot

Every day:

- [ ] **HOST — attention list.** Dashboard › Conversations › needs attention. This collects handoffs, failed or
  window-closed messages and rate-limited customers. Clear each one.
- [ ] **HOST — notifications** on the dashboard home: failed owner alerts, order reminders (`order_review_reminder`,
  `order_payment_reminder`) and `customer_rate_limited`.
- [ ] **HOST — failed messages.** `send_failures_1h` in `/readyz?details=1` and
  `duka_outbox_messages{status="failed"}`. The conversation shows the reason.
- [ ] **HOST — dead letters.** `dead_letters_24h` and `webhook.dead` logs. Fix the cause first, then run
  `python -m app.cli requeue-dead`.
- [ ] **HOST + LLM — agent errors.** `agent_errors_1h` and `agent.error` logs. If errors persist, run `llm-check`.
- [ ] **HOST — AI guard.** `assistant_limited` alerts, `duka_ai_guard_denied_24h` and
  `duka_ai_guard_over_limit_24h`. A refusal means a message, a customer or the shop used its AI allowance: reply
  to the flagged conversations; repeated refusals for normal traffic mean a limit is too low.

Every week, and after any prompt or model change:

- [ ] **HOST + LOCAL — grounding review.** The review happens on the production host; the corrections are made in
  the repository.
  - **Do:** count rejected replies (`agent.ungrounded` logs, `duka_agent_runs_1h{status="ungrounded"}`). Open a
    sample in the conversation view: the run is marked `ungrounded`, and its trace shows the model's rejected text.
    The reasons (violation kinds) are only in the conversation API's payload (`agent_runs[].steps`, type
    `grounding`); the view does not show them. Also read a sample of model replies that **were sent** in
    Kinyarwanda, French and Swahili.
  - **Act:** a correct reply that was rejected is a false alarm: add it to `NOT_CLAIMS` in
    `tests/test_grounding_multilingual.py`. A false claim that was sent goes into `CLAIMS`. Fix the patterns
    afterwards.

## 7. After the pilot (not before)

- [ ] **DECISION + LOCAL — customer-data retention and erasure.** The legal and product decision comes first, then
  the code.
- [ ] **LOCAL — dashboard CSP and session hardening** (server-side sign-out; cookie-based sessions).
- [ ] **HOST + LOCAL — load testing.** It needs a test environment. Running more than one API instance also needs a
  shared rate limiter, which is code.
- [ ] **DECISION + LOCAL — staff roles,** so a shop can have more than one user. Decide the roles first.
