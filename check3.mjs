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
await page.screenshot({ path: "/tmp/kpi-v4.png", fullPage: true });
// Click first expand button
const expandBtns = page.locator("button").filter({ has: page.locator("svg") });
// Find the chevron button in first card
const firstCard = page.locator(".rounded-xl.border.bg-card").first();
const chevronBtn = firstCard.locator("button").last();
await chevronBtn.click();
await page.waitForTimeout(500);
await page.screenshot({ path: "/tmp/kpi-v4-expanded.png", fullPage: true });
console.log("DONE");
await browser.close();
