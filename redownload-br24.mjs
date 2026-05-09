/**
 * 补下载巴西2/4 主播明细数据
 * 用法: node redownload-br24.mjs 2026-04-14
 * 或:   node redownload-br24.mjs 2026-04-14 2026-04-19  (范围)
 */
import { chromium } from "playwright";
import fs from "fs";
import path from "path";

const REPORTS = [
  { name: "巴西2-Evian", url: "https://bi.aliyuncs.com/token3rd/report/view.htm?id=30b58907-ae0c-407d-b3f0-e52d09f71e6b&accessTicket=949597c4-18bd-4bff-b692-6f606a1cd327&dd_orientation=auto" },
  { name: "巴西4-Doce",  url: "https://bi.aliyuncs.com/token3rd/report/view.htm?id=2034e69d-3c70-4b43-8a70-47f3cb8a45a5&accessTicket=a9e70ecc-0acf-4562-947c-6cd2d0fe129a&dd_orientation=auto" },
];

const args = process.argv.slice(2);
const startDate = args[0];
const endDate = args[1] || startDate;
if (!startDate) { console.error("用法: node redownload-br24.mjs YYYY-MM-DD [YYYY-MM-DD]"); process.exit(1); }

function dateRange(start, end) {
  const dates = [];
  const d = new Date(start + "T00:00:00");
  const e = new Date(end + "T00:00:00");
  while (d <= e) {
    const yyyy = d.getFullYear(); const mm = String(d.getMonth()+1).padStart(2,"0"); const dd = String(d.getDate()).padStart(2,"0"); dates.push(yyyy+"-"+mm+"-"+dd);
    d.setDate(d.getDate() + 1);
  }
  return dates;
}

function getISOWeek(dateStr) {
  const date = new Date(dateStr + "T12:00:00");
  const dayNum = date.getDay() || 7;
  date.setDate(date.getDate() + 4 - dayNum);
  const yearStart = new Date(date.getFullYear(), 0, 1);
  return Math.ceil((((date - yearStart) / 86400000) + 1) / 7);
}

async function setWeekPicker(page, targetDate, label) {
  const [targetYear, targetMonth] = targetDate.split("-").map(Number);
  const weekNum = getISOWeek(targetDate);
  const weekInput = page.locator(".ant-picker-range input").first();
  if (!await weekInput.isVisible({ timeout: 5000 }).catch(() => false)) {
    console.log(label + ": 未找到 Week 选择器");
    return false;
  }
  const currentVal = await weekInput.inputValue().catch(() => "");
  const targetWeekStr = targetYear + "-" + weekNum + " 周";
  if (currentVal === targetWeekStr) {
    console.log(label + ": Week 已是 " + targetWeekStr);
    return true;
  }
  await weekInput.click();
  await page.waitForTimeout(1000);
  const panelVisible = await page.locator(".ant-picker-panel").first().isVisible({ timeout: 3000 }).catch(() => false);
  if (!panelVisible) { console.log(label + ": Week 面板未打开"); return false; }

  for (let attempt = 0; attempt < 36; attempt++) {
    const headerTexts = await page.locator(".ant-picker-header-view").allTextContents().catch(() => []);
    const leftHeader = headerTexts[0] || "";
    const match = leftHeader.match(/(\d{4}).*?(\d{1,2})/);
    if (!match) break;
    const panelYear = parseInt(match[1]);
    const panelMonth = parseInt(match[2]);
    const rightYear = panelMonth === 12 ? panelYear + 1 : panelYear;
    const rightMonth = panelMonth === 12 ? 1 : panelMonth + 1;
    if ((panelYear === targetYear && panelMonth === targetMonth) || (rightYear === targetYear && rightMonth === targetMonth)) break;
    const panelDateNum = panelYear * 12 + panelMonth;
    const targetDateNum = targetYear * 12 + targetMonth;
    const diff = Math.abs(panelDateNum - targetDateNum);
    if (panelDateNum > targetDateNum) {
      if (diff >= 12) await page.locator(".ant-picker-header-super-prev-btn").first().click();
      else await page.locator(".ant-picker-header-prev-btn").first().click();
    } else {
      if (diff >= 12) await page.locator(".ant-picker-header-super-next-btn").last().click();
      else await page.locator(".ant-picker-header-next-btn").last().click();
    }
    await page.waitForTimeout(300);
  }

  const cellSelector = "td[title=\"" + targetDate + "\"]";
  let cell = page.locator(cellSelector).first();
  if (!await cell.isVisible({ timeout: 3000 }).catch(() => false)) {
    await page.locator(".ant-picker-header-prev-btn").first().click();
    await page.waitForTimeout(500);
    cell = page.locator(cellSelector).first();
    if (!await cell.isVisible({ timeout: 2000 }).catch(() => false)) {
      const d2 = new Date(targetDate + "T00:00:00");
      const dayOfWeek = d2.getDay() || 7;
      const monday = new Date(d2);
      monday.setDate(d2.getDate() - dayOfWeek + 1);
      let found = false;
      for (let offset = 0; offset < 7; offset++) {
        const tryDate = new Date(monday);
        tryDate.setDate(monday.getDate() + offset);
        const tryStr = tryDate.toISOString().slice(0, 10);
        const tryCell = page.locator("td[title=\"" + tryStr + "\"]").first();
        if (await tryCell.isVisible({ timeout: 1000 }).catch(() => false)) { cell = tryCell; found = true; break; }
      }
      if (!found) {
        const nextBtn = page.locator(".ant-picker-header-next-btn").last();
        await nextBtn.click(); await nextBtn.click();
        await page.waitForTimeout(500);
        cell = page.locator(cellSelector).first();
        if (!await cell.isVisible({ timeout: 2000 }).catch(() => false)) {
          console.log(label + ": 目标日期不可见");
          await page.keyboard.press("Escape");
          return false;
        }
      }
    }
  }
  await cell.click();
  await page.waitForTimeout(800);
  const cell2 = page.locator(cellSelector).first();
  if (await cell2.isVisible({ timeout: 3000 }).catch(() => false)) {
    await cell2.click();
  } else {
    const d3 = new Date(targetDate + "T00:00:00");
    const dayOfWeek2 = d3.getDay() || 7;
    const monday2 = new Date(d3);
    monday2.setDate(d3.getDate() - dayOfWeek2 + 1);
    for (let offset = 0; offset < 7; offset++) {
      const tryDate = new Date(monday2);
      tryDate.setDate(monday2.getDate() + offset);
      const tryStr = tryDate.toISOString().slice(0, 10);
      const tryCell = page.locator("td[title=\"" + tryStr + "\"]").first();
      if (await tryCell.isVisible({ timeout: 1000 }).catch(() => false)) { await tryCell.click(); break; }
    }
  }
  await page.waitForTimeout(500);
  const newVal = await weekInput.inputValue().catch(() => "");
  console.log(label + ": Week " + currentVal + " -> " + newVal);
  return true;
}

