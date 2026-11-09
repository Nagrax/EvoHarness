/* headless OAuth 续航 v2：注入 IAB 解密的 passport 会话 cookie 后访问授权 URL。
 * 用法：node renew_navigate.mjs <authorizeUrl> <cookiesJsonPath>
 * 输出：CALLBACK_URL=<url>（成功）或 AUTH_STUCK=<url>
 */
import { readFileSync } from "node:fs";
import puppeteer from "file:///D:/claudecode/scratch/btest/node_modules/puppeteer-core/lib/puppeteer/puppeteer-core.js";

const authUrl = process.argv[2];
const cookiesPath = process.argv[3];
if (!authUrl) { console.error("missing authorize url"); process.exit(2); }

const browser = await puppeteer.launch({
  executablePath: "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  headless: "new",
  args: ["--no-first-run", "--no-default-browser-check", "--disable-features=Translate"],
});
try {
  const page = await browser.newPage();
  if (cookiesPath) {
    const cookies = JSON.parse(readFileSync(cookiesPath, "utf-8"));
    const usable = cookies
      .filter((c) => c.domain.endsWith("feishu.cn") || c.domain.endsWith("larksuite.com"))
      .map((c) => ({
        name: c.name,
        value: c.value,
        domain: c.domain.startsWith(".") ? c.domain : c.domain,
        path: c.path || "/",
        secure: !!c.secure,
        httpOnly: !!c.httpOnly,
        expires: c.expires && c.expires > 0 ? c.expires : undefined,
      }));
    for (const c of usable) {
      await page.setCookie(c).catch((e) => console.error("setCookie fail", c.name, e.message.slice(0, 60)));
    }
    console.error(`injected ${usable.length} cookies`);
  }
  await page.goto(authUrl, { waitUntil: "domcontentloaded", timeout: 30000 });
  const deadline = Date.now() + 45000;
  let finalUrl = "";
  while (Date.now() < deadline) {
    finalUrl = page.url();
    if (finalUrl.startsWith("http://localhost:18765/callback")) break;
    try {
      const clicked = await page.evaluate(() => {
        const btns = Array.from(document.querySelectorAll("button"));
        const b = btns.find((x) => (x.textContent || "").trim() === "授权");
        if (b) { b.click(); return true; }
        return false;
      });
      if (clicked) { await new Promise((r) => setTimeout(r, 2500)); continue; }
    } catch (e) { /* 页面跳转中 */ }
    await new Promise((r) => setTimeout(r, 2000));
  }
  finalUrl = page.url();
  if (finalUrl.startsWith("http://localhost:18765/callback")) {
    console.log("CALLBACK_URL=" + finalUrl);
  } else {
    console.log("AUTH_STUCK=" + finalUrl);
  }
} finally {
  await browser.close().catch(() => {});
}
