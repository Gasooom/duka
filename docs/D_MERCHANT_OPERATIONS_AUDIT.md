# Phase D — Merchant operations and inventory: audit

Roadmap: `docs/ROADMAP.md` Phase D ("audit first, then close material gaps only"). Status: read-only audit,
2026-10-10, of the code at `0f82acb`. Fixed since, with regression tests: D1–D4, D13, D14
(`tests/test_order_integrity.py`) and D12 (`tests/test_payments.py`). D5–D11 await product decisions.

Method: the order, stock, payment, inbound and recovery paths were read in full (models, services, workflows,
routes, the operator command, the agent tools and their tests). The two concurrency findings (D1, D2) were reproduced
by a probe on a throwaway database of the disposable test server (created, migrated to head, dropped afterwards): two
threads load the same order, as two browser tabs or a retried request would, then act at the same moment.

## 1. What already holds (with evidence)

- **Checkout:** an order exists only after an explicit YES to a delivered, unchanged summary; the summary expires
  after 30 minutes (`commerce_service.py` `CHECKOUT_TTL`, l. 40) and a changed cart, price, stock or zone resends it
  (CLAUDE.md rule 7, `tests/test_orders_handoff.py`, `tests/test_commerce.py`).
- **Stock at checkout:** product rows are locked `FOR UPDATE` in product-id order and decremented in the order's
  transaction (`create_from_cart`, l. 408–454); `ck_products_stock_nonneg` forbids negative stock; two customers
  racing for the last unit get one order and one conflict (`tests/test_commerce.py`
  `test_concurrent_stock_race_is_a_conflict`).
- **Inventory ledger:** every change is an `inventory` movement with its balance (`initial`, `order`,
  `order_cancelled`, `adjustment`); relative adjustments lock the product (`adjust_stock`, `product_service.py`
  l. 243–245); `GET /api/products/{id}/inventory` shows the history.
- **Order states:** `ORDER_TRANSITIONS` (`commerce_service.py` l. 44) keeps `delivered` and `cancelled` final; a
  cancellation restocks; every owner change is audited and the customer is told.
- **Payments:** only the provider or the owner's audited record can mark an order paid; a reference cannot settle two
  orders (unique index); a manual payment can be voided with a reason; provider callbacks lock the payment row and are
  idempotent (`payment_service.py` l. 107).
- **Ageing orders:** the owner is reminded once per order when it waits for review or stays unpaid after acceptance
  (`workflows/orders.py` `remind_aging_orders`); nothing is cancelled automatically.
- **Onboarding:** shops are created by the operator (`python -m app.cli create-business`) while registration is
  closed; the dashboard's setup checklist (`GET /api/dashboard/setup`) checks WhatsApp, products, delivery, payment
  instructions, owner alerts, hours and the AI switch.

## 2. Findings

