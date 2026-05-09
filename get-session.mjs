import { chromium } from "playwright";
import fs from "fs";

const URL = "https://bi.aliyuncs.com/token3rd/report/view.htm?id=bae08b94-8dad-4691-a7ca-9783de160a39&accessTicket=c5e15c05-565c-4aa5-859c-fe2c93910c44&dd_orientation=auto";

async function main() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  const page = await context.newPage();

  let csrfToken = null;
  const capturedRequests = [];

  // 拦截请求获取csrf-token和olap请求体
  page.on("request", req => {
    const token = req.headers()["x-csrf-token"];
    if (token) csrfToken = token;
    
    const url = req.url();
    if (url.includes("olap/query")) {
      capturedRequests.push({
        url: url,
        method: req.method(),
        postData: req.postData(),
        headers: req.headers()
      });
    }
  });

  console.log("Opening BI page...");
  await page.goto(URL, { waitUntil: "networkidle", timeout: 60000 });
  console.log("Page loaded, waiting for API calls...");
  await page.waitForTimeout(8000);

  // 获取cookies
  const cookies = await context.cookies();
  const cookieStr = cookies.map(c => c.name + "=" + c.value).join("; ");

  const session = { cookies: cookieStr, csrfToken, capturedRequests };
  fs.writeFileSync("/tmp/bi-session.json", JSON.stringify(session, null, 2));
  console.log("Session saved.");
  console.log("CSRF token:", csrfToken);
  console.log("Cookie length:", cookieStr.length);
  console.log("Captured olap requests:", capturedRequests.length);
  
  if (capturedRequests.length > 0) {
    console.log("First request URL:", capturedRequests[0].url);
    const pd = capturedRequests[0].postData;
    if (pd) {
      console.log("PostData preview:", pd.substring(0, 500));
    }
  }

  await browser.close();
}
main().catch(e => { console.error("ERROR:", e.message); process.exit(1); });
