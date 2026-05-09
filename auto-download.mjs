/**
 * Nova 自动数据下载 v6（修复 Week 选择器）
 * - 正确操作 Ant Design Week Range Picker
 * - 5 个报表一批并行下载
 */
import { chromium } from 'playwright';
import fs from 'fs';
import path from 'path';
import { execSync } from 'child_process';

const REPORTS = [
  { name: '印尼1-Nova',    url: 'https://bi.aliyuncs.com/token3rd/report/view.htm?id=d67f5126-4e68-47a3-bf5a-4b884866cb5a&accessTicket=644babb5-f5d5-45a9-863d-cb7ba7cab030&dd_orientation=auto' },
  { name: '印尼2-Carote',  url: 'https://bi.aliyuncs.com/token3rd/report/view.htm?id=bae08b94-8dad-4691-a7ca-9783de160a39&accessTicket=c5e15c05-565c-4aa5-859c-fe2c93910c44&dd_orientation=auto' },
  { name: '巴西1-Nova',    url: 'https://bi.aliyuncs.com/token3rd/report/view.htm?id=6d33fdf8-9236-455b-be7b-4ff6ea04dabe&accessTicket=5fe5b405-3302-4fe3-a89a-d657861b9459&dd_orientation=auto' },
  { name: '巴西2-Evian',   url: 'https://bi.aliyuncs.com/token3rd/report/view.htm?id=30b58907-ae0c-407d-b3f0-e52d09f71e6b&accessTicket=949597c4-18bd-4bff-b692-6f606a1cd327&dd_orientation=auto' },
  { name: '巴西3-Wisky',   url: 'https://bi.aliyuncs.com/token3rd/report/view.htm?id=75e0152a-25cf-4bf0-80e6-2889bf8e6798&accessTicket=3f7ee12b-098b-49a5-aef9-fd899b845c18&dd_orientation=auto' },
  { name: '巴西4-Doce',    url: 'https://bi.aliyuncs.com/token3rd/report/view.htm?id=2034e69d-3c70-4b43-8a70-47f3cb8a45a5&accessTicket=a9e70ecc-0acf-4562-947c-6cd2d0fe129a&dd_orientation=auto' },
  { name: '土耳其1-Evian', url: 'https://bi.aliyuncs.com/token3rd/report/view.htm?id=b2ba3620-7bfc-4a54-8e4a-9bf2f4577fa7&accessTicket=bd4038a2-3a79-49c7-9880-270b58697c3a&dd_orientation=auto' },
  { name: '西语1-Nova',    url: 'https://bi.aliyuncs.com/token3rd/report/view.htm?id=6e5c9d15-df45-4ee9-b9b7-59d1265f7388&accessTicket=75d522ff-bff5-4522-90b2-ebb789439485&dd_orientation=auto' },
  { name: '西语2-Evian',   url: 'https://bi.aliyuncs.com/token3rd/report/view.htm?id=1fca6b36-5fa6-4906-906d-9495e60f5fe1&accessTicket=248e5488-a1ab-42c5-8104-aee77e6565b9&dd_orientation=auto' },
];

const PARALLEL = 3; // 2 CPU / 3.3GB RAM，3 个浏览器实例是上限

const args = process.argv.slice(2);
const testMode = args.includes('--test');
const dateArg = args.find(a => /^\d{4}-\d{2}-\d{2}$/.test(a));
const targetDate = dateArg || (() => { const d = new Date(); d.setDate(d.getDate() - 1); return d.toISOString().slice(0, 10); })();
const downloadDir = '/home/ubuntu/nova-data/upload-staging/daily/' + targetDate;
fs.mkdirSync(downloadDir, { recursive: true });

const [targetYear, targetMonth, targetDay] = targetDate.split('-').map(Number);
const startTime = Date.now();

// 计算 ISO 周号（标准算法）
function getISOWeek(dateStr) {
  const date = new Date(dateStr + 'T12:00:00');
  const dayNum = date.getDay() || 7; // Mon=1 ... Sun=7
  date.setDate(date.getDate() + 4 - dayNum); // 调到本周四
  const yearStart = new Date(date.getFullYear(), 0, 1);
  return Math.ceil((((date - yearStart) / 86400000) + 1) / 7);
}

const weekNum = getISOWeek(targetDate);
console.log('=========================================');
console.log('  Nova 自动数据下载 v6');
console.log('  日期: ' + targetDate + ' (第' + weekNum + '周)');
console.log('  报表: ' + (testMode ? '仅第1个' : REPORTS.length + '个'));
console.log('  并行: ' + PARALLEL);
console.log('=========================================\n');

