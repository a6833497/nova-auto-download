/**
 * Nova API数据下载 v1
 *
 * 核心思路：
 * 1. Playwright只用一次：打开BI页面获取session + 拦截所有tab的olap/query请求模板
 * 2. 之后用fetch直接调用olap/query API获取JSON数据
 * 3. 将JSON数据转换为表格格式保存
 *
 * 使用方式：
 *   node api-download.mjs [日期] [--test]
 *   例：node api-download.mjs 2026-04-27
 *   例：node api-download.mjs --test        # 只处理第1个报表
 */
import { chromium } from "playwright";
import fs from "fs";
import path from "path";

// ========== 配置 ==========
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

// 需要下载的tab（组件名包含这些关键词的）
const TARGET_TABS = [
  "日主播工作数据",
  "每日主播数据",
  "公会数据",
  "1V1主播薪资奖励",
  "语音房主播薪资奖励",
  "语音房主播行为数据",
  "语音房数据",
];

const POLL_TIMEOUT_MS = 120000;
const POLL_INTERVAL_MS = 2000;

const args = process.argv.slice(2);
const testMode = args.includes("--test");
const dateArg = args.find(a => /^\d{4}-\d{2}-\d{2}$/.test(a));

const targetDate = dateArg || (() => {
  const d = new Date();
  d.setDate(d.getDate() - 1);
  return d.toISOString().slice(0, 10);
})();

function getISOWeek(dateStr) {
  const date = new Date(dateStr + "T12:00:00");
  const dayNum = date.getDay() || 7;
  date.setDate(date.getDate() + 4 - dayNum);
  const yearStart = new Date(date.getFullYear(), 0, 1);
  return Math.ceil((((date - yearStart) / 86400000) + 1) / 7);
}

function getISOWeekYear(dateStr) {
  const date = new Date(dateStr + "T12:00:00");
  const dayNum = date.getDay() || 7;
  date.setDate(date.getDate() + 4 - dayNum);
  return date.getFullYear();
}

const weekNum = getISOWeek(targetDate);
const weekYear = getISOWeekYear(targetDate);
const weekStr = weekYear + "-" + weekNum;

const downloadDir = "/home/ubuntu/nova-data/upload-staging/daily/" + targetDate;
fs.mkdirSync(downloadDir, { recursive: true });

const startTime = Date.now();

console.log("=========================================");
console.log("  Nova API 数据下载 v1");
console.log("  日期: " + targetDate + " (第" + weekNum + "周, " + weekStr + ")");
console.log("  报表: " + (testMode ? "仅第1个" : REPORTS.length + "个"));
console.log("=========================================\n");

// ========== Session获取 ==========

function buildUrl(report) {
  return "https://bi.aliyuncs.com/token3rd/report/view.htm?id=" + report.id +
    "&accessTicket=" + report.ticket + "&dd_orientation=auto";
}

async function getSessionAndTemplates(report) {
  const url = buildUrl(report);
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1920, height: 1080 } });
  const page = await context.newPage();

  let csrfToken = null;
  let xDlpj = null;
  let xGwReferer = null;
  const templates = {};

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
      try { parsed = JSON.parse(olapParam); } catch (e) {}

      templates[componentId] = {
        componentId,
        componentName: parsed ? parsed.componentName : null,
        reportId: params.get("reportId"),
        componentType: params.get("componentType"),
        olapQueryParam: olapParam,
      };
    }
    await route.continue();
  });

  await page.goto(url, { waitUntil: "networkidle", timeout: 90000 });
  await page.waitForTimeout(8000);

  // 获取tab列表并逐个点击
  const tabTexts = await page.evaluate(() => {
    const container = document.querySelector(".quick-report-preview-sheet-tabs");
    if (!container) return [];
    const spans = container.querySelectorAll("span");
    return Array.from(spans).map(s => s.textContent.trim()).filter(t => t.length > 0);
  });

  for (const tabName of tabTexts) {
    try {
      const sel = ".quick-report-preview-sheet-tabs span";
      const allSpans = await page.locator(sel).all();
      for (const span of allSpans) {
        const text = await span.textContent();
        if (text.trim() === tabName) {
          await span.click();
          await page.waitForTimeout(2000);
          await page.waitForLoadState("networkidle", { timeout: 15000 }).catch(() => {});
          await page.waitForTimeout(2000);
          break;
        }
      }
    } catch (e) {}
  }

  const cookies = await context.cookies();
  const cookieStr = cookies.map(c => c.name + "=" + c.value).join("; ");

  await browser.close();

  return {
    cookies: cookieStr,
    csrfToken,
    xDlpj,
    xGwReferer,
    templates,
    tabs: tabTexts,
  };
}

