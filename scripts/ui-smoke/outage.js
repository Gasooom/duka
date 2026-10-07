// With the API down the dashboard must say so — not hang, not crash — and recover by itself once the API is back.
// Stops and restarts the backend container of the dev stack (docker compose), so it runs last.
const { execSync } = require("child_process");
const path = require("path");
const { chromium } = require("playwright");

const WEB = "http://localhost:3000";
const API = "http://localhost:8000";
const ROOT = path.resolve(__dirname, "..", "..");
const NOT_RESPONDING = "text=/not responding right now/i";
const results = [];
const check = (name, ok, detail = "") => { results.push({ name, ok }); console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`); };
const compose = (args) => execSync(`docker compose ${args}`, { cwd: ROOT, stdio: "inherit" });
const shows = async (page, sel, timeout = 15000) => page.waitForSelector(sel, { timeout }).then(() => true).catch(() => false);

async function apiHealthy(timeoutMs) {
  const end = Date.now() + timeoutMs;
  while (Date.now() < end) {
    try { if ((await fetch(API + "/healthz")).ok) return true; } catch {}
    await new Promise((r) => setTimeout(r, 1000));
  }
  return false;
}

(async () => {
  const login = await fetch(API + "/api/auth/login", { method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ email: "electronics@duka.dev", password: "password123" }) });
  const token = (await login.json()).access_token;
  const browser = await chromium.launch();
  const page = await browser.newPage();
  const crashes = [];
  page.on("pageerror", (e) => crashes.push(String(e)));

  try {
    compose("stop backend");
    await page.goto(WEB + "/login");
    await page.fill('input[name="email"]', "electronics@duka.dev");
    await page.fill('input[name="password"]', "password123");
    await page.click("button:has-text('Sign in')");
    check("sign-in says the server is not responding (no endless spinner)", await shows(page, NOT_RESPONDING));
    await page.evaluate((t) => localStorage.setItem("duka_token", t), token);
    await page.goto(WEB + "/dashboard/orders");
    check("a dashboard page says the server is not responding", await shows(page, NOT_RESPONDING));
    check("the session is kept (no bounce to the login page)", !page.url().includes("/login"), page.url());
  } finally {
    compose("start backend");
  }
  check("the API is back", await apiHealthy(120000));
  const orders = await (await fetch(API + "/api/orders", { headers: { Authorization: `Bearer ${token}` } })).json();
  await page.reload();
  // Data again, not just the page frame: the newest order is listed (the walk-through before this created one).
  const listed = orders.length ? await shows(page, `text=${orders[0].order_number}`) : true;
  check("the dashboard recovers on its own, still signed in", listed && !(await page.isVisible(NOT_RESPONDING)),
    orders.length ? orders[0].order_number : "no orders to list");
  check("no page crashed", crashes.length === 0, crashes.slice(0, 2).join(" | "));

  await browser.close();
  const failed = results.filter((r) => !r.ok).length;
  console.log(`\n${results.length - failed}/${results.length} outage checks passed`);
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error(e); try { compose("start backend"); } catch {} process.exit(2); });