/**
 * 设置 Week Range Picker 到包含 targetDate 的那周
 * Ant Design Week Range Picker 操作步骤：
 * 1. 点击第一个 input 打开面板
 * 2. 翻页到目标月份
 * 3. 点击目标日期所在行（选择起始周）
 * 4. 再次点击同一日期（选择结束周）
 */
async function setWeekPicker(page, label) {
  const weekInput = page.locator('.ant-picker-range input').first();
  if (!await weekInput.isVisible({ timeout: 5000 }).catch(() => false)) {
    console.log(label + ': 未找到 Week 选择器');
    return false;
  }

  // 检查当前值是否已经是目标周
  const currentVal = await weekInput.inputValue().catch(() => '');
  const targetWeekStr = targetYear + '-' + weekNum + ' 周';
  if (currentVal === targetWeekStr) {
    console.log(label + ': Week 已是 ' + targetWeekStr);
    return true;
  }

  // 点击打开面板
  await weekInput.click();
  await page.waitForTimeout(1000);

  // 确认面板打开
  const panelVisible = await page.locator('.ant-picker-panel').first()
    .isVisible({ timeout: 3000 }).catch(() => false);
  if (!panelVisible) {
    console.log(label + ': Week 面板未打开');
    return false;
  }

  // 翻页到目标月份（检查左侧面板的月份）
  for (let attempt = 0; attempt < 36; attempt++) {
    // 获取所有面板头部显示的年月
    const headerTexts = await page.locator('.ant-picker-header-view').allTextContents().catch(() => []);
    const leftHeader = headerTexts[0] || '';
    const rightHeader = headerTexts[1] || '';
    const match = leftHeader.match(/(\d{4}).*?(\d{1,2})/);
    if (!match) break;

    const panelYear = parseInt(match[1]);
    const panelMonth = parseInt(match[2]);

    // 右面板通常是左面板的下一个月
    const rightYear = panelMonth === 12 ? panelYear + 1 : panelYear;
    const rightMonth = panelMonth === 12 ? 1 : panelMonth + 1;

    // 如果左面板或右面板包含目标月份就停止
    if ((panelYear === targetYear && panelMonth === targetMonth) ||
        (rightYear === targetYear && rightMonth === targetMonth)) {
      break;
    }

    // 判断方向和距离
    const panelDate = panelYear * 12 + panelMonth;
    const targetDateNum = targetYear * 12 + targetMonth;
    const diff = Math.abs(panelDate - targetDateNum);

    if (panelDate > targetDateNum) {
      // 需要往前翻
      if (diff >= 12) {
        // 差距>=12个月，用<<按钮（左面板第一个按钮）回退一年
        const superPrevBtn = page.locator('.ant-picker-header-super-prev-btn').first();
        await superPrevBtn.click();
      } else {
        // 差距<12个月，用<按钮回退一个月
        const prevBtn = page.locator('.ant-picker-header-prev-btn').first();
        await prevBtn.click();
      }
    } else {
      // 需要往后翻
      if (diff >= 12) {
        // 差距>=12个月，用>>按钮前进一年
        const superNextBtn = page.locator('.ant-picker-header-super-next-btn').last();
        await superNextBtn.click();
      } else {
        // 差距<12个月，用>按钮前进一个月
        const nextBtn = page.locator('.ant-picker-header-next-btn').last();
        await nextBtn.click();
      }
    }
    await page.waitForTimeout(300);
  }

  // 点击目标日期单元格（选择起始周）
  const cellSelector = 'td[title="' + targetDate + '"]';
  let cell = page.locator(cellSelector).first();
  if (!await cell.isVisible({ timeout: 3000 }).catch(() => false)) {
    // 目标日期不可见，可能需要额外翻页或在右面板
    // 先尝试额外翻一个月
    const extraPrevBtn = page.locator('.ant-picker-header-prev-btn').first();
    await extraPrevBtn.click();
    await page.waitForTimeout(500);

    // 再次检查目标日期
    cell = page.locator(cellSelector).first();
    if (!await cell.isVisible({ timeout: 2000 }).catch(() => false)) {
      // 还是不行，尝试找同周的其他日期
      const d = new Date(targetDate + 'T00:00:00');
      const dayOfWeek = d.getDay() || 7; // 1=Mon ... 7=Sun
      const monday = new Date(d);
      monday.setDate(d.getDate() - dayOfWeek + 1);
      let found = false;
      for (let offset = 0; offset < 7; offset++) {
        const tryDate = new Date(monday);
        tryDate.setDate(monday.getDate() + offset);
        const tryStr = tryDate.toISOString().slice(0, 10);
        const tryCell = page.locator('td[title="' + tryStr + '"]').first();
        if (await tryCell.isVisible({ timeout: 1000 }).catch(() => false)) {
          cell = tryCell;
          found = true;
          break;
        }
      }
      if (!found) {
        // 最后尝试：翻回来再往后翻一个月
        const nextBtn = page.locator('.ant-picker-header-next-btn').last();
        await nextBtn.click();
        await nextBtn.click();
        await page.waitForTimeout(500);
        cell = page.locator(cellSelector).first();
        if (!await cell.isVisible({ timeout: 2000 }).catch(() => false)) {
          console.log(label + ': 目标日期不可见，所有尝试均失败');
          await page.keyboard.press('Escape');
          return false;
        }
      }
    }
  }

  // 第一次点击：选择起始周
  await cell.click();
  await page.waitForTimeout(800);

  // 第二次点击：选择结束周（同一周）
  // 面板可能重新渲染，重新定位
  const cell2 = page.locator(cellSelector).first();
  if (await cell2.isVisible({ timeout: 3000 }).catch(() => false)) {
    await cell2.click();
  } else {
    // 尝试找同周其他日期
    const d = new Date(targetDate + 'T00:00:00');
    const dayOfWeek = d.getDay() || 7;
    const monday = new Date(d);
    monday.setDate(d.getDate() - dayOfWeek + 1);
    for (let offset = 0; offset < 7; offset++) {
      const tryDate = new Date(monday);
      tryDate.setDate(monday.getDate() + offset);
      const tryStr = tryDate.toISOString().slice(0, 10);
      const tryCell = page.locator('td[title="' + tryStr + '"]').first();
      if (await tryCell.isVisible({ timeout: 1000 }).catch(() => false)) {
        await tryCell.click();
        break;
      }
    }
  }
  await page.waitForTimeout(500);

  // 验证
  const newVal = await weekInput.inputValue().catch(() => '');
  console.log(label + ': Week ' + currentVal + ' → ' + newVal);
  return true;
}

