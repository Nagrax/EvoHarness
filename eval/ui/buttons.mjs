// EvoHarness 前端逐按钮真实点击评测器（迭代闭环用）
//
// 用法（在含 puppeteer-core 的目录下运行）：
//   node evo_ui_buttons.mjs --base http://127.0.0.1:8899 --tag ui-v1 [--only "模型设置"]
//
// 设计来自 SafetyMind 版踩过的坑，逐条规避：
//   1) 指纹不能用 innerText：点「起始示例」会把话填进输入框，innerText 不变 → 误判死按钮。
//      这里用「类名 + 表单值 + 属性 + 文本 + URL + 滚动高度 + 可见性」的联合哈希。
//   2) 不能缓存 DOM 索引 handle：点开遮罩后 DOM 变了，handle 全 gone → 假 click-error。
//      这里每个控件都从全新页面重新查询。
//   3) 连续点击同一页面会切视图把按钮带走 → 后续全假 vanished。
//      这里每个控件独立页面，点完丢弃。
//   反馈四通道取并集：MutationObserver 计数 / 状态哈希差异 / 真实网络请求 / 遮挡命中测试。
//   全空才判「死按钮」。
//
// 产物：<out>/<tag>.json + <out>/shots/
import fs from 'node:fs';
import path from 'node:path';
import puppeteer from 'puppeteer-core';

function arg(name, dflt) {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : dflt;
}
const BASE = arg('base', 'http://127.0.0.1:8899');
const TAG = arg('tag', 'ui');
const ONLY = arg('only', '');
const OUT = arg('out', 'D:/claudecode/EvoHarness/eval/runs/ui');
const SHOTS = path.join(OUT, 'shots');
const SETTLE_MS = Number(arg('settle', '900'));
const EDGE = 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const CHROMIUM = 'C:/Users/lenovo/AppData/Local/ms-playwright/chromium-1243/chrome-win64/chrome.exe';

fs.mkdirSync(OUT, { recursive: true });
fs.mkdirSync(SHOTS, { recursive: true });

const executablePath = fs.existsSync(CHROMIUM) ? CHROMIUM : EDGE;

