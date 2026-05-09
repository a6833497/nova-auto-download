import { chromium } from "playwright";

const URL = "https://bi.aliyuncs.com/token3rd/report/view.htm?id=bae08b94-8dad-4691-a7ca-9783de160a39&accessTicket=c5e15c05-565c-4aa5-859c-fe2c93910c44&dd_orientation=auto";

async function main() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  const page = await context.newPage();

  let finalResult = null;
  let queryBodyRaw = null;

  await page.route("**/*", async (route, request) => {
    const url = request.url();
    if (url.includes("olap/query") && !url.includes("queryByPollKey")) {
      const pd = request.postData();
      if (pd) {
        const params = new URLSearchParams(pd);
        queryBodyRaw = params.get("olapQueryParam");
      }
    }
    await route.continue();
  });

  page.on("response", async resp => {
    const url = resp.url();
    if (url.includes("queryByPollKey")) {
      let body = "";
      try { body = await resp.text(); } catch {}
      try {
        const parsed = JSON.parse(body);
        if (parsed.data && parsed.data.normalResult) {
          finalResult = parsed;
        }
      } catch {}
    }
  });

  console.log("Loading page...");
  await page.goto(URL, { waitUntil: "networkidle", timeout: 90000 });
  await page.waitForTimeout(30000);

  // === 分析beforeAggregateCondition（公会过滤） ===
  if (queryBodyRaw) {
    const qb = JSON.parse(queryBodyRaw);
    console.log("\n===== 公会过滤条件 (beforeAggregateCondition) =====");
    const bac = qb.configs.find(c => c.type === "beforeAggregateCondition");
    if (bac) {
      console.log(JSON.stringify(bac, null, 2));
    }
    
    // 查找所有包含日期的config
    console.log("\n===== 日期相关 =====");
    const fullStr = JSON.stringify(qb);
    const dateMatches = fullStr.match(/\d{4}-\d{2}-\d{2}/g);
    console.log("日期值:", dateMatches ? [...new Set(dateMatches)] : "无日期值");
    
    // 查找condition类型
    for (const cfg of qb.configs) {
      if (cfg.type.includes("ondition") || cfg.type.includes("filter")) {
        console.log("条件/过滤 config type:", cfg.type);
        console.log(JSON.stringify(cfg, null, 2).substring(0, 2000));
      }
    }
    
    // 完整config types列表
    console.log("\n===== 所有config types =====");
    console.log(qb.configs.map(c => c.type).join(", "));
  }

  // === 分析response数据格式 ===
  if (finalResult) {
    const v = finalResult.data.value;
    console.log("\n===== RESPONSE value结构 =====");
    console.log("value顶层keys:", Object.keys(v).join(", "));
    
    if (v.rows) {
      console.log("\nrows类型:", typeof v.rows, Array.isArray(v.rows) ? "数组长度=" + v.rows.length : "");
      console.log("rows前3:", JSON.stringify(v.rows.slice(0,3)).substring(0, 2000));
    }
    if (v.columns) {
      console.log("\ncolumns类型:", typeof v.columns, Array.isArray(v.columns) ? "数组长度=" + v.columns.length : "");
      console.log("columns:", JSON.stringify(v.columns).substring(0, 3000));
    }
    if (v.values) {
      console.log("\nvalues类型:", typeof v.values, Array.isArray(v.values) ? "数组长度=" + v.values.length : "");
      console.log("values前3行:", JSON.stringify(v.values.slice(0,3)).substring(0, 3000));
    }
    if (v.page) {
      console.log("\npage:", JSON.stringify(v.page));
    }
    if (v.extra) {
      console.log("\nextra keys:", Object.keys(v.extra).join(", "));
      console.log("extra:", JSON.stringify(v.extra).substring(0, 1000));
    }
  } else {
    console.log("\n没有获取到最终数据(normalResult仍为false), 可能需要更长等待");
  }

  await browser.close();
}
main().catch(e => console.error(e.message));
