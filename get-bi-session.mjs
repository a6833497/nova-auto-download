/**
 * 获取 Quick BI session（Playwright最小化使用）
 * 输出：/tmp/bi-session.json
 * 包含：cookies, csrfToken, xDlpj, xGwReferer
 * 
 * 同时拦截并保存所有tab的olap/query请求模板
 */
import { chromium } from "playwright";
import fs from "fs";

// 所有10个报表的URL（2026-05-02 加印尼3-宝石 idx 9）
const REPORTS = [
  { name: "印尼1-Nova",     id: "d67f5126-4e68-47a3-bf5a-4b884866cb5a", ticket: "644babb5-f5d5-45a9-863d-cb7ba7cab030" },
  { name: "印尼2-Carote",   id: "bae08b94-8dad-4691-a7ca-9783de160a39", ticket: "c5e15c05-565c-4aa5-859c-fe2c93910c44" },
  { name: "巴西1-Nova",     id: "6d33fdf8-9236-455b-be7b-4ff6ea04dabe", ticket: "5fe5b405-3302-4fe3-a89a-d657861b9459" },
  { name: "巴西2-Evian",    id: "30b58907-ae0c-407d-b3f0-e52d09f71e6b", ticket: "949597c4-18bd-4bff-b692-6f606a1cd327" },
  { name: "巴西3-Wisky",    id: "75e0152a-25cf-4bf0-80e6-2889bf8e6798", ticket: "3f7ee12b-098b-49a5-aef9-fd899b845c18" },
  { name: "巴西4-Doce",     id: "2034e69d-3c70-4b43-8a70-47f3cb8a45a5", ticket: "a9e70ecc-0acf-4562-947c-6cd2d0fe129a" },
  { name: "土耳其1-Evian",  id: "b2ba3620-7bfc-4a54-8e4a-9bf2f4577fa7", ticket: "bd4038a2-3a79-49c7-9880-270b58697c3a" },
  { name: "西语1-Nova",     id: "6e5c9d15-df45-4ee9-b9b7-59d1265f7388", ticket: "75d522ff-bff5-4522-90b2-ebb789439485" },
  { name: "西语2-Evian",    id: "1fca6b36-5fa6-4906-906d-9495e60f5fe1", ticket: "248e5488-a1ab-42c5-8104-aee77e6565b9" },
  { name: "印尼3-宝石",     id: "2f802df6-2db1-4ebd-bd6c-0d663d5e0a3f", ticket: "a0f65fad-643c-4225-aa66-de1ae2527ab2" },
];

function buildUrl(report) {
  return "https://bi.aliyuncs.com/token3rd/report/view.htm?id=" + report.id + "&accessTicket=" + report.ticket + "&dd_orientation=auto";
}

async function getSession(reportIndex = 1) {
  const report = REPORTS[reportIndex];
  const url = buildUrl(report);
  console.log("获取session: " + report.name);
  
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1920, height: 1080 } });
  const page = await context.newPage();

  let csrfToken = null;
  let xDlpj = null;
  let xGwReferer = null;
  const templates = {};

  // 拦截请求获取header和body
  await page.route("**/olap/query", async route => {
    const req = route.request();
    const h = req.headers();
    if (h["x-csrf-token"]) csrfToken = h["x-csrf-token"];
    if (h["x-dlpj"]) xDlpj = h["x-dlpj"];
    if (h["x-gw-referer"]) xGwReferer = h["x-gw-referer"];
    
    const body = req.postData();
    if (body) {
      const params = new URLSearchParams(body);
      const olapParam = params.get("olapQueryParam");
      const componentId = params.get("componentId");
      let parsed = null;
      try { parsed = JSON.parse(olapParam); } catch(e) {}
      
      templates[componentId] = {
        componentId,
        componentName: parsed ? parsed.componentName : null,
        reportId: params.get("reportId"),
        componentType: params.get("componentType"),
        olapQueryParam: olapParam,
      };
      console.log("  拦截: " + componentId + " (" + (parsed ? parsed.componentName : "?") + ")");
    }
    await route.continue();
  });

  // 打开页面
  await page.goto(url, { waitUntil: "networkidle", timeout: 90000 });
  await page.waitForTimeout(10000);

  // 切换每个tab获取所有组件的请求模板
  const tabTexts = await page.evaluate(() => {
    const container = document.querySelector(".quick-report-preview-sheet-tabs");
    if (!container) return [];
    const spans = container.querySelectorAll("span");
    return Array.from(spans).map(s => s.textContent.trim()).filter(t => t.length > 0);
  });
  
  console.log("  Tabs: " + tabTexts.join(", "));

  for (const tabName of tabTexts) {
    try {
      const tabEl = page.locator(".quick-report-preview-sheet-tabs span:has-text(\"" + tabName + "\")").first();
      if (await tabEl.isVisible({ timeout: 2000 }).catch(() => false)) {
        await tabEl.click();
        await page.waitForTimeout(2000);
        await page.waitForLoadState("networkidle", { timeout: 15000 }).catch(() => {});
        await page.waitForTimeout(3000);
      }
    } catch(e) {}
  }

  // 获取cookies
  const cookies = await context.cookies();
  const cookieStr = cookies.map(c => c.name + "=" + c.value).join("; ");

  const session = {
    cookies: cookieStr,
    csrfToken,
    xDlpj,
    xGwReferer,
    reportName: report.name,
    reportId: report.id,
    templates,
    tabs: tabTexts,
    capturedAt: new Date().toISOString(),
  };
  
  fs.writeFileSync("/tmp/bi-session.json", JSON.stringify(session, null, 2));
  console.log("  Session已保存 (" + Object.keys(templates).length + " 个组件模板)");

  await browser.close();
  return session;
}

// 如果直接运行
const reportIdx = parseInt(process.argv[2] || "1");
getSession(reportIdx).catch(e => { console.error("FATAL:", e.message); process.exit(1); });
