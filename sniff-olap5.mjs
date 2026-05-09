import { chromium } from "playwright";

const URL = "https://bi.aliyuncs.com/token3rd/report/view.htm?id=bae08b94-8dad-4691-a7ca-9783de160a39&accessTicket=c5e15c05-565c-4aa5-859c-fe2c93910c44&dd_orientation=auto";

async function main() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  const page = await context.newPage();

  let finalResult = null;

  await page.route("**/*", async (route) => { await route.continue(); });

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

  await page.goto(URL, { waitUntil: "networkidle", timeout: 90000 });
  await page.waitForTimeout(30000);

  if (finalResult) {
    const v = finalResult.data.value;
    
    // 列名映射
    console.log("===== 列名映射 =====");
    v.columns.forEach((col, i) => {
      const cell = col.cells[0];
      console.log(i + ": " + cell.value + " (type:" + cell.dataType + ", pathId:" + cell.props.pathId + ", guid:" + cell.props.guid + ")");
    });
    
    console.log("\n===== page信息 =====");
    console.log(JSON.stringify(v.page));
    
    console.log("\n===== 数据样本(有数据的行) =====");
    let shown = 0;
    for (let i = 0; i < v.values.length && shown < 5; i++) {
      const row = v.values[i];
      // 找有实际数据的行
      const hasData = row.some((c, j) => j >= 5 && c.v !== null && c.v !== "0");
      if (hasData) {
        const display = row.map((c, j) => v.columns[j].cells[0].value + "=" + c.v).join(" | ");
        console.log("Row " + i + ": " + display);
        shown++;
      }
    }
    
    console.log("\n===== 数据总行数 =====");
    console.log("values.length:", v.values.length);
    console.log("exceedMaxCount:", v.extra.exceedMaxCount);
  }

  await browser.close();
}
main().catch(e => console.error(e.message));
