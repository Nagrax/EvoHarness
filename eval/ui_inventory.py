#!/usr/bin/env python3
"""枚举前端全部可交互控件（含动态生成的），供逐按钮评测登记。

输出：eval/runs/ui/inventory.json —— 控件清单 + 缺失登记提示。
判据：按钮/onclick 绑定/输入框/可点击卡片，都算"用户能点或能改的东西"。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HTML = REPO / "frontend" / "index.html"


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    text = HTML.read_text(encoding="utf-8")

    static_buttons = []
    for match in re.finditer(r"<button([^>]*)>", text):
        attrs = match.group(1)
        ident = re.search(r'id="([^"]+)"', attrs)
        cls = re.search(r'class="([^"]+)"', attrs)
        data_view = re.search(r'data-view="([^"]+)"', attrs)
        data_mode = re.search(r'data-mode="([^"]+)"', attrs)
        data_p = re.search(r'data-p="([^"]{0,40})', attrs)
        static_buttons.append({
            "id": ident.group(1) if ident else "",
            "class": cls.group(1) if cls else "",
            "dataView": data_view.group(1) if data_view else "",
            "dataMode": data_mode.group(1) if data_mode else "",
            "dataPrompt": data_p.group(1) if data_p else "",
            "offset": match.start(),
        })

    dynamic_templates = []
    for pattern, label in (
        (r'class="copy"', "代码复制按钮"),
        (r'class="sess-item', "会话项"),
        (r'class="tool-head"', "工具卡折叠头"),
        (r'class="le-fix"', "加载错误修复入口"),
        (r'class="starter"', "起始示例"),
        (r'class="mode', "权限模式按钮"),
    ):
        hits = len(re.findall(pattern, text))
        dynamic_templates.append({"label": label, "pattern": pattern, "occurrences": hits})

    onclick_handlers = sorted(set(re.findall(r"\$\('#([A-Za-z0-9_]+)'\)\.onclick", text)))
    direct_handlers = sorted(set(re.findall(r"onclick=\"([A-Za-z0-9_]+)\(", text)))

    inputs = []
    for match in re.finditer(r"<(input|textarea)([^>]*)>", text):
        attrs = match.group(2)
        ident = re.search(r'id="([^"]+)"', attrs)
        inputs.append({"tag": match.group(1), "id": ident.group(1) if ident else ""})

    report = {
        "file": str(HTML.relative_to(REPO)),
        "bytes": len(text.encode("utf-8")),
        "staticButtons": static_buttons,
        "staticButtonCount": len(static_buttons),
        "dynamicTemplates": dynamic_templates,
        "onclickHandlers": onclick_handlers,
        "inlineHandlers": direct_handlers,
        "inputs": inputs,
        "notes": [
            "动态模板（copy/sess-item/tool-head/le-fix）需要先有数据才出现，"
            "逐按钮评测必须通过 setup 预置数据或占位 DOM，否则会误判 element-not-found。",
        ],
    }
    out = REPO / "eval" / "runs" / "ui" / "inventory.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "ok": True,
        "staticButtons": len(static_buttons),
        "dynamicTemplates": sum(1 for t in dynamic_templates if t["occurrences"]),
        "onclickHandlers": len(onclick_handlers),
        "inputs": len(inputs),
        "out": str(out),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
