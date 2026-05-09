import { chromium } from "playwright";
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage();
const errors = [];
page.on("console", msg => { if (msg.type() === "error") errors.push(msg.text()); });
page.on("pageerror", err => errors.push("PAGE_ERROR: " + err.message));
await page.goto("https://nova.hoyisr.com/login", { waitUntil: "networkidle", timeout: 15000 });
console.log("ERRORS:", JSON.stringify(errors));

await page.fill("input[type=text],input[name=username]", "admin");
await page.fill("input[type=password]", "admin123");
await page.click("button[type=submit]");
await page.waitForTimeout(3000);
console.log("URL:", page.url());

const errors2 = [];
page.on("pageerror", err => errors2.push("PAGE_ERROR: " + err.message));
await page.goto("https://nova.hoyisr.com/guild-kpi", { waitUntil: "networkidle", timeout: 15000 });
await page.waitForTimeout(2000);
console.log("KPI_ERRORS:", JSON.stringify(errors2));
console.log("OVERLAYS:", await page.locator("[data-state=open]").count());

const screenshot = "/tmp/kpi-page.png";
await page.screenshot({ path: screenshot, fullPage: false });
console.log("SCREENSHOT:", screenshot);
await browser.close();
