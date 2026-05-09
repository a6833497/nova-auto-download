const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const reports = [
  { name: "巴西2-Evian", url: "https://bi.aliyuncs.com/token3rd/report/view.htm?id=30b58907-ae0c-407d-b3f0-e52d09f71e6b&accessTicket=949597c4-18bd-4bff-b692-6f606a1cd327&dd_orientation=auto" },
  { name: "巴西4-Doce", url: "https://bi.aliyuncs.com/token3rd/report/view.htm?id=2034e69d-3c70-4b43-8a70-47f3cb8a45a5&accessTicket=a9e70ecc-0acf-4562-947c-6cd2d0fe129a&dd_orientation=auto" },
];

// 需要重下的日期
const dates = [];
for (let d = 1; d <= 19; d++) {
  const ds = `2026-04-${String(d).padStart(2, "0")}`;
  const dir = `/home/ubuntu/nova-data/upload-staging/daily/${ds}`;
  if (fs.existsSync(dir)) dates.push(ds);
}

console.log(`需要重下 ${dates.length} 天 x ${reports.length} 个报表 = ${dates.length * reports.length} 个文件`);

(async () => {
  const browser = await chromium.launch({ headless: true });

  for (const report of reports) {
    console.log(`\n=== ${report.name} ===`);
    const page = await browser.newPage({ viewport: { width: 1920, height: 1080 }, acceptDownloads: true });

    try {
      await page.goto(report.url, { waitUntil: "domcontentloaded", timeout: 120000 });
      await page.waitForTimeout(20000);

      // 切换到主播明细tab
      const dataTab = page.locator("text=每日主播数据").or(page.locator("text=日主播工作数据")).or(page.locator("text=主播数据")).first();
      if (await dataTab.isVisible({ timeout: 5000 }).catch(() => false)) {
        await dataTab.click();
        console.log("  切换到主播明细视图");
        await page.waitForTimeout(15000);
      }

      for (const targetDate of dates) {
        try {
          // 设置日期 - 找Week Picker并设置
          const weekInput = page.locator(".ant-picker-input input").first();
          if (await weekInput.isVisible({ timeout: 3000 }).catch(() => false)) {
            await weekInput.click();
            await page.waitForTimeout(500);
            // 清除并输入日期
            await weekInput.fill("");
            await weekInput.type(targetDate.replace(/-/g, "/"));
            await page.keyboard.press("Enter");
            await page.waitForTimeout(1000);
          }

          // 点查询
          const queryBtn = page.locator('button:has-text("查 询"), button:has-text("查询")').first();
          if (await queryBtn.isVisible({ timeout: 3000 }).catch(() => false)) {
            await queryBtn.click();
            await page.waitForTimeout(20000);
          }

          // 导出
          await page.locator("text=导出").first().click();
          await page.waitForTimeout(2000);
          const cs = page.locator('label.ant-radio-wrapper:has-text("当前sheet页")').first();
          if (await cs.isVisible({ timeout: 3000 }).catch(() => false)) await cs.click();
          await page.waitForTimeout(500);

          const downloadPromise = page.waitForEvent("download", { timeout: 120000 }).catch(() => null);
          const primaryBtns = await page.locator("button.ant-btn-primary").all();
          for (let bi = primaryBtns.length - 1; bi >= 0; bi--) {
            const text = await primaryBtns[bi].textContent();
            if (text.includes("确")) { await primaryBtns[bi].click(); break; }
          }

          const download = await downloadPromise;
          if (download) {
            const dir = `/home/ubuntu/nova-data/upload-staging/daily/${targetDate}`;
            const filePath = path.join(dir, `${report.name}_${targetDate}.xlsx`);
            await download.saveAs(filePath);
            const size = fs.statSync(filePath).size;
            console.log(`  ${targetDate}: ${Math.round(size / 1024)}KB`);
          } else {
            console.log(`  ${targetDate}: 下载超时`);
          }
        } catch (e) {
          console.log(`  ${targetDate}: 错误 ${e.message.slice(0, 50)}`);
        }
      }
    } catch (e) {
      console.log(`  初始化失败: ${e.message.slice(0, 80)}`);
    }
    await page.close();
  }

  await browser.close();
  console.log("\n完成");
})();
