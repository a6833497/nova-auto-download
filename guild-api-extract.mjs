import https from 'https';
import http from 'http';
import fs from 'fs';

function fetchUrl(url) {
  const mod = url.startsWith('https') ? https : http;
  return new Promise((resolve, reject) => {
    mod.get(url, { headers: { 'User-Agent': 'Mozilla/5.0' } }, (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => resolve(data));
    }).on('error', reject);
  });
}

async function main() {
  // 直接获取页面HTML找JS文件
  console.log('1. 获取HTML页面...');
  const html = await fetchUrl('https://guild.linke.ai/guild/login');

  // 找script标签
  const scriptMatches = html.match(/src="([^"]+\.js[^"]*)"/g) || [];
  const jsUrls = scriptMatches.map(m => {
    let url = m.match(/src="([^"]+)"/)[1];
    if (url.startsWith('/')) url = 'https://guild.linke.ai' + url;
    return url;
  });

  // 加上已知的non-legacy版本
  const allJsUrls = [...new Set(jsUrls)];
  console.log(`找到 ${allJsUrls.length} 个JS文件`);

  // 下载并分析每个JS
  const allContent = [];
  for (const url of allJsUrls) {
    try {
      console.log(`\n下载: ${url.substring(url.lastIndexOf('/') + 1)}`);
      const content = await fetchUrl(url);
      console.log(`  大小: ${(content.length / 1024).toFixed(0)}KB`);
      allContent.push({ url, content });

      // 保存到本地
      const filename = url.substring(url.lastIndexOf('/') + 1).split('?')[0];
      fs.writeFileSync(`/tmp/guild-js-${filename}`, content);
    } catch (e) {
      console.log(`  失败: ${e.message.substring(0, 100)}`);
    }
  }

  console.log('\n\n2. 搜索API相关内容...');

  const allApis = new Set();
  const allRoutes = new Set();
  const allBaseUrls = new Set();
  const allAuthInfo = [];

  for (const { url, content } of allContent) {
    const filename = url.substring(url.lastIndexOf('/') + 1);
    console.log(`\n--- ${filename} ---`);

    // 搜索各种API路径模式

    // 1. 直接的 /api/ 路径 (最宽泛匹配)
    const apiPaths = content.match(/[/"']\/api\/[a-zA-Z0-9_/\-.]+/g) || [];
    for (const m of apiPaths) {
      const path = m.replace(/^[/"']/, '');
      allApis.add(path);
    }

    // 2. URL中包含 api.linke.ai
    const fullUrls = content.match(/https?:\/\/api\.linke\.ai[a-zA-Z0-9_/\-?.=&]+/g) || [];
    for (const m of fullUrls) {
      allApis.add(m);
    }

    // 3. 字符串拼接的API路径 (如 "/guild/" + "xxx")
    const guildPaths = content.match(/[/"'](\/guild\/[a-zA-Z0-9_/\-.]+)/g) || [];
    for (const m of guildPaths) {
      const path = m.replace(/^[/"']/, '');
      if (!path.includes('assets/') && !path.includes('.js') && !path.includes('.css') && !path.includes('.png')) {
        allRoutes.add(path);
      }
    }

    // 4. 搜索 baseURL 或 base_url
    const baseUrls = content.match(/base[_]?[Uu]rl['":\s=]+["']([^"']+)["']/g) || [];
    for (const m of baseUrls) {
      allBaseUrls.add(m);
    }

    // 5. 搜索 endpoint/url 配置
    const endpoints = content.match(/(?:endpoint|apiUrl|API_URL|api_url|request_url)['":\s=]+["']([^"']+)["']/gi) || [];
    for (const m of endpoints) {
      allBaseUrls.add(m);
    }

    // 6. 搜索 axios/fetch 调用中的URL
    const axiosGets = content.match(/\.(get|post|put|delete|patch)\s*\(\s*["'`]([^"'`]+)["'`]/g) || [];
    for (const m of axiosGets) {
      const urlMatch = m.match(/["'`]([^"'`]+)["'`]/);
      if (urlMatch) {
        const apiPath = urlMatch[1];
        if (apiPath.startsWith('/') || apiPath.startsWith('http')) {
          allApis.add(apiPath);
        }
      }
    }

    // 7. 搜索 request({url: xxx}) 模式
    const reqObjs = content.match(/url\s*:\s*["'`]([^"'`]+)["'`]/g) || [];
    for (const m of reqObjs) {
      const urlMatch = m.match(/["'`]([^"'`]+)["'`]/);
      if (urlMatch) {
        const path = urlMatch[1];
        if ((path.startsWith('/') && path.includes('/')) || path.startsWith('http')) {
          allApis.add(path);
        }
      }
    }

    // 8. 搜索 method 配置
    const methods = content.match(/method\s*:\s*["'`](GET|POST|PUT|DELETE|PATCH)["'`]/gi) || [];
    if (methods.length > 0) {
      console.log(`  HTTP Methods found: ${methods.length}`);
    }

    // 9. 搜索认证header设置
    const authHeaders = content.match(/[Aa]uthorization['":\s]+[^,;\n\r]{5,80}/g) || [];
    for (const m of authHeaders) {
      if (!allAuthInfo.includes(m)) allAuthInfo.push(m);
    }

    // 10. 搜索 token header
    const tokenHeaders = content.match(/headers\s*[.=:]\s*\{[^}]*token[^}]*\}/g) || [];
    for (const m of tokenHeaders) {
      console.log(`  Token Header: ${m.substring(0, 150)}`);
    }

    // 11. 搜索 interceptor 设置
    const interceptors = content.match(/interceptors?\.[a-z]+\.use\s*\(/g) || [];
    if (interceptors.length > 0) {
      console.log(`  Interceptors: ${interceptors.length}`);
      // 找出前后200字符的context
      for (const m of interceptors) {
        const idx = content.indexOf(m);
        console.log(`    Context: ...${content.substring(Math.max(0, idx - 50), idx + 200).replace(/\n/g, ' ')}...`);
      }
    }

    // 12. streamer/guild 相关字符串
    const streamerPaths = content.match(/["'`](\/streamer\/[a-zA-Z0-9_/\-.]+)["'`]/g) || [];
    for (const m of streamerPaths) {
      allApis.add(m.slice(1, -1));
    }

    const guildApiPaths = content.match(/["'`](\/guild\/[a-zA-Z0-9_/\-.]+)["'`]/g) || [];
    for (const m of guildApiPaths) {
      const p = m.slice(1, -1);
      if (!p.includes('assets/') && !p.includes('.js') && !p.includes('.css') && !p.endsWith('.png') && !p.endsWith('.svg')) {
        allApis.add(p);
      }
    }

    // 13. 搜索 settlement/withdrawal 等业务关键词附近的URL
    const keywords = ['settlement', 'withdrawal', 'income', 'revenue', 'streamer', 'anchor', 'host', 'overview', 'dashboard', 'report', 'statistics', 'member', 'chat', 'voice', '1v1'];
    for (const kw of keywords) {
      const kwIdx = content.indexOf(kw);
      if (kwIdx > -1) {
        // 找这个关键词前后的URL-like字符串
        const context = content.substring(Math.max(0, kwIdx - 200), kwIdx + 200);
        const urlsInContext = context.match(/["'`](\/[a-zA-Z0-9_/\-.]+)["'`]/g) || [];
        for (const u of urlsInContext) {
          const p = u.slice(1, -1);
          if (p.includes('/') && p.length > 3 && !p.includes('.js') && !p.includes('.css') && !p.includes('assets')) {
            allApis.add(p);
          }
        }
      }
    }
  }

  // 输出结果
  console.log('\n\n========================================');
  console.log('=== 发现的API路径 ===');
  console.log('========================================');

  const sortedApis = Array.from(allApis).sort();
  for (const api of sortedApis) {
    console.log(`  ${api}`);
  }

  console.log('\n=== 发现的前端路由 ===');
  const sortedRoutes = Array.from(allRoutes).sort();
  for (const route of sortedRoutes) {
    console.log(`  ${route}`);
  }

  console.log('\n=== BaseURL配置 ===');
  for (const base of allBaseUrls) {
    console.log(`  ${base}`);
  }

  console.log('\n=== 认证相关 ===');
  for (const auth of allAuthInfo.slice(0, 20)) {
    console.log(`  ${auth.substring(0, 150)}`);
  }

  // 保存
  fs.writeFileSync('/tmp/guild-api-from-js.json', JSON.stringify({
    apis: sortedApis,
    routes: sortedRoutes,
    baseUrls: Array.from(allBaseUrls),
    auth: allAuthInfo.slice(0, 30),
  }, null, 2));
  console.log('\n保存到 /tmp/guild-api-from-js.json');
}

main().catch(e => console.error('FATAL:', e.message));
