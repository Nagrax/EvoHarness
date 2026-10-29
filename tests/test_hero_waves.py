"""前端动效/布局回归（2026-10-29 最终形态：内联 AmbientLightBg + 抽屉侧栏）。

关键事实：
① 直接内联 price-agent-next 的 `AmbientLightBg.module.js`（自包含 ESM）
② 它导出为 `export{Ot as AmbientLightBg}`，内联必须改写为 `window.AmbientLightBg = Ot`
③ 容器挂到 `.main` 下（全屏），不是 hero 内（否则 canvas 尺寸偏小、出现怪色带）
④ 四个 starter 卡片已删除
⑤ 侧栏是宽度收缩式抽屉（不用 fixed+margin，那在 flex 里会溢出）
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

HTML = REPO / "frontend" / "index.html"


def _t() -> str:
    return HTML.read_text(encoding="utf-8")


def test_ambient_module_inlined_and_export_rewritten():
    t = _t()
    assert "window.AmbientLightBg = Ot" in t, "ESM 导出别名必须改写成 window 绑定"
    assert "new AmbientLightBg(" in t
    assert 'type="module"' in t


def test_ambient_params_match_price_component():
    """参数必须与 AmbientLightBg.tsx 一致。"""
    t = _t()
    assert "speed: 0.25" in t
    assert "instance.update('pattern scale', 0.9)" in t
    assert "instance.update('edge blur', 0.06)" in t
    assert "instance.update('noise', 0.08)" in t
    assert '"#004880"' in t and '"#3390C0"' in t, "应使用 price 的 dark colors"


def test_ambient_mounted_under_main_not_hero():
    t = _t()
    i_main = t.find('class="main"')
    i_amb = t.find('id="ambientBg"')
    assert 0 < i_main < i_amb, "ambient 容器必须在 .main 之后"
    hero_start = t.find('id="hero"')
    assert not (hero_start < i_amb < hero_start + 2000), "不应再嵌在 hero 里"


def test_starters_removed():
    assert '<div class="starters">' not in _t(), "四个 starter 卡片应已删除"


def test_sidebar_drawer_uses_width_collapse():
    t = _t()
    assert ".app.side-closed .side{width:0" in t, "抽屉应收起宽度"
    assert "margin-left:236px" not in t, "不得用 margin 位移（flex 里会溢出）"
    assert 'id="sideToggle"' in t


def test_composer_centered_when_idle():
    t = _t()
    assert "body:not(.chatting) .comp" in t
    assert "document.body.classList.add('chatting')" in t, "发送时应切到对话态"


def test_dark_theme_tokens():
    t = _t().replace(" ", "")
    assert "--bg:#08111F" in t
    assert "--txt:#E6EDF3" in t
    assert "color-scheme:dark" in t


def test_controls_still_addressable():
    """关键控件 id 必须都还在（布局大改不能丢元素）。"""
    t = _t()
    for cid in ("newChat", "sessList", "viewTabs", "skRefresh", "skTable",
                "input", "send", "sideToggle"):
        assert f'id="{cid}"' in t, f"控件 {cid} 丢失"