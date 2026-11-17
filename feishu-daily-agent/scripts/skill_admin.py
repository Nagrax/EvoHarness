"""skill 运维命令：查看多层 skill、手动重建、从 skill 反向导入。

用法（在 feishu-daily-agent 目录）：
    python scripts/skill_admin.py list        # 看当前有哪些领域 skill
    python scripts/skill_admin.py rebuild     # 按库里经验全量重建
    python scripts/skill_admin.py import feishu-it   # skill → 飞书候选
    python scripts/skill_admin.py roster      # 看日报收件人花名册
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REPO = ROOT.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
import os

os.environ.setdefault("EVOHARNESS_ROOT", str(REPO))


def cmd_list() -> int:
    from app.core.database import session_scope
    from app.memory import skill_bridge, skill_sync
    from app.models import Experience

    items = skill_bridge.list_skill_domains()
    if not items:
        print("还没有由飞书派生的 skill（未发布过经验，或技能目录为空）")
        return 0
    print(f"{'领域':<12}{'层次':<14}{'分节':<6}{'字节':<8}路径")
    for item in items:
        tier = skill_bridge.tier_of(item["domain"])
        print(
            f"{item['domain']:<12}{tier:<14}{item['sections']:<6}"
            f"{item['bytes']:<8}{item['path']}"
        )

    # 晋升建议：上游要求人工放行，这里只给判断依据
    print("\n晋升建议（需人工执行 manual_promote，系统不会自动提权）：")
    with session_scope() as session:
        rows = list(session.query(Experience).all())
        by_domain: dict[str, list[dict]] = {}
        for exp in rows:
            by_domain.setdefault(skill_sync.domain_of(exp), []).append(
                {"status": exp.status}
            )
    for domain in sorted(by_domain):
        print(f"  {domain:<12}{skill_bridge.promotion_hint(domain, by_domain[domain])}")
    return 0


def cmd_rebuild() -> int:
    from app.core.database import session_scope
    from app.memory import skill_sync

    with session_scope() as session:
        results = skill_sync.sync_all(session)
    if not results:
        print("库里没有可投影的经验")
        return 0
    for r in results:
        print(f"{r['domain']:<12}{r['action']:<12}{len(r['experienceIds'])} 条")
    return 0


def cmd_import(slug: str) -> int:
    from app.core.config import get_settings
    from app.core.database import session_scope
    from app.memory import skill_bridge

    settings = get_settings()
    with session_scope():
        pass
    result = skill_bridge.import_skill_as_candidates(slug, tenant=settings.tenant_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


def cmd_roster() -> int:
    from app.core.database import session_scope
    from app.services import roster

    with session_scope() as session:
        data = roster.build_roster(session).as_dict()
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    cmd = args[0]
    if cmd == "list":
        return cmd_list()
    if cmd == "rebuild":
        return cmd_rebuild()
    if cmd == "import":
        if len(args) < 2:
            print("需要 skill 目录名，例如：import feishu-it")
            return 2
        return cmd_import(args[1])
    if cmd == "roster":
        return cmd_roster()
    print(f"未知命令 {cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
