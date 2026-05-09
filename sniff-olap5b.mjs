import { chromium } from "playwright";

const URL = "https://bi.aliyuncs.com/token3rd/report/view.htm?id=bae08b94-8dad-4691-a7ca-9783de160a39&accessTicket=c5e15c05-565c-4aa5-859c-fe2c93910c44&dd_orientation=auto";

async function main() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  const page = await context.newPage();

  let finalResult = null;
  let pollCount = 0;

  page.on("response", async resp => {
    const url = resp.url();
    if (url.includes("queryByPollKey")) {
      pollCount++;
      let body = "";
      try { body = await resp.text(); } catch(e) { console.log("resp error:", e.message); return; }
      try {
        const parsed = JSON.parse(body);
        console.log("Poll #" + pollCount + " normalResult=" + parsed.data.normalResult + " bodySize=" + body.length);
        if (parsed.data && parsed.data.normalResult) {
          finalResult = parsed;
        }
      } catch(e) { console.log("parse error:", e.message); }
    }
  });

  console.log("Loading...");
  await page.goto(URL, { waitUntil: "networkidle", timeout: 90000 });
  console.log("Page loaded, waiting...");
  await page.waitForTimeout(30000);
  console.log("Done waiting, polls:", pollCount);

  if (finalResult) {
    const v = finalResult.data.value;
    
    console.log("\n===== 列名映射 =====");
    v.columns.forEach((col, i) => {
      const cell = col.cells[0];
      console.log(i + ": " + cell.value + " (type:" + cell.dataType + ", pathId:" + cell.props.pathId + ")");
    });
    
    console.log("\npage:", JSON.stringify(v.page));
    console.log("total rows:", v.values.length);
    
    // 找有数据的行
    let shown = 0;
    for (let i = 0; i < v.values.length && shown < 5; i++) {
      const row = v.values[i];
      const hasData = row.some((c, j) => j >= 5 && c.v !== null && c.v !== "0");
      if (hasData) {
        const display = row.map((c, j) => v.columns[j].cells[0].value + "=" + c.v).join(" | ");
        console.log("Sample: " + display);
        shown++;
      }
    }
  } else {
    console.log("No final result obtained");
  }

  await browser.close();
}
main().catch(e => console.error("FATAL:", e.message));
