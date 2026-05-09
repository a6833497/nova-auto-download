import { chromium } from "playwright";
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
page.on("pageerror", err => console.log("ERR:", err.message));
await page.goto("https://nova.hoyisr.com/login", { waitUntil: "networkidle", timeout: 15000 });
await page.fill("input[type=text],input[name=username]", "admin");
await page.fill("input[type=password]", "admin123");
await page.click("button[type=submit]");
await page.waitForTimeout(3000);
// Go to daily report with specific date
await page.goto("https://nova.hoyisr.com/daily", { waitUntil: "networkidle", timeout: 30000 });
await page.waitForTimeout(5000);
// Scroll to quality alert section
const alert = page.locator("text=S女质量预警");
if (await alert.count() > 0) {
  await alert.scrollIntoViewIfNeeded();
  await page.waitForTimeout(500);
  // Take screenshot focused on the alert area
  await page.screenshot({ path: "/tmp/daily-quality.png", fullPage: false });
  console.log("FOUND quality alert");
} else {
  console.log("quality alert NOT found");
  await page.screenshot({ path: "/tmp/daily-quality.png", fullPage: true });
}
await browser.close();