// ========== API查询 ==========

async function queryComponent(session, componentId, olapQueryParam, reportId, componentType) {
  const baseHeaders = {
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Cookie": session.cookies,
    "X-Csrf-Token": session.csrfToken,
    "Origin": "https://bi.aliyuncs.com",
    "X-Requested-With": "XMLHttpRequest",
    "X-Dlpj": session.xDlpj || "",
    "X-Gw-Referer": session.xGwReferer || "",
    "Referer": session.xGwReferer || "",
  };

  const body = new URLSearchParams({
    olapQueryParam,
    componentId,
    reportId,
    componentType,
  }).toString();

  const resp = await fetch("https://bi.aliyuncs.com/api/v2/olap/query", {
    method: "POST",
    headers: baseHeaders,
    body,
  });

  if (!resp.ok) {
    throw new Error("HTTP " + resp.status);
  }

  const data = await resp.json();
  if (!data.success) {
    throw new Error(data.message || data.code || "query failed");
  }

  const result = data.data;

  // 直接返回
  if (result.normalResult && result.value) {
    return result.value;
  }

  // 需要poll
  if (!result.pollKey) {
    throw new Error("No pollKey and no result");
  }

  const pollHeaders = {
    "Cookie": session.cookies,
    "X-Csrf-Token": session.csrfToken,
    "X-Requested-With": "XMLHttpRequest",
    "X-Dlpj": session.xDlpj || "",
    "X-Gw-Referer": session.xGwReferer || "",
  };

  const deadline = Date.now() + POLL_TIMEOUT_MS;
  let attempts = 0;

  while (Date.now() < deadline) {
    await new Promise(r => setTimeout(r, POLL_INTERVAL_MS));
    attempts++;

    const pollUrl = "https://bi.aliyuncs.com/api/v2/olap/queryByPollKey?componentId=" +
      componentId + "&pollKey=" + encodeURIComponent(result.pollKey);
    const resp2 = await fetch(pollUrl, { headers: pollHeaders });
    const data2 = await resp2.json();

    if (data2.data && data2.data.normalResult) {
      if (data2.data.value) {
        return data2.data.value;
      }
      throw new Error("BI查询失败(normalResult=true但无数据)");
    }
  }

  throw new Error("Poll超时 (" + attempts + "次, " + (POLL_TIMEOUT_MS / 1000) + "s)");
}

// ========== 日期修改 ==========

function updateWeekInTemplate(olapQueryParam, targetWeekStr) {
  const param = JSON.parse(olapQueryParam);
  for (const cfg of param.configs || []) {
    if (cfg.type === "beforeAggregateCondition") {
      updateWeekInCondition(cfg.config, targetWeekStr);
    }
  }
  return JSON.stringify(param);
}

function updateWeekInCondition(condition, targetWeekStr) {
  if (!condition) return;

  if (condition.field && condition.field.dateTrunc === "week") {
    if (condition.args) {
      for (const arg of condition.args) {
        if (arg.valueType === "string" && /^\d{4}-\d+$/.test(arg.value)) {
          arg.value = targetWeekStr;
        }
      }
    }
  }

  if (condition.conditions) {
    for (const sub of condition.conditions) {
      updateWeekInCondition(sub, targetWeekStr);
    }
  }
}

// ========== 数据转换 ==========

function convertToRows(value) {
  const columns = value.columns.map(col => {
    const cell = col.cells[0];
    return {
      name: cell.value,
      pathId: cell.props.pathId,
      dataType: cell.dataType,
    };
  });

  const headers = columns.map(c => c.name);
  const rows = [];

  for (const rowData of value.values || []) {
    const row = {};
    for (let i = 0; i < columns.length; i++) {
      const cell = rowData[i];
      row[columns[i].name] = cell ? cell.v : null;
    }
    rows.push(row);
  }

  return { headers, rows, columns };
}

