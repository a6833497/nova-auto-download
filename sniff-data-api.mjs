import { chromium } from 'playwright';

const URL = "https://bi.aliyuncs.com/token3rd/report/view.htm?id=bae08b94-8dad-4691-a7ca-9783de160a39&accessTicket=c5e15c05-565c-4aa5-859c-fe2c93910c44&dd_orientation=auto";

async function main() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  const page = await context.newPage();

  const dataRequests = [];
  
  page.on('response', async (resp) => {
    const url = resp.url();
    if (url.includes('.js') || url.includes('.css') || url.includes('.png') || url.includes('.woff')) return;
    
    const contentType = resp.headers()['content-type'] || '';
    if (!contentType.includes('json')) return;
    
    try {
      const body = await resp.text();
      if (body.length > 1000 && (body.includes('sid') || body.includes('diamond') || body.includes('streamer') || body.includes('total_diamond'))) {
        dataRequests.push({
          url: url.substring(0, 200),
          method: resp.request().method(),
          status: resp.status(),
          contentType,
          bodySize: body.length,
          bodyPreview: body.substring(0, 500),
          requestHeaders: JSON.stringify(Object.fromEntries(
            Object.entries(resp.request().headers()).filter(([k]) => 
              ['cookie','authorization','x-csrf-token','x-token','content-type'].includes(k.toLowerCase())
            )
          )),
          postData: resp.request().postData()?.substring(0, 500) || null,
        });
      }
    } catch {}
  });

  // 也捕获所有JSON请求用于了解API结构
  const allJsonRequests = [];
  page.on('response', async (resp) => {
    const url = resp.url();
    if (url.includes('.js') || url.includes('.css') || url.includes('.png') || url.includes('.woff')) return;
    const contentType = resp.headers()['content-type'] || '';
    if (!contentType.includes('json')) return;
    try {
      const body = await resp.text();
      allJsonRequests.push({
        url: url.substring(0, 300),
        method: resp.request().method(),
        status: resp.status(),
        bodySize: body.length,
        postData: resp.request().postData()?.substring(0, 300) || null,
      });
    } catch {}
  });

  console.log('Loading page...');
  await page.goto(URL, { waitUntil: 'networkidle', timeout: 90000 });
  await page.waitForTimeout(15000);
  
  console.log('\n=== 找到 ' + dataRequests.length + ' 个包含主播数据的请求 ===\n');
  for (const req of dataRequests) {
    console.log('--- DATA REQUEST ---');
    console.log('URL:', req.url);
    console.log('Method:', req.method);
    console.log('Size:', req.bodySize, 'bytes');
    console.log('Auth Headers:', req.requestHeaders);
    if (req.postData) console.log('POST Body:', req.postData);
    console.log('Preview:', req.bodyPreview);
    console.log('');
  }

  console.log('\n=== 所有JSON请求 (' + allJsonRequests.length + '个) ===\n');
  for (const req of allJsonRequests) {
    console.log(req.method, req.url.substring(0, 150), '|', req.bodySize, 'bytes');
    if (req.postData) console.log('  POST:', req.postData.substring(0, 200));
  }
  
  await browser.close();
}
main().catch(e => console.error(e.message));