async function downloadOne(browser, report) {
  const label = report.name;
  const context = await browser.newContext({
    acceptDownloads: true,
    viewport: { width: 1920, height: 1080 },
  });
  const page = await context.newPage();
  page.setDefaultTimeout(120000);

  try {
    await page.goto(report.url, { waitUntil: 'domcontentloaded', timeout: 120000 });
    await page.waitForLoadState("networkidle", { timeout: 30000 }).catch(() => {});

    // 切换 tab
    const dataTab = page.locator("text=每日主播数据").or(page.locator("text=日主播工作数据")).or(page.locator("text=主播数据")).first();
    if (await dataTab.isVisible({ timeout: 5000 }).catch(() => false)) {
      await dataTab.click();
      await page.waitForLoadState("networkidle", { timeout: 30000 }).catch(() => {});
    }

    // 设置 Week 选择器
    await setWeekPicker(page, label);
    await page.waitForTimeout(1000);

    // 查询
    const queryBtn = page.locator('button:has-text("查 询"), button:has-text("查询"), span:has-text("查 询")').first();
    if (await queryBtn.isVisible({ timeout: 3000 }).catch(() => false)) {
      await queryBtn.click();
      console.log(label + ': 查询已点击，等待数据加载...');
      await page.waitForLoadState("networkidle", { timeout: 30000 }).catch(() => {});
    } else {
      console.log(label + ': ⚠️ 查询按钮不可见，跳过查询');
    }

    // 导出
    await page.locator('text=导出').first().click();
    await page.waitForTimeout(2000);

    // 选当前sheet页（确保导出的是当前激活的tab）
    const cs = page.locator('label.ant-radio-wrapper:has-text("当前sheet页")').first();
    if (await cs.isVisible({ timeout: 3000 }).catch(() => false)) await cs.click();
    await page.waitForTimeout(500);

    // 确定 + 等下载
    const downloadPromise = page.waitForEvent('download', { timeout: 120000 }).catch(() => null);
    const primaryBtns = await page.locator('button.ant-btn-primary').all();
    for (let bi = primaryBtns.length - 1; bi >= 0; bi--) {
      const text = await primaryBtns[bi].textContent();
      if (text.includes('确')) { await primaryBtns[bi].click(); break; }
    }

    const download = await downloadPromise;
    const fileName = report.name + '_' + targetDate + '.xlsx';
    const filePath = path.join(downloadDir, fileName);
    if (download) {
      await download.saveAs(filePath);
      const size = fs.statSync(filePath).size;
      // 验证文件大小 - 正常数据文件应该大于100KB
      if (size < 50 * 1024) {
        console.log(label + ': ⚠️ 文件过小 (' + Math.round(size / 1024) + 'KB)，可能数据未加载');
      } else {
        console.log(label + ': ✅ ' + Math.round(size / 1024) + 'KB');
      }
      // ── 语音房tab额外下载 ──
      if (report.voiceRoomTab) {
        try {
          console.log(label + ': 开始下载语音房数据...');
          const voiceTab = page.locator('text=' + report.voiceRoomTab).first();
          if (await voiceTab.isVisible({ timeout: 5000 }).catch(() => false)) {
            await voiceTab.click();
            await page.waitForTimeout(15000);

            // 查询
            const vQueryBtn = page.locator('button:has-text("查 询"), button:has-text("查询")').first();
            if (await vQueryBtn.isVisible({ timeout: 3000 }).catch(() => false)) {
              await vQueryBtn.click();
              await page.waitForTimeout(20000);
            }

            // 导出
            await page.locator('text=导出').first().click();
            await page.waitForTimeout(2000);
            const vCs = page.locator('label.ant-radio-wrapper:has-text("当前sheet页")').first();
            if (await vCs.isVisible({ timeout: 3000 }).catch(() => false)) await vCs.click();
            await page.waitForTimeout(500);

            const vDownloadPromise = page.waitForEvent('download', { timeout: 120000 }).catch(() => null);
            const vBtns = await page.locator('button.ant-btn-primary').all();
            for (let bi = vBtns.length - 1; bi >= 0; bi--) {
              const text = await vBtns[bi].textContent();
              if (text.includes('确')) { await vBtns[bi].click(); break; }
            }
            const vDownload = await vDownloadPromise;
            if (vDownload) {
              const vFileName = report.name + '_voice_' + targetDate + '.xlsx';
              const vFilePath = path.join(downloadDir, vFileName);
              await vDownload.saveAs(vFilePath);
              const vSize = fs.statSync(vFilePath).size;
              console.log(label + ': ✅ 语音房 ' + Math.round(vSize / 1024) + 'KB');
            } else {
              console.log(label + ': ⚠️ 语音房无下载');
            }
          } else {
            console.log(label + ': ⚠️ 语音房tab不可见，跳过');
          }
        } catch (vErr) {
          console.error(label + ': ❌ 语音房下载失败: ' + vErr.message.split('\n')[0]);
        }
      }

      await context.close();
      return filePath;
    }
    console.log(label + ': ⚠️ 无下载');
    await context.close();
    return null;
  } catch (error) {
    console.error(label + ': ❌ ' + error.message.split('\n')[0]);
    await page.screenshot({ path: path.join(downloadDir, report.name + '_error.png') }).catch(() => {});
    await context.close();
    return null;
  }
}

