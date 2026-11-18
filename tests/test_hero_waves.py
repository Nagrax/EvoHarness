"""前端动效/布局回归（2026-11-16 v2：数字员工工作台 / 品牌青绿波纹）。

设计规范见 docs/design/WORKBENCH-UI-DESIGN.md。相对 2026-10-29 v1 的关键演进：
① AmbientLightBg 模块内联且以 IIFE 隔离——v1 直接平铺顶层声明，
   其压缩产物里的 `$` 与主脚本 `const $` 在全局作用域冲突，波纹一直静默挂掉；
② 配色从 price 深海蓝改为品牌青绿系（锚定仓库 logo.png 的天线光点 #4EE0B5）；
③ 侧栏宽度收缩用 grid-template-columns（v1 的 width:0 方案已废）；
④ starter 建议回到空态（工作台形态需要示例任务），数量受减法约束；
⑤ 官方 logo.png 以 data URI 内嵌（--logo-img 单份引用）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

HTML = REPO / "frontend" / "index.html"


def _t() -> str:
    return HTML.read_text(encoding="utf-8")


def test_ambient_module_inlined_and_isolated():
    t = _t()
    assert "window.AmbientLightBg = Ot" in t, "ESM 导出别名必须改写成 window 绑定"
    assert "new AmbientLightBg(" in t
    # IIFE 隔离契约：模块顶层声明（含压缩产物里的 $）不得泄漏到全局作用域
    i_wrap = t.find("<script>\n(function(){\nfunction t(t,e){")
    assert i_wrap > 0, "压缩模块必须以 IIFE 包裹开头"
    assert "window.AmbientLightBg = Ot;\n})();" in t, "IIFE 必须在导出后闭合"


def test_ambient_params_match_price_component():
    """运行参数必须与 AmbientLightBg.tsx 一致；配色为品牌青绿系（§3.1）。"""
    t = _t()
    assert "speed: 0.25" in t
    assert "instance.update('pattern scale', 0.9)" in t
    assert "instance.update('edge blur', 0.06)" in t
    assert "instance.update('noise', 0.08)" in t
    assert "'#0B3B33'" in t and "'#17705E'" in t, "应使用品牌青绿 dark colors"
    assert "'#004880'" not in t, "v1 的 price 深海蓝配色不得残留"


def test_ambient_mounted_under_main_not_hero():
    t = _t()
    i_main = t.find('class="main"')
    i_amb = t.find('id="ambientBg"')
    assert 0 < i_main < i_amb, "ambient 容器必须在 .main 之后"
    hero_start = t.find('id="hero"')
    assert not (hero_start < i_amb < hero_start + 2000), "不应嵌在 hero 里"


def test_starters_bounded_by_subtraction():
    """空态 starter 建议：保留（工作台示例任务）但不得超过 4 个（界面减法）。"""
    t = _t()
    n = t.count('class="starter"')
    assert 0 < n <= 4, f"starter 数量应在 1~4，实际 {n}"


def test_sidebar_drawer_uses_grid_collapse():
    t = _t()
    assert ".app.side-closed{grid-template-columns:56px 1fr}" in t, "抽屉应用 grid 列宽收缩"
    assert "margin-left:236px" not in t, "不得用 margin 位移（flex 里会溢出）"
    assert 'id="sideToggle"' in t


def test_dark_theme_tokens():
    t = _t().replace(" ", "")
    assert "--bg:#070d10" in t, "v2 页面底色（深蓝黑带青绿倾向）"
    assert "--acc:#4EE0B5" in t, "品牌强调色 = logo 天线光点"
    assert "color-scheme:dark" in t


def test_official_logo_embedded():
    t = _t()
    assert '--logo-img:url("data:image/png;base64,' in t, "官方 logo.png 以 CSS 单份内嵌"
    assert t.count("data:image/png;base64,") <= 2, "data URI 至多两处（CSS + favicon），不得逐元素重复"


def test_controls_still_addressable():
    """关键控件 id 必须都还在（布局大改不能丢元素）。"""
    t = _t()
    for cid in ("nav", "newChat", "sessList", "skRefresh", "skTable",
                "input", "send", "sideToggle", "ambientBg",
                "homeAlerts", "homeWork", "homeCaps", "dyBody",
                "status", "modeRow", "srv", "drawer"):
        assert f'id="{cid}"' in t, f"控件 {cid} 丢失"


def test_workbench_navigation_views_complete():
    """工作台八视图路由齐备（super-agent 三问 + DE-43 组织/目标/知识）。"""
    t = _t()
    m = re.search(r"const VIEWS=\[([^\]]+)\]", t)
    assert m, "VIEWS 路由表缺失"
    for v in ("home", "work", "org", "goals", "knowledge", "skills", "eval", "daily"):
        assert f"'{v}'" in m.group(1), f"视图 {v} 缺失"
    # 组织视图必须走 /api/work/* 契约并在未配置库时诚实降级（不渲染假数据）
    assert "/api/work/org?" in t and "/api/work/view?" in t
    assert "/api/work/knowledge?" in t
    assert "database_not_configured" in t


def test_org_view_uses_official_api_contract():
    """组织视图的 API 参数与 evoharness 契约层一致（tenant/owner/org）。"""
    from evoharness.__main__ import SEED_OWNER, SEED_TENANT

    t = _t()
    assert f"OG_TENANT='{SEED_TENANT}'" in t
    assert f"OG_OWNER='{SEED_OWNER}'" in t
    assert "encodeURIComponent(org)" in t


def test_ambient_renders_only_while_visible_with_a_time_based_clock():
    """The app owns frame scheduling, visibility, context loss and reduced motion."""
    import shutil
    import subprocess

    import pytest

    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for frontend JavaScript")
    script = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync(process.argv[1], 'utf8');
const source = html.match(/<script>\s*\/\* 工作台只在可见[^]*?<\/script>/)[0].slice(8, -9);
const frames = new Map();
const handlers = {};
const calls = [];
const animationTimes = [];
let observer, nextFrame = 0, instances = 0;
const motion = {matches: false, addEventListener(name, fn) {handlers.motion = fn;}};
const canvas = {
  hidden: false,
  addEventListener(name, fn) {handlers[name] = fn;},
  toggleAttribute(name, hidden) {this.hidden = hidden;},
};
const host = {
  parentNode: null,
  querySelector() {return canvas;},
  replaceChildren() {calls.push('static fallback');},
};
function surface() {return {prepend(node) {node.parentNode = this;}};}
const home = surface(), chat = surface();
home.prepend(host);
const document = {
  hidden: false, body: {dataset: {view: 'home'}},
  getElementById(id) {return {ambientBg: host, homeCover: home, hero: chat}[id];},
  addEventListener(name, fn) {handlers[name] = fn;},
};
const context = {
  document, matchMedia: () => motion,
  requestAnimationFrame(fn) {frames.set(++nextFrame, fn); return nextFrame;},
  cancelAnimationFrame(id) {frames.delete(id);},
  IntersectionObserver: class {
    constructor(fn) {observer = value => fn([{isIntersecting: value}]);}
    observe() {}
    disconnect() {}
  },
  MutationObserver: class {
    constructor(fn) {handlers.route = fn;}
    observe() {}
  },
  AmbientLightBg: class {
    constructor(options) {
      instances++;
      assert.equal(options.manual, true);
      assert.equal(options.measure().w * options.measure().h, 155520);
      assert.equal(options.textureSize, 192);
      this.gl = {canvas, clearColor() {}};
      this.renderer = {render(pass) {calls.push(pass.target ? 'texture' : 'screen');}};
      this.rtt = {};
    }
    update() {}
    _animate() {animationTimes.push(this.frame);}
  },
};
context.window = context;
vm.runInNewContext(source, context);
function frame(time) {
  assert.equal(frames.size, 1);
  const [id, next] = frames.entries().next().value;
  frames.delete(id);
  next(time);
}
assert.equal(instances, 0);
observer(true);
observer(true);
frame(10);
frame(27);
assert.deepEqual(animationTimes, [0, 1.02]);
assert.deepEqual(calls, ['texture','screen','texture','screen']);
document.body.dataset.view = 'work';
handlers.route();
assert.equal(frames.size, 0);
document.body.dataset.view = 'chat';
handlers.route();
assert.equal(host.parentNode, chat);
assert.equal(frames.size, 0);
observer(true);
frame(1000);
assert.equal(animationTimes.at(-1), 1.02);
document.hidden = true;
handlers.visibilitychange();
assert.equal(frames.size, 0);
document.hidden = false;
handlers.visibilitychange();
assert.equal(frames.size, 1);
motion.matches = true;
handlers.motion(motion);
assert.equal(canvas.hidden, true);
assert.equal(frames.size, 0);
motion.matches = false;
handlers.motion(motion);
assert.equal(canvas.hidden, false);
frame(2000);
assert.equal(animationTimes.at(-1), 1.02);
observer(false);
assert.equal(frames.size, 0);
observer(true);
handlers.webglcontextlost();
assert.equal(calls.at(-1), 'static fallback');
assert.equal(frames.size, 0);
assert.equal(instances, 1);
'''
    result = subprocess.run(
        [node, "-e", script, str(HTML)], capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
