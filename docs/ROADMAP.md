# Production MVP roadmap — merchant validation and operational readiness

Working plan set by the product owner on 2026-10-10. `CLAUDE.md` holds the short version and the live status table;
this file holds the detail. History (what was built, with evidence) stays in `docs/MILESTONES.md`; the pilot
checklist stays in `docs/EXTERNAL_VALIDATION.md`. Nothing here is a claim that work is done: the status column of
`CLAUDE.md` is, and every status there must point at evidence.

## 1. Objective

Duka is a multi-tenant WhatsApp commerce and business-management platform for small merchants, first in Rwanda.

**Promise:** a merchant manages products, inventory, customer conversations and orders through WhatsApp and a
dashboard, while Duka automates routine work safely, answers with accurate commerce facts, and gives the merchant
control over operations and costs.

The product is production-ready only when the four questions in section 3 are answered with evidence. Green CI,
a running Docker stack or a passing mocked end-to-end test answer none of them on their own.

## 2. Value proposition, and what exists today

Verified against the code on 2026-10-10 (commit `e1a18c2`). "Exists" means implemented and covered by tests in CI;
it does not mean validated with a real merchant, real WhatsApp traffic or the production model.

| Area | Exists (CI-tested) | Known gaps |
|---|---|---|
| **A. Products and inventory** | Product CRUD and CSV import with per-row errors; deactivate; stock changes recorded in the `inventory` movement ledger (`ProductService.set_stock` / `adjust_stock`); stock rows locked `FOR UPDATE` in id order at checkout (`commerce_service.py`, ~l. 432); restock on cancellation (`_restock`); the AI gets prices and stock only from tools, and grounding rejects invented ones | No inventory-discrepancy / stock-count workflow beyond adjustments; unpaid orders never expire and release stock (README › Future work); search is unindexed beyond a few thousand products |
| **B. Orders and customers** | Explicit YES to a delivered server summary (CLAUDE.md rule 7); status and payment state machines; manual payments with evidence; audit trail; owner notifications and reminders; human handoff and takeover; 24-hour window handling | No staff invitations/roles UI (single owner login); refunds and partial payments are manual |
| **C. AI reliability** | Grounding check with deterministic fallback; adversarial eval gate in CI; per-turn caps (5 iterations, 8 tool calls, 45 s, 3 HTTP attempts) | Retry amplification and no tenant quota (`docs/P1_RUNAWAY_GUARD.md` F1–F3); real-provider eval only from a developer key; no native-speaker review |
| **D. System and business management** | Dashboard (inbox, orders, products, knowledge, settings, alerts, setup checklist); `/healthz`, `/readyz`, `/metrics`; scrubbed JSON logs; backup/restore scripts with a verified restore; usage ledger (AI + WhatsApp) | No usage/cost aggregation or merchant-facing usage view; no quotas or spend alerts; no platform admin console; multi-instance not validated |

## 3. The four acceptance questions

Every milestone must move at least one answer forward with evidence. Current state, 2026-10-10:

| # | Question | Evidence required | Current evidence | State |
|---|---|---|---|---|
| Q1 | Can a real merchant operate independently? | Onboarding path; product/inventory workflow; a real WhatsApp conversation; an end-to-end order; merchant visibility of orders, errors and actions; a supervised pilot where the merchant completes agreed tasks | Dashboard walk-through in a headless browser (CI); mocked WhatsApp E2E (CI); no real merchant, no real WhatsApp | BLOCKED (merchant, Meta) |
| Q2 | Are AI answers accurate and safe enough? | Regression + adversarial eval; thresholds approved **before** the final run; zero fabricated payment/order/refund claims in critical cases; native review per pilot language; real-provider run with the production configuration | Offline + adversarial gate in CI; real model (`gpt-4o-mini`, developer key) 67/71 on suite v1.2.0 (`docs/VALIDATION_REPORT.md`); no approved thresholds; no native review | IN PROGRESS / BLOCKED (LLM account, reviewers) |
| Q3 | Is the business economically viable? | Real usage and cost per merchant (AI calls, tokens, retries, embeddings; WhatsApp messages/templates; hosting, DB, backups, monitoring; support); revenue per merchant; margin, break-even, low/expected/high scenarios; the usage level where a merchant becomes unprofitable | Usage ledger records AI calls and WhatsApp traffic (Phase 4 P0); no prices loaded (all unpriced); embeddings not metered; no aggregation; no revenue model | NOT STARTED (needs prices, invoices, pricing decision) |
| Q4 | Does the system stay safe under growth and failure? | Measured targets set first, then: multi-tenant concurrency, duplicate webhooks, provider failures and ambiguous outcomes, DB failures and lock contention, worker restarts, AI loops and usage spikes, inventory races, noisy neighbours, backup/restore, security, migration-failure recovery | Many failure paths tested in CI (durability, isolation, concurrency, DB limits, crash recovery live-tested locally); restore drill verified locally; **AI loops/usage spikes not bounded** (P1); no load test; multi-instance not validated | IN PROGRESS |