async function downloadForDate(browser, report, targetDate) {
  const label = report.name + " " + targetDate;
  const downloadDir = "/home/ubuntu/nova-data/upload-staging/daily/" + targetDate;
  fs.mkdirSync(downloadDir, { recursive: true });
  const context = await browser.newContext({ acceptDownloads: true, viewport: { width: 1920, height: 1080 } });
  const page = await context.newPage();
  page.setDefaultTimeout(120000);
  try {
    console.log(label + ": 打开页面...");
    await page.goto(report.url, { waitUntil: "domcontentloaded", timeout: 120000 });
    await page.waitForTimeout(25000);
    const dataTab = page.locator("text=每日主播数据").or(page.locator("text=日主播工作数据")).or(page.locator("text=主播数据")).first();
    if (await dataTab.isVisible({ timeout: 5000 }).catch(() => false)) {
      await dataTab.click();
      console.log(label + ": 已切换到主播数据 tab");
      await page.waitForTimeout(25000);
    } else {
      console.log(label + ": 未找到主播数据 tab");
    }
    const weekOk = await setWeekPicker(page, targetDate, label);
    if (!weekOk) console.log(label + ": 周选择器设置失败");
    await page.waitForTimeout(1000);
    const queryBtn = page.locator("button:has-text(\"查 询\"), button:has-text(\"查询\"), span:has-text(\"查 询\")").first();
    if (await queryBtn.isVisible({ timeout: 3000 }).catch(() => false)) {
      await queryBtn.click();
      console.log(label + ": 查询已点击，等待数据...");
      await page.waitForTimeout(30000);
    }
    await page.locator("text=导出").first().click();
    await page.waitForTimeout(2000);
    const cs = page.locator("label.ant-radio-wrapper:has-text(\"当前sheet页\")").first();
    if (await cs.isVisible({ timeout: 3000 }).catch(() => false)) await cs.click();
    await page.waitForTimeout(500);
    const downloadPromise = page.waitForEvent("download", { timeout: 120000 }).catch(() => null);
    const primaryBtns = await page.locator("button.ant-btn-primary").all();
    for (let bi = primaryBtns.length - 1; bi >= 0; bi--) {
      const text = await primaryBtns[bi].textContent();
      if (text.includes("确")) { await primaryBtns[bi].click(); break; }
    }
    const download = await downloadPromise;
    const fileName = report.name + "_" + targetDate + ".xlsx";
    const filePath = path.join(downloadDir, fileName);
    if (download) {
      await download.saveAs(filePath);
      const size = fs.statSync(filePath).size;
      if (size < 50 * 1024) console.log(label + ": 文件过小 (" + Math.round(size / 1024) + "KB)");
      else console.log(label + ": OK " + Math.round(size / 1024) + "KB");
      await context.close();
      return { filePath, size, ok: true };
    }
    console.log(label + ": 无下载");
    await context.close();
    return { ok: false };
  } catch (error) {
    console.error(label + ": ERROR " + error.message.split("\n")[0]);
    await page.screenshot({ path: path.join(downloadDir, report.name + "_error_" + targetDate + ".png") }).catch(() => {});
    await context.close();
    return { ok: false };
  }
}

async function main() {
  const dates = dateRange(startDate, endDate);
  console.log("=========================================");
  console.log("  补下载巴西2/4 主播明细");
  console.log("  日期范围: " + startDate + " ~ " + endDate + " (" + dates.length + "天)");
  console.log("  报表: " + REPORTS.map(r => r.name).join(", "));
  console.log("=========================================\n");
  const browser = await chromium.launch({ headless: true });
  const results = [];
  for (const date of dates) {
    console.log("\n>>> 处理日期: " + date + " <<<");
    for (const report of REPORTS) {
      const r = await downloadForDate(browser, report, date);
      results.push({ date, report: report.name, ...r });
    }
  }
  await browser.close();
  console.log("\n=========== 汇总 ===========");
  const ok = results.filter(r => r.ok);
  const fail = results.filter(r => !r.ok);
  console.log("成功: " + ok.length + "/" + results.length);
  if (fail.length) { console.log("失败:"); fail.forEach(f => console.log("  - " + f.report + " " + f.date)); }
  ok.forEach(r => console.log("  " + r.report + " " + r.date + ": " + Math.round(r.size / 1024) + "KB"));
}

main().catch(console.error);
