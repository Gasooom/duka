# P2 — Embedding usage metering: design

Roadmap: `docs/ROADMAP.md` Phase C1 (Phase 4 P2). Status: implemented and tested locally on 2026-10-10 (§6: what was
built, findings, what is not validated). It only measures: no reply, limit or merchant workflow changes, so it
needed no product decision. Code references are to the P1-complete tree (`f7bc76a`).

## 1. Goal

Record every real embeddings request in the `usage_events` ledger, for the tenant whose data or question it served,
with what the provider reports (tokens, served model) and an estimated cost from the operator's price list — once per
request, never once per text and never twice for a retry.

## 2. Where embeddings are requested today (verified in code)

| Call site | When | Texts per request | Tenant known |
|---|---|---|---|
| `ProductService._embed` (`product_service.py` ~l. 151) | a product is created or edited in the dashboard | 1 | `self.business_id` |
| `ProductService.import_csv` (~l. 405) | CSV import: one batch for all new products | n | `self.business_id` |
| `KnowledgeService.add_document` (`knowledge_service.py` ~l. 84) | a knowledge document is added | one per chunk, one request | `self.business_id` |
| `ProductService.search` via `query_vector` (~l. 299) | the `search_products` tool, inside an AI turn | 1 | `self.business_id` |
| `KnowledgeService.search` via `query_vector` (~l. 104) | the `search_knowledge` tool, inside an AI turn | 1 | `self.business_id` |

Only `OpenAICompatEmbedder` costs money (`EMBEDDING_PROVIDER=openai_compat`). The default `HashingEmbedder` is
offline and free and is not metered, as the rules engine is not. Evaluation runs and tests use the hash embedder.

## 3. Design

- **One event per HTTP request**, kind `embedding`: `units` = number of texts in the request, `input_tokens` = the
  tokens the provider reports for it (`usage.prompt_tokens`, else `usage.total_tokens`; NULL when not reported),
  `attempts` = HTTP attempts made for it (the embedder's own retries are attempts of one event, never new events),
  `status` = success | error, `provider`, `model` (served, from the response), `configured_model`
  (`EMBEDDING_MODEL`), `source_type` = product | product_import | knowledge_document | product_search |
  knowledge_search, `source_id` = the product or document when there is one.
- **Idempotency:** key `emb:<uuid made before the request>`: one external request, one event; a request repeated by a
  retried turn is a new request and a new event (as for `llm_call`).
- **Transaction:** the event is written in its own short transaction right after the request, like `llm_call`, so a
  rollback of the import, document or AI turn that made it does not remove it; a write failure is logged
  (`usage.record_failed`) and never breaks the caller.
- **Failures:** a failed request is recorded (`status` error, tokens NULL, attempts as made) and the existing
  behaviour continues: the import or document add fails as before; a search falls back to word ranking (`query_vector`).
- **Tenant:** always the calling service's `business_id` (server-side), passed explicitly to the metering wrapper;
  never from input.
- **Interface:** `OpenAICompatEmbedder.embed_counted(texts)` returns vectors plus usage and attempts; `embed()` keeps
  returning vectors. A wrapper `embed_texts(bind, business_id, texts, source_type, source_id)` calls the embedder and
  records the event when the embedder is metered; the five call sites use it.
- **Pricing:** the price list gains an optional `embeddings` list (`provider`, `model`, `match`, `input_per_1m`), read
  like the `llm` list; Duka ships no prices. Unpriced when there is no entry or the provider did not report tokens; a
  failed request costs 0 (the rule `llm_call` already follows; a request that timed out after the provider did the
  work may still have been billed — not knowable here).
- **Schema (migration 0012):** `ck_usage_events_kind` gains `embedding`; the two WhatsApp CHECKs treat `embedding` like
  `llm_call` (no WhatsApp fields, any status). Nothing is backfilled; downgrade is refused while `embedding` rows
  exist (insert-only ledger), as for 0010.

## 4. Not in P2

Limits on embeddings (the Runaway Conversation Guard bounds model calls and attempts only; embeddings are bounded per
turn by the tool-call cap and the turn deadline); monthly aggregation (P3); costs per tenant (P4); quotas (P5).

## 5. Tests

One event per request with units, tokens, attempts and both models; a CSV import of n products is one event with
`units` n; a knowledge document of k chunks is one event with `units` k; retries are attempts of one event; a failed
request is one `error` event and the import fails as before / the search degrades; the hash embedder records
nothing; events belong to the calling tenant only; an event survives the rollback of what made it; a ledger write
failure never breaks the import or search; pricing (exact, prefix, unpriced, failed = 0, invalid list refused);
migration 0012 up/down on scratch databases (downgrade refused with embedding rows; existing rows untouched).

## 6. As built (2026-10-10)

- `app/services/embeddings.py`: `Embedder.metered` (True only for `OpenAICompatEmbedder`);
  `OpenAICompatEmbedder.embed_counted` returns the vectors or the error with the attempts made, the reported tokens
  (`prompt_tokens`, else `total_tokens`) and the served model, and never raises (`embed()` raises as before);
  `embed_texts(bind, business_id, texts, source_type=, source_id=)` records one event per request of a metered
  embedder; `query_vector` takes the tenant and source too. The five call sites use them.
- `usage_service.record_embedding`, `pricing.embedding_request_price` (the `embeddings` section of the price list),
  migration `0012` and the matching CHECKs on the model.
- A request that was never sent (too little of the AI turn left) records nothing: nothing was used. Evaluation runs
  switch embeddings metering off while they run (`embeddings.set_metering`, called by `evals/harness.py`), as they
  do for their model calls and WhatsApp sends: platform activity, never a tenant's usage. The requests are still
  made.
- Tests: `tests/test_embedding_metering.py`, `tests/test_migration_0012.py`. The latter also checks that the model's
  CHECK constraints on `usage_events` read back exactly as the migrated ones: `alembic check` does not compare CHECK
  constraints, so a drift there was invisible before (verified: the test fails when one CHECK is changed in the model).

Findings:
1. **A CSV import re-embeds every product it updates, one request each, whether or not its text changed.** For an
   existing SKU, `import_csv` calls `update()` with `category` always present, and `update()` re-embeds whenever
   `category` is given. Re-importing a catalog of n existing products therefore makes n requests to a paid embedder.
   This predates P2; P2 makes it visible (`product` rows). Proposed fix, not part of P2 because it changes when
   embeddings are recomputed: re-embed only when the embedding text changed, and batch an import's updates into one
   request (Phase C or D).
2. **Rounding.** A row's cost is stored in millionths of the currency, rounded half to even. A short request, such as
   a search query, can cost less than half a millionth and is then stored as 0, with its tokens kept. Totals (P3)
   should be priced from summed tokens per model and price version, not by adding rounded per-row costs.
3. **Reported usage.** Whether a given provider reports `usage` for embeddings requests has not been checked against a
   real provider. When it does not, the rows are unpriced, never assumed free.

Not validated: no real embeddings provider has been called (the tests use a mocked endpoint); migration `0012` is
not applied to the development database (still at `0010`); nothing has been deployed.