async function main() {
  const browser = await chromium.launch({ headless: true });
  const reports = testMode ? [REPORTS[0]] : REPORTS;
  const results = [];

  for (let i = 0; i < reports.length; i += PARALLEL) {
    const batch = reports.slice(i, i + PARALLEL);
    console.log('--- 批次 ' + (Math.floor(i / PARALLEL) + 1) + ': ' + batch.map(r => r.name).join(', ') + ' ---');
    const batchResults = await Promise.all(batch.map(r => downloadOne(browser, r)));
    results.push(...batchResults);
  }


  // ── 失败报表重试（最多2次）──
  const failedReports = reports.filter((r, i) => !results[i]);
  if (failedReports.length > 0) {
    console.log('\n--- 重试失败报表: ' + failedReports.map(r => r.name).join(', ') + ' ---');
    for (let retry = 1; retry <= 2; retry++) {
      const stillFailed = [];
      for (const report of failedReports) {
        const idx = reports.indexOf(report);
        if (results[idx]) continue; // 已在之前重试中成功
        console.log('重试 #' + retry + ': ' + report.name);
        const result = await downloadOne(browser, report);
        if (result) {
          results[idx] = result;
          console.log(report.name + ': 重试成功 ✅');
        } else {
          stillFailed.push(report);
        }
      }
      if (stillFailed.length === 0) break;
    }
  }

  await browser.close();

  const success = results.filter(Boolean);
  const elapsed = Math.round((Date.now() - startTime) / 1000);
  console.log('\n下载完成: ' + success.length + '/' + reports.length + ' (' + elapsed + '秒)');

  // 导入由 daily-sync.sh 统一调度，auto-download 只负责下载
}

main().catch(console.error);
