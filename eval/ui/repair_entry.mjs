// 条件渲染按钮端到端验证：le-fix 出现 → 点击 → 弹层 → 建议 diff → 取消（不真改）
import puppeteer from 'puppeteer-core';

const PORT = process.env.EVO_UI_PORT || '8914';
const URL = `http://127.0.0.1:${PORT}`;
const CHROME = process.env.EVO_CHROME ||
  'C:/Users/lenovo/AppData/Local/ms-playwright/chromium-1243/chrome-win64/chrome.exe';

const out = { url: URL, steps: [] };
const step = (name, ok, detail) => {
  out.steps.push({ name, ok, detail });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  ' + detail : ''}`);
};

const browser = await puppeteer.launch({
  executablePath: CHROME, headless: 'new',
  args: ['--no-sandbox', '--disable-dev-shm-usage'],
});
try {
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 900 });
  await page.goto(URL, { waitUntil: 'networkidle2', timeout: 30000 });

  // 1) 进入 Skills 视图
  await page.$eval('button.vtab[data-view="skills"]', (el) => el.click());
  await new Promise((r) => setTimeout(r, 2500));

  // 2) le-fix 必须出现（这是本轮的关键：条件已满足）
  const fixSel = 'button.le-fix';
  const appeared = await page.waitForSelector(fixSel, { timeout: 15000 }).then(() => true).catch(() => false);
  step('repair-entry-appears', appeared, appeared ? '' : 'button.le-fix 未出现');
  if (!appeared) { out.ok = false; console.log(JSON.stringify(out, null, 1)); process.exit(2); }

  const fixText = await page.$eval(fixSel, (el) => el.textContent.trim());
  step('repair-entry-label', fixText.includes('修复'), `label=${fixText}`);

  // 3) 点击 → 弹层出现（用 DOM click：page.click 会因遮挡/零尺寸报 not clickable）
  await page.$eval(fixSel, (el) => { el.scrollIntoView({block:'center'}); el.click(); });
  // openRepair 复用 #setModal/#setMask（frontend/index.html:844）
  const modalShown = await page.waitForFunction(() => {
    const m = document.querySelector('#setModal');
    return !!m && m.classList.contains('open');
  }, { timeout: 15000 }).then(() => true).catch(() => false);
  step('repair-modal-opens', modalShown);

  // 4) 弹层里有 diff 与"应用修复"
  // 先等"生成建议中…"被真实内容替换（async fetch 有延迟）
  await page.waitForFunction(() => {
    const b = document.querySelector('#setModal .set-body');
    return !!b && !b.textContent.includes('生成建议中');
  }, { timeout: 20000 }).catch(() => {});
  const detail = await page.evaluate(() => {
    const root = document.querySelector('#setModal') || document;
    return {
      hasDiff: !!root.querySelector('.repair-diff'),
      hasApply: [...root.querySelectorAll('button')].some((b) => b.textContent.includes('应用修复')),
      hasAction: !!root.querySelector('.repair-action'),
      text: (root.textContent || '').slice(0, 180),
    };
  });
  step('modal-has-diff', detail.hasDiff);
  step('modal-has-apply-button', detail.hasApply);
  step('modal-has-action-hint', detail.hasAction);

  // 5) 关闭弹层（不真正应用修复——避免改文件）
  const closed = await page.evaluate(() => {
    const modal = document.querySelector('#setModal');
    if (!modal) return false;
    const btn = [...modal.querySelectorAll('button')].find(
      (b) => /取消|关闭/.test(b.textContent));
    if (btn) { btn.click(); return !modal.classList.contains('open'); }
    return false;
  });
  await new Promise((r) => setTimeout(r, 800));
  step('modal-closable', closed);

  out.modalText = detail.text;
  out.ok = out.steps.every((s) => s.ok);
} finally {
  await browser.close();
}
console.log(JSON.stringify(out, null, 1));
process.exit(out.ok ? 0 : 1);