| ID | Finding | Evidence | Severity | Fix | Product decision |
|---|---|---|---|---|---|
| D1 | Two concurrent status changes of one order both apply. Two cancellations restock twice (reproduced); by the same path, a cancellation and an acceptance can leave an accepted order whose stock was returned (from the code). | Probe: two concurrent cancellations of a 1-unit order, stock 5 → 4 → **6** (5 expected), two `order_cancelled` movements. Code: `PATCH /api/orders/{id}` loads the order without a lock (`routes/orders.py` l. 52) and `OrderService.transition` checks the status in Python (`commerce_service.py` l. 482). | High (stock integrity) | Lock the order row (`FOR UPDATE`) before checking the transition: the second request then sees the new status and is refused by the existing rules. | No |
| D2 | Two concurrent manual payments of one order both record. | Probe: two concurrent cash payments → **2** successful payments (1 expected), order `paid`. Code: `record_manual` checks `_payable` on an unlocked order (`payment_service.py` l. 158–169); the unique index only covers equal references. | Medium (payment records; revenue counts the order once) | Lock the order row in `record_manual`, `void_manual` and `report_reference`. | No |
| D3 | Setting an absolute stock (product edit, CSV re-import of an existing SKU) reads the product without a lock: a sale committed meanwhile is overwritten, and the movement's `change` comes from the stale value, so the ledger no longer adds up to the balance. | Code: `ProductService.update` loads with `get_or_404` without `for_update` (`product_service.py` l. 182) and `set_stock` computes `change` from that value (l. 239). Not probed. | Medium (ledger integrity) | Lock the product row in `update()`, as `adjust_stock` does: the movement is then exact. Whether an owner's count should override sales made during the count is part of D13. | No (for the lock) |
| D4 | A cancellation locks the products in the order's item order; checkout locks them by product id. A cancellation and a checkout of the same products can deadlock: PostgreSQL aborts one of them. | Code: `_restock` iterates `order.items` (`commerce_service.py` l. 497–498); checkout sorts (l. 430–432). Not probed. | Low | Lock in product-id order in `_restock`. | No |
| D5 | A dead-lettered inbound message disappears from the shop's view. Processing rolls back the stored customer message with everything else; after `WEBHOOK_MAX_ATTEMPTS` only the event is marked `dead` and logged. The owner is not told, the inbox does not show it, and the customer gets no answer. | Code: `workflows/inbound.py` `process_event` (l. 216–241) and `_record_failure` (l. 244–261). Recovery: the operator's `requeue-dead` command after a fix. | Medium | (a) On dead-letter, store the message alone, flag the conversation and alert the owner; or (b) a dashboard list of unprocessed messages with a retry action. | Yes (merchant-visible) |
| D6 | Unpaid orders never expire: their stock stays reserved until the owner acts (reminders only). | `remind_aging_orders` docstring; README › Future work. | Medium | Expiry per state, with a customer message and a restock. | Yes (rules, timing, wording) |
| D7 | No failed delivery or return: `out_for_delivery` can only become `delivered`, and `delivered` is final. A failed delivery or a return has no state and no restock (only a manual stock adjustment). | `ORDER_TRANSITIONS` (l. 44–51). | Medium | States and stock rules for failed delivery and returns. | Yes |
| D8 | An order cannot be edited: a wrong item or quantity means cancelling it and the customer ordering again. | No route besides status changes (`routes/orders.py`). | Low | Owner edits before acceptance would need a new summary and YES (rule 7). | Yes |
| D9 | A customer cannot cancel on WhatsApp: no agent tool may change an order, so they must reach a person (handoff). | The 19 tools in `tools/commerce_tools.py`; CLAUDE.md "Adding things". | Low | A server-side customer cancellation of a pending order, confirmed like an order. | Yes |
| D10 | Staff accounts do not exist: `users.role` is owner \| staff and `require_owner` guards payments and settings, but nothing creates a staff user. One login per shop. | `models/business.py` l. 42; `api/deps.py` l. 60–63; no route or command creates a staff user. | Low (pilot: one owner) | Staff invitations and permissions. | Yes (roadmap) |
| D11 | Low stock is only a list on the dashboard home; the owner gets no alert. | `dashboard.py` `stats` (`low_stock`). | Low | An owner notification when a product crosses its threshold. | Yes (alert channel and cost) |
| D12 | Money arriving for a cancelled order (provider payments) is audited and the customer is told it was paid, but the owner gets no notification to refund. MoMo is not enabled; a manual payment cannot be recorded on a cancelled order. | `payment_service.py` l. 119–121; `workflows/payments.py` `notify_payment_result`; `tests/test_payments.py` `test_payment_for_a_cancelled_order_is_flagged`. | Low (now) | Notify the owner (dashboard notification) when it happens. | No |
| D13 | No stock-count workflow and no check that the ledger adds up: nothing compares the sum of movements with `products.stock_quantity`, and D3 can make them differ. | No such check in `ops.py`, the command line or the dashboard. | Low | A read-only check (operator command or `/readyz` detail) listing products whose ledger and balance disagree; a counted-stock workflow later. | No (check) / Yes (workflow) |
| D14 | Stock set by a CSV import is recorded as `adjustment`, indistinguishable from a manual edit. | `product_service.py` l. 209. | Low | Record it as `import`. | No |

## 3. Proposed order

1. **Integrity fixes that need no product decision:** D1 and D2 (lock the order row), then D3 and D4 (product
   locks), D13 (the ledger check), D12 and D14. Each is small and gets regression tests that run the races
   deterministically, as the probe did. Recommended next, on approval of this audit.
2. **Decisions to request:** D5 (dead-lettered messages), D6 (expiry), D7 (failed delivery and returns), D8 (order
   edits), D9 (customer cancellation), D10 (staff), D11 (low-stock alerts).

Not covered here: anything that needs real traffic (Meta delivery, real payments), performance under load, and the
dashboard's own usability (Phase G, with a merchant).
