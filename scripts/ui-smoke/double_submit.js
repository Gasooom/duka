// A double click (or double tap) on a dashboard action must have exactly one effect: one product, one knowledge
// document, one WhatsApp reply, one recorded payment. Runs against the dev stack with seed data (see README).
const { chromium } = require("playwright");

const WEB = "http://localhost:3000";
const API = "http://localhost:8000";
const results = [];
const check = (name, ok, detail = "") => { results.push({ name, ok }); console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`); };
const stamp = Date.now().toString().slice(-6);
const phone = () => "2507" + Math.floor(10000000 + Math.random() * 89999999);

async function apiCall(method, path, body, token) {
  const r = await fetch(API + path, { method, headers: { "content-type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) }, body: body ? JSON.stringify(body) : undefined });
  return r.status === 204 ? null : r.json();
}

(async () => {
  const T = (await apiCall("POST", "/api/auth/login", { email: "fashion@duka.dev", password: "password123" })).access_token;
  // Arrange through the WhatsApp simulator: an order waiting for review and a customer waiting for a person.
  const buyer = phone();
  for (const text of ["black sneakers under 100k", "add 1", "deliver to Remera, KG 11 Ave", "yes"]) {
    await apiCall("POST", "/api/dev/simulate", { text, from_number: buyer, name: "Double Click Buyer" }, T);
  }
  const order = (await apiCall("GET", "/api/orders?status=pending", null, T))[0];
  const handoff = await apiCall("POST", "/api/dev/simulate", { text: "I want to talk to a person", from_number: phone(), name: `Waiting ${stamp}` }, T);

  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  await page.goto(WEB + "/login");
  await page.fill('input[name="email"]', "fashion@duka.dev");
  await page.fill('input[name="password"]', "password123");
  await page.click("button:has-text('Sign in')");
  await page.waitForURL("**/dashboard");

  const product = `Double Click Hat ${stamp}`;
  await page.goto(WEB + "/dashboard/products");
  await page.click("button:has-text('Add product')");
  await page.fill('form input[name="name"]', product);
  await page.fill('form input[name="price"]', "5000");
  await page.dblclick("form button:has-text('Add product')");
  await page.waitForTimeout(2500);
  const products = await apiCall("GET", `/api/products?q=${encodeURIComponent(product)}`, null, T);
  check("double click on Add product creates one product", products.length === 1, `${products.length} created`);

  const title = `Opening hours ${stamp}`;
  await page.goto(WEB + "/dashboard/knowledge");
  await page.fill('input[name="title"]', title);
  await page.fill('textarea[name="content"]', "We open at 9am every day.");
  await page.dblclick("button:has-text('Add')");
  await page.waitForTimeout(2500);
  const docs = (await apiCall("GET", "/api/knowledge", null, T)).filter((d) => d.title === title);
  check("double click on Add document creates one document", docs.length === 1, `${docs.length} created`);

  const reply = `Hello from the shop ${stamp}`;
  await page.goto(`${WEB}/dashboard/conversations/${handoff.conversation_id}`);
  await page.fill("input[placeholder^='Reply as staff']", reply);
  await page.dblclick("form button:has-text('Send')");
  await page.waitForTimeout(2500);
  const conv = await apiCall("GET", `/api/conversations/${handoff.conversation_id}`, null, T);
  const sent = conv.messages.filter((m) => m.role === "human_agent" && m.content === reply);
  check("double click on Send delivers the staff reply once", sent.length === 1, `${sent.length} sent`);

  await page.goto(`${WEB}/dashboard/orders?id=${order.id}`);
  await page.click("button:has-text('Accept order')");
  await page.waitForSelector("text=Fulfilment");
  await page.selectOption("form select", "cash");
  await page.fill('form input[name="note"]', "Cash received at the shop");
  await page.dblclick("button:has-text('Confirm payment received')");
  await page.waitForTimeout(2500);
  const paid = await apiCall("GET", `/api/orders/${order.id}`, null, T);
  const payments = (paid.payments || []).filter((p) => p.status === "successful");
  check("double click on Confirm payment records one payment", payments.length === 1 && paid.payment_status === "paid",
    `${payments.length} payment(s), ${paid.payment_status}`);

  await browser.close();
  const failed = results.filter((r) => !r.ok).length;
  console.log(`\n${results.length - failed}/${results.length} double-submit checks passed`);
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(2); });