Unpriced usage is never treated as zero cost. A spreadsheet margin built on unverified prices does not answer Q3.

## 4. Release gates

Distinct states, never interchangeable: `CODE COMPLETE` → `LOCAL TESTS PASS` → `CI PASS` → `EXTERNAL INTEGRATION
VERIFIED` → `PILOT ACCEPTED` → `PRODUCTION READY`. A mock does not prove an integration; an offline eval does not
prove multilingual production accuracy; CI does not prove recovery, Meta delivery, merchant usability, economics or
multi-instance behaviour.

Milestone statuses used in `CLAUDE.md`: `NOT STARTED`, `IN PROGRESS`, `LOCALLY VERIFIED`, `CI VERIFIED`,
`EXTERNALLY VALIDATED`, `BLOCKED`, `ACCEPTED` (accepted = the owner signed off the acceptance criteria as met).

## 5. Phases

Order matters: a phase starts when its dependencies are met and its open decisions are approved. Each item lists
acceptance criteria (AC), verification, and external needs.

### Phase A — Baseline and product-critical safety
- **A1 Baseline verified** — AC: branch/HEAD/CI/tests/migrations re-verified at the start of each work session.
  Verify: `git status`, CI run of HEAD, `pytest -q`, `alembic check`.
- **A2 Risk register** (section 6) — AC: every open risk has severity, owner phase and mitigation; reviewed at each
  milestone.
- **A3 Keep the existing guarantees** — tenant isolation, explicit confirmation, provider/owner-only payments,
  grounding (CLAUDE.md non-negotiables). AC: their tests stay green; no milestone weakens them.

### Phase B — P1 Runaway Conversation Guard (design: `docs/P1_RUNAWAY_GUARD.md`)
- **B1 Deadlines over tools and embeddings; facts kept when the budget runs out** (no schema, no product decision).
  AC: a slow tool cannot extend a turn more than one tool past its budget; on budget exhaustion the customer gets
  the facts the tools already returned; embedding calls take the remaining turn time as their timeout.
- **B2 Counter table + guard in observe mode** (migration `0011`). AC: atomic reservation proven by a concurrency
  test; observe mode never changes a reply; decisions logged and counted.
- **B3 Enforce the per-message budget across retries.** AC: a turn that fails and is retried can never exceed one
  attempt's worth of model calls in total (probe: 25 calls today).
- **B4 Enforce per-customer limits. B5 Enforce tenant limits.** AC: exact boundaries, racing workers, restart and
  second-instance tests; deterministic commerce paths unaffected.
- **B6 (optional) Tenant fairness in the worker claim.**
- Needs before B2: the six decisions in `docs/P1_RUNAWAY_GUARD.md` §10.

### Phase C — Usage and cost visibility (Phase 4 P2–P7)
C1 embedding metering · C2 monthly usage aggregation (real vs simulated, unknown and unpriced kept distinct) ·
C3 tenant cost model (known prices + documented infrastructure allocation; estimates labelled) · C4 quotas and spend
alerts · C5 merchant usage UI (tenant-isolated) · C6 billing, deferred until C2–C4 are validated.
Needs: provider price lists or invoices (C3), a pricing/revenue decision (C3, C6).