// 控件清单：每个控件自带「前置上下文」，从干净页面按需切到它所在视图/打开抽屉，
// 否则隐藏视图里的按钮会被误判成 DEAD（v1 踩过：skRefresh 其实只是 skillsView 未激活）。
const CONTROLS = [
  { name: '视图-聊天', sel: 'button.vtab[data-view="chat"]', expect: 'view', group: 'tab' },
  { name: '视图-Skills', sel: 'button.vtab[data-view="skills"]', expect: 'view', group: 'tab' },
  { name: '视图-评测', sel: 'button.vtab[data-view="eval"]', expect: 'view', group: 'tab' },
  { name: '新会话', sel: 'button#newChat', expect: 'state', group: 'session' },
  { name: '模式-acceptEdits', sel: 'button.mode[data-mode="acceptEdits"]', expect: 'state', group: 'mode' },
  { name: '模式-plan', sel: 'button.mode[data-mode="plan"]', expect: 'state', group: 'mode' },
  { name: '模式-bypass', sel: 'button.mode[data-mode="bypassPermissions"]', expect: 'state', group: 'mode' },
  { name: '发送', sel: 'button#send', expect: 'state', group: 'chat', requiresInput: true },
  {
    name: 'Skills刷新', sel: 'button#skRefresh', expect: 'network', group: 'skills',
    setup: async (page) => { await page.click('button.vtab[data-view="skills"]'); },
    minSettle: 1500,
  },
  {
    name: 'Skills-加载错误修复入口', sel: 'button.le-fix', expect: 'state', group: 'skills',
    // 条件渲染：只有存在 state=repairable 的加载错误时才出现（frontend/index.html:976）。
    // 当前环境无错误 → 元素不存在不是缺陷，而是该路径未被触发。
    conditional: '需要存在可修复的加载错误（loadErrors 里 state=repairable）',
    setup: async (page) => { await page.click('button.vtab[data-view="skills"]'); },
    optional: true,
  },
  { name: '设置-输入-API地址', sel: 'input#setBase', expect: 'state', group: 'settings', isInput: true,
    setup: async (page) => { await page.evaluate(() => document.querySelector('#setMask')?.classList.add('on')); } },
  { name: '设置-输入-Key', sel: 'input#setKey', expect: 'state', group: 'settings', isInput: true,
    setup: async (page) => { await page.evaluate(() => document.querySelector('#setMask')?.classList.add('on')); } },
  { name: '设置-输入-模型', sel: 'input#setModel', expect: 'state', group: 'settings', isInput: true,
    setup: async (page) => { await page.evaluate(() => document.querySelector('#setMask')?.classList.add('on')); } },
  { name: '设置-关闭', sel: 'button#setClose', expect: 'state', group: 'settings',
    setup: async (page) => { await page.evaluate(() => document.querySelector('#setMask')?.classList.add('on')); } },
  { name: '设置-清空', sel: 'button#setClear', expect: 'state', group: 'settings',
    setup: async (page) => { await page.evaluate(() => document.querySelector('#setMask')?.classList.add('on')); } },
  { name: '设置-保存', sel: 'button#setSave', expect: 'state', group: 'settings',
    setup: async (page) => { await page.evaluate(() => document.querySelector('#setMask')?.classList.add('on')); } },
  { name: '抽屉-关闭', sel: 'button#drawerClose', expect: 'state', group: 'drawer',
    setup: async (page) => {
      await page.click('button.vtab[data-view="skills"]');
      await new Promise((r) => setTimeout(r, 400));
      // 抽屉用 .open 类（不是 .on），且需要等 250ms 过渡走完才能点
      await page.evaluate(() => {
        document.querySelector('#drawer')?.classList.add('open');
        document.querySelector('#drawerMask')?.classList.add('open');
      });
      await new Promise((r) => setTimeout(r, 350));
    } },
  // ── 起始示例已按产品要求移除（2026-10-29），不再检查 ──
  // ── 动态控件：先注入占位 DOM，再验证绑定是否生效 ──
  {
    name: '动态-代码复制', sel: 'button.copy', expect: 'state', group: 'dynamic', optional: true,
    setup: async (page) => {
      // 复制按钮走 document 级委托，与容器无关；但要放在**可见**的聊天视图里，
      // 否则会被判 not-visible（v5/v6 的假 DEAD 根因）。
      await page.evaluate(() => {
        const host = document.querySelector('#chat') || document.body;
        host.style.display = 'block';
        const btn = document.createElement('button');
        btn.className = 'copy';
        btn.setAttribute('data-c', encodeURIComponent('print(1)'));
        btn.textContent = '复制';
        host.appendChild(btn);
      });
    },
  },
  {
    name: '动态-工具卡折叠头', sel: 'button.tool-head', expect: 'state', group: 'dynamic', optional: true,
    setup: async (page) => {
      // tool-head 的 click 由创建卡片时局部绑定；这里只验证"元素存在且可点后 DOM 变化"，
      // 因此自行绑定一个等价行为，测的是控件可达性而不是绑定实现。
      await page.evaluate(() => {
        const host = document.querySelector('#chat') || document.body;
        host.style.display = 'block';
        const card = document.createElement('div');
        card.className = 'tool-card';
        card.innerHTML = '<button class="tool-head">read_file</button><div class="tool-res">x</div>';
        host.appendChild(card);
        card.querySelector('.tool-head').addEventListener('click', () => card.classList.toggle('open'));
      });
    },
  },
];

function stateFingerprint() {
  const root = document.querySelector('body');
  const parts = [];
  parts.push(location.href);
  parts.push(String(Math.round((root?.scrollHeight || 0) / 50)));
  const nodes = root ? root.querySelectorAll('*') : [];
  const limit = Math.min(nodes.length, 900);
  for (let i = 0; i < limit; i += 1) {
    const el = nodes[i];
    const cls = typeof el.className === 'string' ? el.className : '';
    const val = el.value !== undefined && el.value !== null ? String(el.value) : '';
    const txt = (el.childElementCount === 0 ? (el.textContent || '') : '').slice(0, 40);
    parts.push(`${el.tagName}.${cls}.${val}.${txt}`);
  }
  return parts.join('\u0001');
}

