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
// Screenshot card view
await page.screenshot({ path: "/tmp/kpi-cards.png", fullPage: false });
// Click table toggle (2nd button in layout toggle group)
const tableBtn = page.locator("button[title=对比视图]");
await tableBtn.click();
await page.waitForTimeout(1000);
await page.screenshot({ path: "/tmp/kpi-table.png", fullPage: false });
console.log("DONE");
await browser.close();
