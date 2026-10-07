// Unit tests for lib/api.ts, run with Node's built-in runner (npm test): no test framework, no browser.
// The module is compiled with the project's own TypeScript and given the two browser globals it uses.
// What matters: an identical write that is still in flight is sent once (a double click must never create two
// products, payments or WhatsApp messages), while reads, different writes and later retries always go out.
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { after, beforeEach, test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";
import ts from "typescript";

const here = dirname(fileURLToPath(import.meta.url));
const { outputText } = ts.transpileModule(readFileSync(join(here, "..", "lib", "api.ts"), "utf8"), {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 },
});
const workdir = mkdtempSync(join(tmpdir(), "duka-api-test-"));
const compiled = join(workdir, "api.mjs");
writeFileSync(compiled, outputText);
after(() => rmSync(workdir, { recursive: true, force: true }));

const storage = new Map();
globalThis.localStorage = {
  getItem: (k) => (storage.has(k) ? storage.get(k) : null),
  setItem: (k, v) => storage.set(k, String(v)),
  removeItem: (k) => storage.delete(k),
};

let calls = [];
// fetch that answers only when the test says so, so requests can be kept "in flight".
globalThis.fetch = (url, init = {}) => new Promise((resolve) => {
  calls.push({ url, ...init, answer: (status, body) => resolve(new Response(body === undefined ? null : JSON.stringify(body),
    { status, headers: { "content-type": "application/json" } })) });
});

const { api, ApiError } = await import(pathToFileURL(compiled).href);
const tick = () => new Promise((r) => setTimeout(r, 0));

beforeEach(() => {
  calls = [];
  storage.clear();
});

test("an identical write still in flight is sent once and both callers get its result", async () => {
  const first = api("/products", { body: { name: "Hat", price: 5000 } });
  const second = api("/products", { body: { name: "Hat", price: 5000 } });
  await tick();
  assert.equal(calls.length, 1);
  calls[0].answer(201, { id: "p1" });
  assert.deepEqual(await first, { id: "p1" });
  assert.deepEqual(await second, { id: "p1" });
});

test("once the write has finished, the same write can be sent again", async () => {
  const first = api("/orders/o1/payments/manual", { body: { method: "cash" } });
  await tick();
  calls[0].answer(200, { ok: true });
  await first;
  const again = api("/orders/o1/payments/manual", { body: { method: "cash" } });
  await tick();
  assert.equal(calls.length, 2);
  calls[1].answer(200, { ok: true });
  await again;
});

test("different writes are never merged", async () => {
  api("/conversations/c1/reply", { body: { text: "Hello" } });
  api("/conversations/c1/reply", { body: { text: "Hello again" } });
  api("/products/p1", { method: "PATCH", body: { price: 1 } });
  api("/products/p1", { method: "DELETE" });
  await tick();
  assert.deepEqual(calls.map((c) => `${c.method} ${c.url} ${c.body ?? ""}`), [
    'POST /api/conversations/c1/reply {"text":"Hello"}',
    'POST /api/conversations/c1/reply {"text":"Hello again"}',
    'PATCH /api/products/p1 {"price":1}',
    "DELETE /api/products/p1 ",
  ]);
  calls.forEach((c) => c.answer(200, {}));
});

test("reads are never merged", async () => {
  api("/orders");
  api("/orders");
  await tick();
  assert.equal(calls.length, 2);
  calls.forEach((c) => c.answer(200, []));
});

test("a failed write reaches every waiting caller and does not block the retry", async () => {
  const first = api("/knowledge", { body: { title: "Hours", content: "9-5" } });
  const second = api("/knowledge", { body: { title: "Hours", content: "9-5" } });
  await tick();
  calls[0].answer(502, { detail: "Duka's server is not responding right now. Please try again in a minute." });
  for (const p of [first, second]) {
    await assert.rejects(p, (e) => e instanceof ApiError && e.status === 502 && /not responding/.test(e.message));
  }
  const retry = api("/knowledge", { body: { title: "Hours", content: "9-5" } });
  await tick();
  assert.equal(calls.length, 2);
  calls[1].answer(201, { id: "d1" });
  assert.deepEqual(await retry, { id: "d1" });
});

test("requests carry the signed-in session, and validation errors are readable", async () => {
  storage.set("duka_token", "token-123");
  const r = api("/business/agent-config", { method: "PATCH", body: { model: "nope" } });
  await tick();
  assert.equal(calls[0].headers.Authorization, "Bearer token-123");
  assert.equal(calls[0].headers["Content-Type"], "application/json");
  calls[0].answer(422, { detail: "Invalid request", errors: [{ field: "model", message: "not available" }] });
  await assert.rejects(r, (e) => e.status === 422 && e.message === "model: not available");
});
