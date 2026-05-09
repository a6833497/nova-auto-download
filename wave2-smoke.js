const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  const errs = [];
  page.on('pageerror', e => errs.push(e.message.substring(0, 200)));
  page.on('console', m => { if (m.type() === 'error') errs.push('CONSOLE:' + m.text().substring(0, 200)); });

  await page.goto('https://nova.hoyisr.com/login', { waitUntil: 'networkidle', timeout: 30000 });
  await page.fill('input[type=text]', 'admin');
  await page.fill('input[type=password]', 'admin123');
  await page.click('button[type=submit]');
  await page.waitForTimeout(3000);

  // 试几个可能的路径
  const candidates = ['/admin/strategy-overview', '/admin', '/strategy', '/admin/strategy'];
  let landed = '';
  for (const p of candidates) {
    await page.goto('https://nova.hoyisr.com' + p, { waitUntil: 'networkidle', timeout: 20000 });
    await page.waitForTimeout(2000);
    const has = await page.locator('text=/策略总览/').count();
    if (has > 0) { landed = p; break; }
  }
  console.log('LANDED_AT:', landed);
  await page.waitForTimeout(3000);

  const hasScatter = await page.evaluate(() => !!document.querySelector('.recharts-scatter'));
  console.log('SCATTER_RENDERED:', hasScatter);

  const hasToggle = await page.locator('button:has-text("7天")').count();
  console.log('QUICK_BTN_7D:', hasToggle);

  const hasCustomBtn = await page.locator('button:has-text("自定义")').count();
  console.log('CUSTOM_DATE_BTN:', hasCustomBtn);

  const yoyText = await page.locator('text=/vs.+上周/').count().catch(() => 0);
  console.log('YOY_LABEL:', yoyText);

  const guildRows = await page.locator('text=/印尼1|印尼2|巴西1/').count();
  console.log('GUILD_ROWS:', guildRows);

  // 点第一个公会
  const targetRow = page.locator('div.cursor-pointer').filter({ hasText: /印尼|巴西|西语/ }).first();
  const cnt = await targetRow.count();
  console.log('CLICKABLE_ROW_COUNT:', cnt);
  if (cnt > 0) {
    await targetRow.click();
    await page.waitForTimeout(2500);
    const drawer = await page.locator('text=/14 日趋势/').count();
    console.log('DRAWER_OPEN:', drawer);
    const lineChart = await page.evaluate(() => document.querySelectorAll('.recharts-line').length);
    console.log('LINE_COUNT:', lineChart);
    const topHosts = await page.locator('text=/Top 10 主播/').count();
    console.log('TOPHOSTS_SECTION:', topHosts);
  }

  console.log('ERRORS_TOTAL:', errs.length);
  if (errs.length) console.log('ERR_SAMPLE:', errs.slice(0, 3).join(' || '));
  await page.screenshot({ path: '/tmp/wave2-overview.png', fullPage: true });
  console.log('SCREENSHOT_AT:', '/tmp/wave2-overview.png');
  await browser.close();
})().catch(e => { console.log('FATAL:' + e.message); process.exit(1); });
