import { chromium } from "playwright";
import fs from "fs";

const URL = "https://bi.aliyuncs.com/token3rd/report/view.htm?id=bae08b94-8dad-4691-a7ca-9783de160a39&accessTicket=c5e15c05-565c-4aa5-859c-fe2c93910c44&dd_orientation=auto";

async function main() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  const page = await context.newPage();

  let csrfToken = null;
  const capturedBodies = [];

  // 用route拦截获取完整请求体
  await page.route("**/olap/query", async route => {
    const request = route.request();
    const token = request.headers()["x-csrf-token"];
    if (token) csrfToken = token;
    
    const body = request.postData();
    capturedBodies.push({
      url: request.url(),
      method: request.method(),
      postData: body,
      headers: request.headers()
    });
    console.log("Intercepted olap/query, body length:", body ? body.length : 0);
    
    await route.continue();
  });

  // 也拦截pollKey请求获取响应
  const pollResponses = [];
  await page.route("**/olap/queryByPollKey**", async route => {
    const request = route.request();
    if (!csrfToken) {
      const token = request.headers()["x-csrf-token"];
      if (token) csrfToken = token;
    }
    await route.continue();
  });

  console.log("Opening BI page...");
  await page.goto(URL, { waitUntil: "networkidle", timeout: 60000 });
  console.log("Page loaded, waiting...");
  await page.waitForTimeout(10000);

  const cookies = await context.cookies();
  const cookieStr = cookies.map(c => c.name + "=" + c.value).join("; ");

  const session = { cookies: cookieStr, csrfToken, capturedBodies };
  fs.writeFileSync("/tmp/bi-session.json", JSON.stringify(session, null, 2));
  console.log("Session saved.");
  console.log("CSRF:", csrfToken);
  console.log("Cookie length:", cookieStr.length);
  console.log("Captured bodies:", capturedBodies.length);
  
  for (let i = 0; i < capturedBodies.length; i++) {
    const b = capturedBodies[i];
    if (b.postData) {
      console.log(`Body ${i} length:`, b.postData.length);
      console.log(`Body ${i} preview:`, b.postData.substring(0, 1000));
    }
  }

  await browser.close();
}
main().catch(e => { console.error("ERROR:", e.message); process.exit(1); });
