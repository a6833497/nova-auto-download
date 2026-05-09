import { chromium } from "playwright";

const URL = "https://bi.aliyuncs.com/token3rd/report/view.htm?id=bae08b94-8dad-4691-a7ca-9783de160a39&accessTicket=c5e15c05-565c-4aa5-859c-fe2c93910c44&dd_orientation=auto";

async function main() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  const page = await context.newPage();

  // Use route to intercept and log full request/response bodies
  await page.route("**/*", async (route, request) => {
    const url = request.url();
    if (url.includes("olap/query") && !url.includes("queryByPollKey")) {
      console.log("\n========== OLAP QUERY REQUEST ==========");
      console.log("URL:", url);
      console.log("Method:", request.method());
      console.log("Content-Type:", request.headers()["content-type"]);
      const pd = request.postData();
      console.log("POST Data raw length:", pd ? pd.length : 0);
      console.log("POST Data:", pd);
      // Try to decode if urlencoded
      if (pd && pd.includes("=")) {
        try {
          const params = new URLSearchParams(pd);
          for (const [k, v] of params.entries()) {
            console.log("  PARAM [" + k + "]:", v.substring(0, 5000));
          }
        } catch(e) { console.log("  (not urlencoded)"); }
      }
    }
    await route.continue();
  });

  let pollCount = 0;
  page.on("response", async resp => {
    const url = resp.url();
    if (url.includes("queryByPollKey")) {
      pollCount++;
      let body = "";
      try { body = await resp.text(); } catch {}
      const parsed = JSON.parse(body);
      const hasResult = parsed.data && parsed.data.normalResult;
      console.log("\n========== POLL RESPONSE #" + pollCount + " ==========");
      console.log("URL:", url);
      console.log("Status:", resp.status());
      console.log("normalResult:", hasResult);
      console.log("Body size:", body.length);
      if (hasResult) {
        // This is the final result with data
        console.log("FULL RESPONSE (first 8000 chars):", body.substring(0, 8000));
      }
    }
    if (url.includes("olap/query") && !url.includes("queryByPollKey")) {
      let body = "";
      try { body = await resp.text(); } catch {}
      console.log("\n========== OLAP QUERY RESPONSE ==========");
      console.log("Body:", body);
    }
  });

  console.log("Loading page...");
  await page.goto(URL, { waitUntil: "networkidle", timeout: 90000 });
  console.log("Page loaded, waiting 30s for data...");
  await page.waitForTimeout(30000);

  console.log("\nTotal poll responses:", pollCount);
  await browser.close();
}
main().catch(e => console.error(e.message));