// ========== 主流程 ==========

async function processReport(report) {
  const label = report.name;
  const results = {};

  try {
    const session = await getSessionAndTemplates(report);
    console.log("[" + label + "] Session获取完成 (" + Object.keys(session.templates).length + " 组件)");

    for (const [cid, tpl] of Object.entries(session.templates)) {
      // 从componentName提取tab名：'日主播工作数据'!B1 → 日主播工作数据
      const rawName = tpl.componentName || "";
      const tabName = rawName.replace(/^'/, "").replace(/'!.*$/, "");

      const isTarget = TARGET_TABS.some(t => tabName.includes(t));
      if (!isTarget) {
        console.log("[" + label + "] 跳过: " + tabName);
        continue;
      }

      console.log("[" + label + "] 查询: " + tabName + " (" + cid + ")...");

      try {
        const updatedParam = updateWeekInTemplate(tpl.olapQueryParam, weekStr);
        const start = Date.now();
        const value = await queryComponent(session, cid, updatedParam, tpl.reportId, tpl.componentType);
        const elapsed = ((Date.now() - start) / 1000).toFixed(1);

        const { headers, rows } = convertToRows(value);
        console.log("[" + label + "] " + tabName + ": " + rows.length + " 行, " + headers.length + " 列 (" + elapsed + "s)");

        // 保存JSON
        const fileName = label + "_" + tabName + ".json";
        const filePath = path.join(downloadDir, fileName);
        fs.writeFileSync(filePath, JSON.stringify({
          headers,
          rows,
          meta: {
            reportName: label,
            tabName,
            componentId: cid,
            weekStr,
            date: targetDate,
            rowCount: rows.length,
            colCount: headers.length,
            queriedAt: new Date().toISOString(),
          },
        }));

        results[tabName] = {
          success: true,
          rows: rows.length,
          cols: headers.length,
          file: filePath,
        };
      } catch (e) {
        console.error("[" + label + "] " + tabName + " 失败: " + e.message);
        results[tabName] = { success: false, error: e.message };
      }
    }
  } catch (e) {
    console.error("[" + label + "] Session获取失败: " + e.message);
    results._session = { success: false, error: e.message };
  }

  return results;
}

async function main() {
  const reports = testMode ? [REPORTS[0]] : REPORTS;
  const allResults = {};

  for (const report of reports) {
    console.log("\n--- " + report.name + " ---");
    allResults[report.name] = await processReport(report);
  }

  // 汇总
  const elapsed = Math.round((Date.now() - startTime) / 1000);
  console.log("\n=========================================");
  console.log("  汇总 (" + elapsed + "秒)");
  console.log("=========================================");

  let totalSuccess = 0;
  let totalFail = 0;

  for (const [reportName, tabs] of Object.entries(allResults)) {
    const successes = Object.values(tabs).filter(t => t.success);
    const failures = Object.values(tabs).filter(t => !t.success);
    totalSuccess += successes.length;
    totalFail += failures.length;

    if (failures.length === 0) {
      console.log("  " + reportName + ": 全部成功 (" + successes.length + " tabs)");
    } else {
      console.log("  " + reportName + ": " + successes.length + " 成功, " + failures.length + " 失败");
      for (const [tab, result] of Object.entries(tabs)) {
        if (!result.success) {
          console.log("    失败: " + tab + " - " + result.error);
        }
      }
    }
  }

  console.log("\n  总计: " + totalSuccess + " 成功, " + totalFail + " 失败");

  // 保存汇总
  fs.writeFileSync(
    path.join(downloadDir, "_api_download_summary.json"),
    JSON.stringify({
      date: targetDate,
      weekStr,
      elapsed,
      results: allResults,
      completedAt: new Date().toISOString(),
    }, null, 2)
  );

  if (totalFail > 0) {
    process.exit(1);
  }
}

main().catch(e => {
  console.error("FATAL:", e.message);
  process.exit(2);
});
