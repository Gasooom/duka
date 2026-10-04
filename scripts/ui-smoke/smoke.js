// Headless walk-through of the merchant dashboard against the running Docker stack.
const { chromium } = require("playwright");

const WEB = "http://localhost:3000";
const API = "http://localhost:8000";
const results = [];
const seen = async (page, sel) => page.waitForSelector(sel, { timeout: 8000 }).then(() => true).catch(() => false);
const check = (name, ok, detail = "") => { results.push({ name, ok, detail }); console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`); };

async function apiCall(method, path, body, token) {
  const r = await fetch(API + path, { method, headers: { "content-type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) }, body: body ? JSON.stringify(body) : undefined });
  return r.status === 204 ? null : r.json();
}

(async () => {
  // Arrange: a fresh customer places an order and asks for a person (through the simulator pipeline).
  const login = await apiCall("POST", "/api/auth/login", { email: "electronics@duka.dev", password: "password123" });
  const T = login.access_token;
  const number = "2507" + Math.floor(10000000 + Math.random() * 89999999);
  for (const text of ["Do you have a Samsung phone under 300k?", "add it", "deliver to Remera, KG 11 Ave", "yes"]) {
    await apiCall("POST", "/api/dev/simulate", { text, from_number: number, name: "UI Smoke" }, T);
  }
  const other = "2507" + Math.floor(10000000 + Math.random() * 89999999);
  await apiCall("POST", "/api/dev/simulate", { text: "I want to talk to a person", from_number: other, name: "Needs Help" }, T);
  const orders = await apiCall("GET", "/api/orders?status=pending", null, T);
  const order = orders[0];

  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const consoleErrors = [];
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });
  page.on("pageerror", (e) => consoleErrors.push(String(e)));

  await page.goto(WEB + "/login");
  await page.fill('input[name="email"]', "electronics@duka.dev");
  await page.fill('input[name="password"]', "password123");
  await page.click("button:has-text('Sign in')");
  await page.waitForURL("**/dashboard");
  await page.waitForSelector("text=Overview");
  check("login lands on the overview", true);
  check("setup checklist is shown", await seen(page, "text=Before real customers write in"));
  check("platform AI warning is honest", await seen(page, "text=AI language model not configured"));
  await page.waitForSelector("text=Your alerts");
  check("owner alerts list shows the new order", await seen(page, `text=New order ${order.order_number}`));
  const ordersBadge = await page.locator("nav a[href='/dashboard/orders'] span.rounded-full").textContent().catch(() => null);
  check("orders nav badge counts orders to review", Number(ordersBadge) >= 1, `badge=${ordersBadge}`);
  const convBadge = await page.locator("nav a[href='/dashboard/conversations'] span.rounded-full").textContent().catch(() => null);
  check("conversations nav badge counts handoffs", Number(convBadge) >= 1, `badge=${convBadge}`);

  // Review the order, then record a cash payment.
  await page.goto(`${WEB}/dashboard/orders?id=${order.id}`);
  await page.waitForSelector(`text=${order.order_number}`);
  check("order detail shows confirmation evidence", await page.isVisible("text=Confirmed by the customer on WhatsApp"));
  await page.click("button:has-text('Accept order')");
  await page.waitForSelector("text=Fulfilment");
  check("accept moves the order to fulfilment", true);
  await page.selectOption("select", "cash");
  await page.fill('input[name="note"]', "Received by Jane at the shop");
  await page.click("button:has-text('Confirm payment received')");
  await page.waitForSelector("text=Confirmed by the shop (manual record)");
  check("manual payment is recorded and attributed", true);
  check("history lists the audited actions", await page.isVisible("text=payment manual recorded"));
  const detail = await apiCall("GET", `/api/orders/${order.id}`, null, T);
  check("API agrees: accepted + paid by owner", detail.status === "accepted" && detail.payment_status === "paid" && detail.payments[0].confirmation_source === "owner");

  // Inbox: the handoff waits for a person; take over is already done, reply is possible.
  await page.goto(`${WEB}/dashboard/conversations?attention=1`);
  await page.waitForSelector("text=Needs Help");
  await page.click("text=Needs Help");
  await page.waitForSelector("text=Return to AI");
  await page.fill("input[placeholder^='Reply as staff']", "Hello, this is the shop. How can we help?");
  await page.click("button:has-text('Send')");
  await page.waitForSelector("text=Hello, this is the shop. How can we help?");
  check("staff reply from the inbox is delivered", true);

  // Settings and account pages render their controls.
  await page.goto(`${WEB}/dashboard/settings`);
  check("AI pause switch present", await seen(page, "text=AI assistant is answering customers"));
  check("payment instructions field present", await seen(page, "text=Payment instructions sent to customers"));
  await page.goto(`${WEB}/dashboard/account`);
  check("account page offers password change", await page.isVisible("text=Change password"));

  // Mobile: no horizontal scroll on the busiest pages.
  await page.setViewportSize({ width: 375, height: 800 });
  for (const path of ["/dashboard", "/dashboard/orders?id=" + order.id, "/dashboard/conversations", "/dashboard/settings"]) {
    await page.goto(WEB + path);
    await page.waitForTimeout(800);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    check(`no horizontal scroll at 375px: ${path}`, overflow <= 1, `overflow=${overflow}px`);
  }
  check("no browser console errors", consoleErrors.length === 0, consoleErrors.slice(0, 3).join(" | "));
  await browser.close();
  const failed = results.filter((r) => !r.ok).length;
  console.log(`\n${results.length - failed}/${results.length} checks passed`);
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(2); });
