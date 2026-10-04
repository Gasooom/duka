// A merchant has the orders page open; a customer orders on WhatsApp; the order must appear without a reload.
const { chromium } = require("playwright");
const WEB = "http://localhost:3000", API = "http://localhost:8000";
const call = async (m, p, b, t) => (await fetch(API + p, { method: m, headers: { "content-type": "application/json", ...(t ? { Authorization: `Bearer ${t}` } : {}) }, body: b ? JSON.stringify(b) : undefined })).json();

(async () => {
  const T = (await call("POST", "/api/auth/login", { email: "electronics@duka.dev", password: "password123" })).access_token;
  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.goto(WEB + "/login");
  await page.fill('input[name="email"]', "electronics@duka.dev");
  await page.fill('input[name="password"]', "password123");
  await page.click("button:has-text('Sign in')");
  await page.waitForURL("**/dashboard");
  await page.goto(WEB + "/dashboard/orders");
  await page.waitForSelector("text=Orders");
  const number = "2507" + Math.floor(10000000 + Math.random() * 89999999);
  for (const text of ["Do you have a Samsung phone under 300k?", "add it", "deliver to Remera, KG 11 Ave", "yes"]) {
    await call("POST", "/api/dev/simulate", { text, from_number: number }, T);
  }
  const newest = (await call("GET", "/api/orders", null, T))[0].order_number;
  const start = Date.now();
  const appeared = await page.waitForSelector(`text=${newest}`, { timeout: 25000 }).then(() => true).catch(() => false);
  console.log(appeared ? `PASS  ${newest} appeared without reload after ${((Date.now() - start) / 1000).toFixed(1)}s`
                       : `FAIL  ${newest} did not appear within 25s`);
  await browser.close();
  process.exit(appeared ? 0 : 1);
})();
