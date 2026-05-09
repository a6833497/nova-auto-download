import { chromium } from "playwright";

const URL = "https://bi.aliyuncs.com/token3rd/report/view.htm?id=bae08b94-8dad-4691-a7ca-9783de160a39&accessTicket=c5e15c05-565c-4aa5-859c-fe2c93910c44&dd_orientation=auto";

async function main() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  const page = await context.newPage();

  const olapRequests = [];

  page.on("request", async req => {
    const url = req.url();
    if (url.includes("olap/query") || url.includes("queryByPollKey")) {
      olapRequests.push({
        type: "REQUEST",
        url: url,
        method: req.method(),
        postData: req.postData(),
        headers: JSON.stringify({
          "content-type": req.headers()["content-type"],
          "x-csrf-token": req.headers()["x-csrf-token"],
          "cookie": (req.headers()["cookie"] || "").substring(0, 200) + "...",
        }),
      });
    }
  });

  page.on("response", async resp => {
    const url = resp.url();
    if (url.includes("olap/query") || url.includes("queryByPollKey")) {
      let body = "";
      try { body = await resp.text(); } catch {}
      olapRequests.push({
        type: "RESPONSE",
        url: url.substring(0, 200),
        status: resp.status(),
        bodySize: body.length,
        body: url.includes("queryByPollKey") ? body.substring(0, 3000) : body,
      });
    }
  });

  console.log("Loading page...");
  await page.goto(URL, { waitUntil: "networkidle", timeout: 90000 });
  await page.waitForTimeout(15000);

  console.log("\n=== OLAP API Analysis (" + olapRequests.length + " requests/responses) ===\n");
  for (const r of olapRequests) {
    console.log("---", r.type, "---");
    console.log("URL:", (r.url || "").substring(0, 250));
    if (r.method) console.log("Method:", r.method);
    if (r.postData) console.log("POST Body:", r.postData);
    if (r.headers) console.log("Headers:", r.headers);
    if (r.status !== undefined) console.log("Status:", r.status);
    if (r.bodySize !== undefined) console.log("Body Size:", r.bodySize);
    if (r.body) console.log("Body:", r.body);
    console.log("");
  }

  await browser.close();
}
main().catch(e => console.error(e.message));
