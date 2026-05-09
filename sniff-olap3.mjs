import { chromium } from "playwright";

const URL = "https://bi.aliyuncs.com/token3rd/report/view.htm?id=bae08b94-8dad-4691-a7ca-9783de160a39&accessTicket=c5e15c05-565c-4aa5-859c-fe2c93910c44&dd_orientation=auto";

async function main() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  const page = await context.newPage();

  let queryBody = null;
  let finalResult = null;

  await page.route("**/*", async (route, request) => {
    const url = request.url();
    if (url.includes("olap/query") && !url.includes("queryByPollKey")) {
      const pd = request.postData();
      if (pd) {
        const params = new URLSearchParams(pd);
        const raw = params.get("olapQueryParam");
        if (raw) queryBody = JSON.parse(raw);
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

  // === Analyze query body ===
  if (queryBody) {
    console.log("\n===== 1. POST BODY 顶层结构 =====");
    console.log("顶层key:", Object.keys(queryBody).join(", "));
    console.log("componentId:", queryBody.componentId);
    console.log("componentName:", queryBody.componentName);
    console.log("cubeId:", queryBody.cubeId);
    console.log("isLazyLoad:", queryBody.isLazyLoad);
    console.log("datasetId:", queryBody.datasetId);

    console.log("\n===== 2. CONFIGS 结构 =====");
    if (queryBody.configs) {
      for (const cfg of queryBody.configs) {
        console.log("\n  Config type:", cfg.type);
        if (cfg.type === "field" && cfg.config && cfg.config.fields) {
          console.log("  Fields count:", cfg.config.fields.length);
          for (const f of cfg.config.fields) {
            console.log("    - guid:", f.guid, "fid:", f.fid, "area:", f.areaType);
          }
        }
        if (cfg.type === "filter") {
          console.log("  Filter config:", JSON.stringify(cfg.config).substring(0, 2000));
        }
        if (cfg.type === "sort") {
          console.log("  Sort config:", JSON.stringify(cfg.config).substring(0, 500));
        }
        if (cfg.type === "limit") {
          console.log("  Limit config:", JSON.stringify(cfg.config).substring(0, 500));
        }
        if (cfg.type !== "field" && cfg.type !== "filter" && cfg.type !== "sort" && cfg.type !== "limit") {
          console.log("  Config:", JSON.stringify(cfg.config).substring(0, 1000));
        }
      }
    }

    console.log("\n===== 3. FILTERS 分析 =====");
    if (queryBody.filters) {
      console.log("顶层filters:", JSON.stringify(queryBody.filters).substring(0, 3000));
    }
    // Search for date/filter in all configs
    const fullStr = JSON.stringify(queryBody);
    // Find date patterns
    const dateMatches = fullStr.match(/\d{4}-\d{2}-\d{2}/g);
    if (dateMatches) {
      console.log("发现的日期值:", [...new Set(dateMatches)].join(", "));
    }
    // Find admin_name
    if (fullStr.includes("admin_name")) {
      console.log("包含admin_name: YES");
      // Extract surrounding context
      const idx = fullStr.indexOf("admin_name");
      console.log("admin_name上下文:", fullStr.substring(Math.max(0,idx-100), idx+200));
    }

    console.log("\n===== 4. 完整POST Body (前8000字符) =====");
    console.log(JSON.stringify(queryBody, null, 2).substring(0, 8000));
  }

  // === Analyze response ===
  if (finalResult) {
    console.log("\n===== 5. RESPONSE 结构 =====");
    console.log("顶层key:", Object.keys(finalResult).join(", "));
    console.log("success:", finalResult.success);
    const data = finalResult.data;
    if (data) {
      console.log("data keys:", Object.keys(data).join(", "));
      if (data.value) {
        const v = data.value;
        console.log("value keys:", Object.keys(v).join(", "));
        if (v.header) {
          console.log("header (列名):", JSON.stringify(v.header).substring(0, 3000));
        }
        if (v.data) {
          console.log("data行数:", v.data.length);
          console.log("前3行:", JSON.stringify(v.data.slice(0,3)).substring(0, 3000));
        }
        if (v.axisData) {
          console.log("axisData:", JSON.stringify(v.axisData).substring(0, 1000));
        }
      }
      if (data.axisResult) {
        console.log("axisResult keys:", Object.keys(data.axisResult).join(", "));
        const ar = data.axisResult;
        if (ar.header) console.log("axisResult.header:", JSON.stringify(ar.header).substring(0, 3000));
        if (ar.data) {
          console.log("axisResult.data行数:", ar.data.length);
          console.log("axisResult.data前3行:", JSON.stringify(ar.data.slice(0,3)).substring(0, 3000));
        }
      }
      if (data.oldQueryResult) {
        console.log("oldQueryResult:", JSON.stringify(data.oldQueryResult).substring(0, 2000));
      }
    }
  } else {
    console.log("\n没有获取到最终数据结果(normalResult=false)");
  }

  await browser.close();
}
main().catch(e => console.error(e.message));
