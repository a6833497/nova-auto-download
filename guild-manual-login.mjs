import { chromium } from "playwright";
import fs from "fs";

async function main() {
  // 有头模式，弹出浏览器让用户手动登录
  const browser = await chromium.launch({ 
    headless: false,
    args: ["--no-sandbox"]
  });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();

  console.log("打开公会后台登录页...");
  await page.goto("https://guild.linke.ai/guild/login", { waitUntil: "networkidle", timeout: 30000 });

  console.log("");
  console.log("========================================");
  console.log("  请在弹出的浏览器中手动登录");
  console.log("  登录完成后，按回车继续...");
  console.log("========================================");
  console.log("");

  // 等待用户登录
  // 每5秒检查一次是否已登录（URL变化或cookie出现）
  let loggedIn = false;
  for (let i = 0; i < 120; i++) { // 最多等10分钟
    await new Promise(r => setTimeout(r, 5000));
    
    const cookies = await context.cookies();
    const oauthToken = cookies.find(c => c.name === "oauth_token");
    const oauthSecret = cookies.find(c => c.name === "oauth_token_secret");
    const currentUrl = page.url();
    
    if (oauthToken && oauthSecret) {
      console.log("✅ 检测到登录成功！");
      console.log("  oauth_token:", oauthToken.value.substring(0, 20) + "...");
      console.log("  oauth_token_secret:", oauthSecret.value.substring(0, 20) + "...");
      
      // 保存所有cookie
      const allCookies = await context.cookies();
      const session = {
        oauth_token: oauthToken.value,
        oauth_token_secret: oauthSecret.value,
        all_cookies: allCookies,
        saved_at: new Date().toISOString(),
      };
      fs.writeFileSync("/home/ubuntu/nova-auto-download/guild-session.json", JSON.stringify(session, null, 2));
      console.log("  Session已保存到 guild-session.json");
      loggedIn = true;
      break;
    }
    
    if (i % 6 === 0) {
      console.log("  等待登录中... (" + Math.round(i*5/60) + "分钟) URL:", currentUrl.substring(0, 60));
    }
  }

  if (!loggedIn) {
    console.log("❌ 等待超时，未检测到登录");
  }

  await browser.close();
}

main().catch(e => console.error(e.message));