### Phase D — Merchant operations and inventory reliability
Audit first, then close material gaps only: inventory consistency and discrepancy handling, unpaid-order expiry and
stock release, cancellations and failure recovery, onboarding, staff roles, recovery from failed jobs without a
developer. AC per gap: a merchant-facing workflow with tests for normal, invalid, retried and concurrent cases.
Needs: product decisions on expiry rules and staff roles.

### Phase E — AI and multilingual validation
E1 stable eval datasets with thresholds approved before the final run · E2 real-provider tool calling and grounding
with the production model, pinned · E3 failure/timeout/retry/ambiguous-result scenarios · E4 native-speaker review
per pilot language (`docs/MULTILINGUAL_GROUNDING_REVIEW.md`) · E5 critical failures fixed before customers see the
workflow. Never edit a baseline or weaken an assertion to pass. Needs: production LLM account, reviewers, the
pilot-language decision.

### Phase F — Production infrastructure and security
Host and HTTPS, secrets, migrations and their recovery, off-site backups and scheduled restore drills, monitoring and
alerting, deployment rollback, runbooks, rate limiting for the chosen topology, dependency and image scanning.
Verify: `deploy/verify_deployment.sh` (self-hosted) or `docs/DEPLOYMENT.md` › R (Render), `scripts/verify_restore.sh`.
Needs: server or Render account, domain, monitor account, off-site bucket. No deployment without explicit
authorization.

### Phase G — External validation and supervised pilot
`docs/EXTERNAL_VALIDATION.md` §2–6: Meta app/number/templates; signed webhook, inbound, outbound and status events;
production LLM pinned; hosting, monitoring, backups; native and legal/privacy review; a pilot merchant, signed
operating procedure, support owner and rollback criteria. Fix high-severity defects and repeat the tests before
expanding.

### Phase H — Release decision
Go/no-go on evidence for Q1–Q4 (section 3). The final report contains: evidence per question, the real merchant
workflow results, AI accuracy and critical failures, real usage and cost (or a statement that economics are
unvalidated), load/concurrency/recovery/backup/security evidence, remaining risks, and the recommendation.

## 6. Risk register (open risks, 2026-10-10)

| Risk | Severity | Evidence | Mitigation (phase) |
|---|---|---|---|
| A failing turn repeats its model calls on every retry (up to 25 calls for one message, no reply) | High → mitigated | F1, probe 2; B3 enforces the per-message budget by default (6 calls in the same probe) | B3 done |
| No tenant ceiling on model calls/tokens | High | F2, probe 3 | Tenant limits (B5), then quotas (C4) |
| Per-customer limit is in-process only | Medium → mitigated once limits are set | F3; B4 adds durable per-customer AI limits in PostgreSQL (enforced when configured) | B4 done; the 30 messages/minute inbound limiter stays per process: shared limiter before multi-instance (F) |
| Tool and embedding time not covered by the turn deadline | Medium → mitigated | F4, probe 4; fixed by B1 (`tests/test_turn_deadline.py`) | B1 done; other tool-side external calls (MoMo, not enabled) only have their own timeouts |
| No real WhatsApp traffic ever processed; metering's real paths unproven | High (for go-live) | `docs/MILESTONES.md` M4 | G |
| Production LLM not validated; no approved accuracy thresholds; no native review | High (for go-live) | M3, M10, Q2 | E, G |
| No prices loaded; economics unknown | High (for viability) | OPERATIONS › Usage metering | C1–C3 |
| Noisy neighbour: one tenant can occupy the shared workers | Medium | F6 (`inbound.py` claim order) | B6 / F |
| Unpaid orders hold stock indefinitely | Medium | README › Future work | D |
| Multi-instance behaviour unvalidated | Medium | README › Future work | F |

## 7. Rollback and release discipline

- Small isolated commits; push only after the diff is reviewed and the relevant checks pass; verify CI on the pushed
  commit. Schema changes are additive where possible, with a tested downgrade or an explicit refusal (as 0010).
- New runtime behaviour that can affect merchants ships behind a switch (off / observe / enforce) when its effect is
  uncertain.
- No deployment, production data change, external spend or new paid service without explicit authorization.
