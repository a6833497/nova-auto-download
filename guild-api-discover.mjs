import { chromium } from 'playwright';
import fs from 'fs';

async function main() {
  const browser = await chromium.launch({
    headless: false,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-blink-features=AutomationControlled'],
  });

  const context = await browser.newContext({
    viewport: { width: 1280, height: 800 },
    userAgent: 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    locale: 'en-US',
  });

  const page = await context.newPage();
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
  });

  const apiCalls = [];
  page.on('response', async (resp) => {
    const req = resp.request();
    if (req.resourceType() === 'xhr' || req.resourceType() === 'fetch') {
      let body = '';
      try { body = await resp.text(); } catch {}
      apiCalls.push({
        url: req.url(),
        method: req.method(),
        status: resp.status(),
        requestHeaders: {
          authorization: req.headers()['authorization'] || '',
          token: req.headers()['token'] || '',
        },
        postData: req.postData()?.substring(0, 500) || null,
        responseSize: body.length,
        responsePreview: body.substring(0, 1500),
      });
    }
  });

  // 登录流程
  console.log('1. 打开登录页...');
  await page.goto('https://guild.linke.ai/guild/login', { waitUntil: 'networkidle', timeout: 30000 });
  await page.waitForTimeout(2000);

  console.log('2. 点击Google登录...');
  const popupPromise = context.waitForEvent('page', { timeout: 15000 }).catch(() => null);
  await page.locator('div:has-text("Log in with Google")').first().click();
  await page.waitForTimeout(3000);
  let googlePage = await popupPromise;
  if (!googlePage) googlePage = page;
  await googlePage.waitForLoadState('domcontentloaded', { timeout: 10000 }).catch(() => {});

  console.log('3. 输入邮箱...');
  const emailInput = googlePage.locator('input[type="email"]').first();
  await emailInput.waitFor({ timeout: 10000 });
  await emailInput.type('yuyingwang207@gmail.com', { delay: 60 });
  await googlePage.locator('#identifierNext').click();
  await googlePage.waitForTimeout(5000);

  console.log('4. 输入密码...');
  const pwdInput = googlePage.locator('input[type="password"]').first();
  await pwdInput.waitFor({ timeout: 10000 });
  await pwdInput.type('Qaz298117.', { delay: 60 });
  await googlePage.locator('#passwordNext').click();
  await googlePage.waitForTimeout(5000);

  // 截图2FA页面
  await googlePage.screenshot({ path: '/tmp/guild-v7-2fa.png', fullPage: true });

  // 获取2FA数字和提示
  const pageText = await googlePage.evaluate(() => document.body.innerText);
  console.log('\n2FA页面内容:');
  console.log(pageText.substring(0, 500));

  // 尝试找 "Try another way" 并滚动到底部
  await googlePage.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
  await googlePage.waitForTimeout(1000);
  await googlePage.screenshot({ path: '/tmp/guild-v7-2fa-scrolled.png', fullPage: true });

  // 检查是否有其他验证方式的链接
  const links = await googlePage.evaluate(() => {
    return Array.from(document.querySelectorAll('a, button')).map(el => ({
      text: el.innerText?.trim()?.substring(0, 80),
      href: el.href || '',
    })).filter(x => x.text);
  });
  console.log('\n2FA页面上的链接/按钮:');
  for (const l of links) {
    console.log(`  "${l.text}" -> ${l.href}`);
  }

  // 等待180秒让用户手动确认2FA
  console.log('\n\n========================================');
  console.log('*** 请立即在手机Gmail上确认2FA验证! ***');
  console.log('*** 等待180秒(3分钟)... ***');
  console.log('========================================\n');

  let loginSuccess = false;
  for (let i = 0; i < 36; i++) {
    await page.waitForTimeout(5000);
    const currentUrl = page.url();

    if (!currentUrl.includes('login')) {
      console.log(`\n*** 登录成功! (${(i+1)*5}秒后) ***`);
      console.log('URL:', currentUrl);
      loginSuccess = true;
      break;
    }

    // 每15秒输出一次状态
    if ((i + 1) % 3 === 0) {
      console.log(`等待中... ${(i+1)*5}秒`);
    }

    // 检查Google弹窗是否关闭
    try {
      if (googlePage !== page && googlePage.isClosed()) {
        console.log('Google弹窗已关闭');
        await page.waitForTimeout(5000);
        if (!page.url().includes('login')) {
          console.log('*** 登录成功! ***');
          loginSuccess = true;
          break;
        }
      }
    } catch {}
  }

  console.log('\n最终URL:', page.url());
  await page.screenshot({ path: '/tmp/guild-v7-final.png' });

  if (!loginSuccess) {
    console.log('\n2FA确认超时，登录失败');
    // 保存中间结果
    fs.writeFileSync('/tmp/guild-api-discovery.json', JSON.stringify({ error: '2FA_TIMEOUT', apiCalls }, null, 2));
    await browser.close();
    return;
  }

  // === 登录成功，开始探索 ===
  console.log('\n\n========================================');
  console.log('=== 开始探索公会后台 ===');
  console.log('========================================');

  // 认证信息
  const cookies = await context.cookies();
  const ls = await page.evaluate(() => JSON.stringify(localStorage));

  console.log('\n=== 认证信息 ===');
  for (const c of cookies) {
    console.log(`Cookie: ${c.name} = ${c.value.substring(0, 100)}... (${c.domain})`);
  }
  console.log('LocalStorage:', ls.substring(0, 2000));

  // 保存session
  fs.writeFileSync('/tmp/guild-session-state.json', JSON.stringify({ cookies, localStorage: JSON.parse(ls || '{}') }, null, 2));

  // 等待首页加载
  await page.waitForTimeout(8000);
  await page.screenshot({ path: '/tmp/guild-v7-home.png' });

  // 获取页面结构
  const pageStructure = await page.evaluate(() => {
    const body = document.body.innerHTML;
    const links = Array.from(document.querySelectorAll('a')).map(a => ({
      text: a.innerText?.trim()?.substring(0, 80), href: a.href
    })).filter(x => x.href && x.href.includes('guild.linke.ai'));
    const tabs = Array.from(document.querySelectorAll('[role="tab"], .tab, [class*="tab"]')).map(t => ({
      text: t.innerText?.trim()?.substring(0, 50)
    }));
    return { links, tabs, bodyLength: body.length };
  });

  console.log('\n=== 页面结构 ===');
  console.log('链接:');
  for (const l of pageStructure.links) {
    console.log(`  "${l.text}" -> ${l.href}`);
  }
  console.log('Tabs:', pageStructure.tabs.map(t => t.text));

  // 遍历所有链接
  const visited = new Set();
  for (const link of pageStructure.links) {
    if (visited.has(link.href) || link.href.includes('login')) continue;
    visited.add(link.href);
    try {
      console.log(`\n--- 访问: "${link.text}" ${link.href} ---`);
      await page.goto(link.href, { waitUntil: 'networkidle', timeout: 20000 });
      await page.waitForTimeout(4000);
      const safeName = (link.text || 'unnamed').replace(/[^a-zA-Z0-9]/g, '_').substring(0, 30);
      await page.screenshot({ path: `/tmp/guild-v7-page-${safeName}.png` });
      console.log('OK - URL:', page.url());

      // 检查页面内tab
      const innerTabs = await page.evaluate(() => {
        return Array.from(document.querySelectorAll('[role="tab"], .ant-tabs-tab, [class*="tab-item"], [class*="TabItem"]')).map(t => ({
          text: t.innerText?.trim()?.substring(0, 50)
        }));
      });
      if (innerTabs.length > 0) {
        console.log('  内部Tab:', innerTabs.map(t => t.text));
        // 依次点击tab
        for (const tab of innerTabs) {
          if (!tab.text) continue;
          try {
            const tabEl = page.locator(`[role="tab"]:has-text("${tab.text}"), [class*="tab"]:has-text("${tab.text}")`).first();
            if (await tabEl.isVisible({ timeout: 1000 }).catch(() => false)) {
              console.log(`  点击tab: "${tab.text}"`);
              await tabEl.click();
              await page.waitForTimeout(3000);
            }
          } catch {}
        }
      }
    } catch (e) {
      console.log(`  失败: ${e.message.substring(0, 100)}`);
    }
  }

  // 猜测路径
  const guessedPaths = [
    '/guild/home', '/guild/data', '/guild/overview', '/guild/anchor',
    '/guild/income', '/guild/settlement', '/guild/report', '/guild/statistics',
    '/guild/member', '/guild/streamer', '/guild/voice', '/guild/chat',
    '/guild/manage', '/guild/withdrawal', '/guild/1v1',
    '/guild/host/list', '/guild/anchor/detail', '/guild/revenue',
  ];
  for (const path of guessedPaths) {
    const url = 'https://guild.linke.ai' + path;
    if (visited.has(url)) continue;
    visited.add(url);
    try {
      await page.goto(url, { waitUntil: 'networkidle', timeout: 15000 });
      await page.waitForTimeout(3000);
      const actual = page.url();
      if (!actual.includes('login') && actual !== 'https://guild.linke.ai/guild/login') {
        console.log(`\n猜测路径成功: ${path} -> ${actual}`);
        await page.screenshot({ path: `/tmp/guild-v7-guess${path.replace(/\//g, '-')}.png` });
      }
    } catch {}
  }

  // 输出所有API
  console.log('\n\n========================================');
  console.log('=== 全部捕获的API请求 (' + apiCalls.length + '个) ===');
  console.log('========================================\n');

  const uniqueUrls = new Map();
  for (const call of apiCalls) {
    const key = call.method + ' ' + call.url.split('?')[0];
    if (!uniqueUrls.has(key)) {
      uniqueUrls.set(key, call);
    }
  }

  // 只显示 api.linke.ai 的请求
  const apiLinke = Array.from(uniqueUrls.values()).filter(c => c.url.includes('api.linke.ai'));
  console.log(`\n=== api.linke.ai 接口 (${apiLinke.length}个) ===`);
  for (const call of apiLinke) {
    console.log('---');
    console.log(`[${call.status}] ${call.method} ${call.url}`);
    if (call.requestHeaders.authorization) console.log('  Auth:', call.requestHeaders.authorization.substring(0, 100));
    if (call.requestHeaders.token) console.log('  Token:', call.requestHeaders.token.substring(0, 100));
    if (call.postData) console.log('  PostData:', call.postData);
    console.log('  Response:', call.responsePreview.substring(0, 500));
  }

  // 其他API
  const otherApis = Array.from(uniqueUrls.values()).filter(c => !c.url.includes('api.linke.ai'));
  console.log(`\n=== 其他接口 (${otherApis.length}个) ===`);
  for (const call of otherApis) {
    console.log(`  [${call.status}] ${call.method} ${call.url.substring(0, 200)}`);
  }

  // 保存
  fs.writeFileSync('/tmp/guild-api-discovery.json', JSON.stringify({
    totalCalls: apiCalls.length,
    uniqueApiLinke: apiLinke.map(c => ({ method: c.method, url: c.url, status: c.status, postData: c.postData, responsePreview: c.responsePreview })),
    allCalls: apiCalls,
  }, null, 2));
  console.log('\n完整结果保存到 /tmp/guild-api-discovery.json');

  await browser.close();
}

main().catch(e => console.error('FATAL:', e.message));
