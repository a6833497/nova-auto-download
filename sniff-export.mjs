import { chromium } from "playwright";

const URL = "https://bi.aliyuncs.com/token3rd/report/view.htm?id=bae08b94-8dad-4691-a7ca-9783de160a39&accessTicket=c5e15c05-565c-4aa5-859c-fe2c93910c44&dd_orientation=auto";

async function main() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ acceptDownloads: true, viewport: { width: 1920, height: 1080 } });
  const page = await context.newPage();

  const captured = [];
  page.on("request", req => {
    const u = req.url();
    if (u.includes("export") || u.includes("download") || u.includes("oss") || u.includes("blob")) {
      captured.push({ dir: "REQ", method: req.method(), url: u.substring(0, 150) });
    }
  });
  page.on("response", async resp => {
    const u = resp.url();
    if (u.includes("export") || u.includes("download") || u.includes("oss") || u.includes("blob")) {
      let body = "";
      try { body = (await resp.text()).substring(0, 500); } catch {}
      captured.push({ dir: "RES", status: resp.status(), url: u.substring(0, 150), contentType: resp.headers()["content-type"] || "", body });
    }
  });

  page.on("download", d => console.log("DOWNLOAD_EVENT:", d.url(), d.suggestedFilename()));

  console.log("1. Loading page...");
  await page.goto(URL, { waitUntil: "domcontentloaded", timeout: 60000 });
  await page.waitForTimeout(15000);

  console.log("2. Click export...");
  const exportBtn = page.locator("text=导出").first();
  if (!await exportBtn.isVisible({ timeout: 5000 }).catch(() => false)) {
    console.log("Export button not visible"); await browser.close(); return;
  }
  await exportBtn.click();
  await page.waitForTimeout(2000);

  const cs = page.locator('label.ant-radio-wrapper').filter({ hasText: '当前sheet页' }).first();
  if (await cs.isVisible({ timeout: 2000 }).catch(() => false)) await cs.click();
  await page.waitForTimeout(500);

  console.log("3. Click confirm...");
  const dlPromise = page.waitForEvent("download", { timeout: 90000 }).catch(() => null);
  const btns = await page.locator("button.ant-btn-primary").all();
  for (let i = btns.length - 1; i >= 0; i--) {
    const t = await btns[i].textContent();
    if (t.includes("确")) { await btns[i].click(); break; }
  }

  console.log("4. Waiting for download...");
  const dl = await dlPromise;
  if (dl) {
    console.log("GOT DOWNLOAD:", dl.url(), dl.suggestedFilename());
  } else {
    console.log("NO DOWNLOAD received");
  }

  await page.waitForTimeout(3000);

  console.log("\n=== CAPTURED NETWORK ===");
  for (const c of captured) {
    console.log(JSON.stringify(c));
  }

  await browser.close();
}
main().catch(e => console.error("FATAL:", e.message));