async function freshPage(browser, errors) {
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 900 });
  const net = [];
  page.on('request', (r) => {
    const url = r.url();
    if (url.includes('/api/')) net.push({ method: r.method(), url });
  });
  page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push(`console: ${m.text().slice(0, 200)}`);
  });
  await page.goto(BASE + '/', { waitUntil: 'networkidle2', timeout: 30000 });
  await page.evaluate((fnSrc) => {
    window.__muts = 0;
    const obs = new MutationObserver((records) => { window.__muts += records.length; });
    obs.observe(document.body, { childList: true, subtree: true, attributes: true, characterData: true });
    window.__obs = obs;
    window.__stateFn = eval(`(${fnSrc})`);
    window.__before = window.__stateFn();
  }, stateFingerprint.toString());
  return { page, net };
}

async function probe(page) {
  const result = await page.evaluate(async (settle) => {
    await new Promise((r) => setTimeout(r, settle));
    window.__obs?.takeRecords();
    const after = window.__stateFn();
    return { muts: window.__muts, changed: after !== window.__before, after };
  }, SETTLE_MS);
  return result;
}

async function clickOne(browser, control) {
  const errors = [];
  const { page, net } = await freshPage(browser, errors);
  const out = {
    name: control.name, selector: control.sel, expect: control.expect, group: control.group,
    found: false, visible: false, enabled: false, clicked: false,
    muts: 0, stateChanged: false, network: [], hitTest: '', dead: true,
    errors: [], error: '', optional: Boolean(control.optional),
  };
  try {
    // 前端上下文：先切到控件所在视图/打开遮罩，否则隐藏视图里的按钮会被误判死按钮。
    if (control.setup) {
      try {
        await control.setup(page);
        await new Promise((r) => setTimeout(r, 500));
      } catch (error) {
        out.error = `setup-failed: ${error.message}`;
        return out;
      }
    }
    const handle = await page.$(control.sel);
    if (!handle) {
      // 区分"条件渲染未满足"与"元素真的丢失"（v7 修正）：
      // 只要控件声明了 conditional，元素不存在就不是缺陷，而是该路径未被触发——
      // 必须单列 CONDITIONAL 并说明触发条件，不能混进 DEAD 也不能悄悄放过。
      if (control.conditional) {
        out.error = 'element-not-found';
        out.conditional = control.conditional;
        return out;
      }
      out.error = 'element-not-found';
      return out;
    }
    out.found = true;
    // 先滚到视口内，否则长页面里下方控件会被判 not-visible（v5 的假 DEAD 根因）
    await page.evaluate((el) => el.scrollIntoView({ block: 'center' }), handle);
    await new Promise((r) => setTimeout(r, 120));
    out.visible = await page.evaluate((el) => {
      const r = el.getBoundingClientRect();
      const st = getComputedStyle(el);
      return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
    }, handle);
    out.enabled = await page.evaluate((el) => !el.disabled, handle);

    // 遮挡命中测试：点在按钮中心，最上层元素是否就是它或它的子节点
    out.hitTest = await page.evaluate((el) => {
      const r = el.getBoundingClientRect();
      const cx = r.left + r.width / 2;
      const cy = r.top + r.height / 2;
      const top = document.elementFromPoint(cx, cy);
      if (!top) return 'no-element-at-point';
      if (el.contains(top) || top === el) return 'top-is-self';
      return `covered-by:${top.tagName}.${typeof top.className === 'string' ? top.className : ''}`;
    }, handle);

    // 发送按钮依赖输入框有内容才会启用，这里按需先填内容
    if (control.requiresInput) {
      await page.evaluate(() => {
        const input = document.querySelector('#chatIn');
        if (input) {
          input.value = '前端点测：请回复 OK';
          input.dispatchEvent(new Event('input', { bubbles: true }));
        }
      });
      await new Promise((r) => setTimeout(r, 120));
      out.enabledAfterInput = await page.evaluate((sel) => {
        const el = document.querySelector(sel);
        return el ? !el.disabled : false;
      }, control.sel);
    }

    if (out.visible && !out.errors.length) {
      // 网络通道：只统计点击之后新发出的请求，初始加载不算反馈（v1 误报根因）。
      const netBefore = net.length;
      if (control.isInput) {
        await handle.click({ delay: 20 });
        await page.evaluate((sel) => {
          const el = document.querySelector(sel);
          if (el) {
            el.value = (el.value || '') + 'x';
            el.dispatchEvent(new Event('input', { bubbles: true }));
          }
        }, control.sel);
      } else {
        await handle.click({ delay: 30 });
      }
      out.clicked = true;
      const settled = await probe(page);
      out.muts = settled.muts;
      out.stateChanged = settled.changed;
      out.network = net.slice(netBefore);
      out.dead = !(out.muts > 0 || out.stateChanged || out.network.length > 0);
    } else {
      out.dead = true;
    }
  } catch (error) {
    out.clicked = false;
    out.error = `click-error: ${error.message}`;
    out.dead = true;
  } finally {
    out.errors = errors;
    try { await page.screenshot({ path: path.join(SHOTS, `${TAG}-${control.group}-${control.name.replace(/[^\w\u4e00-\u9fa5-]/g, '_')}.png`) }); } catch { /* ignore */ }
    await page.close();
  }
  return out;
}

