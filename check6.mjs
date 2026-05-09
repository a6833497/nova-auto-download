import { chromium } from "playwright";
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
page.on("pageerror", err => console.log("ERR:", err.message));
await page.goto("https://nova.hoyisr.com/login", { waitUntil: "networkidle", timeout: 15000 });
await page.fill("input[type=text],input[name=username]", "admin");
await page.fill("input[type=password]", "admin123");
await page.click("button[type=submit]");
await page.waitForTimeout(3000);
await page.goto("https://nova.hoyisr.com/guild-kpi", { waitUntil: "networkidle", timeout: 15000 });
await page.waitForTimeout(1500);
// Select week with data
await page.evaluate(() => {
  const btns = document.querySelectorAll("button[role=combobox]");
  if (btns[0]) btns[0].click();
});
await page.waitForTimeout(500);
const items = page.locator("[role=option]");
const count = await items.count();
if (count >= 3) await items.nth(2).click();
await page.waitForTimeout(3000);
// Expand first card (巴西1 or 印尼1)
const firstExpand = page.locator(".rounded-xl.border.bg-card").first().locator("button").last();
await firstExpand.click();
await page.waitForTimeout(3000);
// Scroll down to see agent section
await page.evaluate(() => {
  const el = document.querySelector(".rounded-xl.border.bg-card");
  if (el) el.scrollIntoView({ block: "start" });
});
await page.waitForTimeout(500);
await page.screenshot({ path: "/tmp/kpi-agent.png", fullPage: false });
console.log("DONE");
await browser.close();
