// Unit tests for the dashboard's API proxy (app/api/[...path]/route.ts), run with Node's built-in runner (npm test).
// The route is compiled with the project's own TypeScript and fetch is replaced, so no server is needed.
// What matters: every BACKEND_URL form a deployment uses reaches the API — a full URL (docker compose, self-hosted)
// and the bare host:port that render.yaml passes for the API on Render's private network.
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { after, test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";
import ts from "typescript";

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, "..", "app", "api", "[...path]", "route.ts"), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 },
});
const workdir = mkdtempSync(join(tmpdir(), "duka-proxy-test-"));
const compiled = join(workdir, "route.mjs");
writeFileSync(compiled, outputText);
after(() => rmSync(workdir, { recursive: true, force: true }));

let calls = [];
globalThis.fetch = async (url, init = {}) => {
  calls.push({ url, ...init });
  return new Response(JSON.stringify({ ok: true }), { status: 200, headers: { "content-type": "application/json" } });
};

let loads = 0;
// The backend address is read when the route module loads: load a fresh copy for each configuration.
async function routeWith(backendUrl) {
  if (backendUrl === undefined) delete process.env.BACKEND_URL;
  else process.env.BACKEND_URL = backendUrl;
  return import(`${pathToFileURL(compiled).href}?load=${++loads}`);
}

async function forward(backendUrl) {
  const { GET } = await routeWith(backendUrl);
  calls = [];
  const req = {
    method: "GET",
    headers: new Headers({ authorization: "Bearer session-token" }),
    nextUrl: { search: "?status=pending" },
    arrayBuffer: async () => new ArrayBuffer(0),
  };
  const res = await GET(req, { params: Promise.resolve({ path: ["orders", "KF 1"] }) });
  assert.equal(res.status, 200);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].headers.get("authorization"), "Bearer session-token");
  return calls[0].url;
}

test("a full BACKEND_URL is used as given (docker compose, self-hosted)", async () => {
  assert.equal(await forward("http://backend:8000"), "http://backend:8000/api/orders/KF%201?status=pending");
  assert.equal(await forward("https://api.example.com"), "https://api.example.com/api/orders/KF%201?status=pending");
});

test("the bare host:port of Render's private network is reached over http", async () => {
  assert.equal(await forward("duka-api-k3x9:10000"), "http://duka-api-k3x9:10000/api/orders/KF%201?status=pending");
});

test("without BACKEND_URL the local API is used", async () => {
  assert.equal(await forward(undefined), "http://localhost:8000/api/orders/KF%201?status=pending");
});