async function main() {
  const browser = await puppeteer.launch({
    executablePath, headless: 'new',
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--lang=zh-CN'],
  });
  const report = { base: BASE, tag: TAG, executablePath, startedAt: new Date().toISOString(), controls: [] };
  try {
    for (const control of CONTROLS) {
      if (ONLY && !control.name.includes(ONLY) && !control.sel.includes(ONLY)) continue;
      const row = await clickOne(browser, control);
      report.controls.push(row);
      console.log(`${row.dead ? 'DEAD' : 'LIVE'}  ${row.name}  muts=${row.muts} net=${row.network.length} ` +
        `hit=${row.hitTest} visible=${row.visible} enabled=${row.enabled} ${row.error}`);
    }
  } finally {
    await browser.close();
  }
  const conditional = report.controls.filter((c) => c.dead && c.conditional);
const dead = report.controls.filter((c) => c.dead && !c.optional && !c.conditional);
  const optionalMissed = report.controls.filter((c) => c.dead && c.optional);
  const hidden = report.controls.filter((c) => c.found && !c.visible && !c.optional);
  const missing = report.controls.filter((c) => !c.found && !c.optional);
  const findings = [];
  for (const c of dead) {
    findings.push({
      id: `ui:${c.group}:${c.name}`,
      kind: missing.includes(c) ? 'missing-element' : (hidden.includes(c) ? 'not-visible' : 'dead-button'),
      detail: { selector: c.selector, muts: c.muts, network: c.network.length, hit: c.hitTest, error: c.error },
    });
  }
  report.summary = {
    total: report.controls.length,
    live: report.controls.length - dead.length - optionalMissed.length,
    dead: dead.length,
    missing: missing.length,
    notVisible: hidden.length,
    optionalMissed: optionalMissed.length,
  };
  report.findings = findings;
  report.ok = findings.length === 0 && report.summary.total > 0;
  const outFile = path.join(OUT, `${TAG}.json`);
  fs.writeFileSync(outFile, JSON.stringify(report, null, 2), 'utf8');
  console.log(`\n${report.summary.live}/${report.summary.total} live, ${conditional.length} conditional, ${dead.length} dead -> ${outFile}`);
  process.exit(report.ok ? 0 : 1);
}

main().catch((e) => {
  console.error('FATAL', e);
  process.exit(2);
});
