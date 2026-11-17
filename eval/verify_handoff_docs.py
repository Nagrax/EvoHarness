"""校验交接文档里引用的链接与命令是否真实存在。

为什么需要：交接文档最大的失败模式是"写得很全，但引用的文件/命令是错的"，
接手者照做就卡住，反而浪费更多时间。这里做机器校验，不靠人眼。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]

DOCS = [
    ROOT / "docs" / "AGENT-HANDOFF.md",
    ROOT / "docs" / "与原版的差异.md",
    ROOT / "docs" / "数字分身-交接文档.md",
    ROOT / "docs" / "飞书机器人使用手册.md",
    ROOT / "README.md",
    ROOT / "feishu-daily-agent" / "README.md",
    ROOT / "本地部署指南.md",
]

LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")

# 上游 README 引用了 docs/screenshots/ 与 docs/design/，但这两个目录
# 在 jsDelivr 拉取的快照里就不存在（图片与设计文档未随包分发）。
# 这不是本次改动引入的，也不能靠改文档"修好"——需要原作者补文件。
# 这里显式登记为已知缺口，避免校验器噪音掩盖真正的新问题。
KNOWN_UPSTREAM_GAPS = {"docs/screenshots/", "docs/design/"}

problems: list[str] = []
known: list[str] = []
checked = 0

for doc in DOCS:
    if not doc.is_file():
        problems.append(f"文档缺失: {doc}")
        continue
    text = doc.read_text(encoding="utf-8")
    for raw in LINK_RE.findall(text):
        target = raw.strip()
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        target = target.split("#", 1)[0]
        if not target:
            continue
        checked += 1
        resolved = (doc.parent / target).resolve()
        if resolved.exists():
            continue
        if any(target.startswith(gap) for gap in KNOWN_UPSTREAM_GAPS):
            known.append(f"{doc.name} → 上游缺口: {target}")
        else:
            problems.append(f"{doc.name} → 失效链接: {raw}")

# 校验文档里提到的关键代码文件真实存在
code_refs = [
    "feishu-daily-agent/app/services/oauth.py",
    "feishu-daily-agent/app/services/identity.py",
    "feishu-daily-agent/app/services/roster.py",
    "feishu-daily-agent/app/services/subscription.py",
    "feishu-daily-agent/app/memory/skill_bridge.py",
    "feishu-daily-agent/app/memory/skill_sync.py",
    "feishu-daily-agent/app/pipeline/poller.py",
    "feishu-daily-agent/tests/test_digital_twin_boundary.py",
    "feishu-daily-agent/tests/test_digital_twin_ownership.py",
    "feishu-daily-agent/tests/test_twin_send_identity.py",
    "feishu-daily-agent/tests/test_migration_twin_sources.py",
    "feishu-daily-agent/scripts/oauth_server.py",
    "feishu-daily-agent/scripts/verify_twin_refresh.py",
    "panel/panel.py",
    "integration/iterate.py",
]
for rel in code_refs:
    checked += 1
    if not (ROOT / rel).is_file():
        problems.append(f"文档声称存在的代码不存在: {rel}")

print(f"检查链接/引用 {checked} 处")
if known:
    print(f"\n上游已知缺口（非本次引入，共 {len(known)} 处）：")
    for item in sorted(set(known)):
        print("  ~", item)
if problems:
    print(f"\n发现 {len(problems)} 个问题：")
    for item in problems:
        print("  ✗", item)
    raise SystemExit(1)
print("\n本地文档链接全部通过 ✅")
