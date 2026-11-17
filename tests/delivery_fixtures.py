"""Synthetic delivery manifests for validator tests; these are not project status.

Generate the former document-shaped inputs in a temporary directory so a clean
checkout tests the same contracts without distributing private audit snapshots.
"""
from functools import lru_cache
import atexit
import json
from pathlib import Path
import tempfile


@lru_cache(maxsize=1)
def delivery_fixture() -> Path:
    directory = tempfile.TemporaryDirectory(prefix="evo-contract-")
    atexit.register(directory.cleanup)
    root = Path(directory.name)

    def write(rel, value):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(value, encoding="utf-8")

    tasks = [{"id": f"DE-{i:02d}", "optional": i in (26, 27)} for i in range(45)]
    states = {t["id"]: ("optional" if t["optional"] else "done" if i < 13 else "blocked")
              for i, t in enumerate(tasks)}
    write("docs/digital-employee/tasks.json", json.dumps({"tasks": tasks}))
    evidence = "fixtures/verified.txt"
    write(evidence, "Synthetic verifier receipt; not a product acceptance result.\n不宣称通过；不得宣称真实试点通过。\n")
    lines = ["# 合成验收输入", "", "45 项 = done 13；blocked 30；optional 2。", "",
             "## 2. DE 工单状态判定表", "", "| 工单 | 状态 | 证据 | 阻塞原因与备注 |",
             "|---|---|---|---|"]
    for t in tasks:
        lines.append(f"| {t['id']} | {states[t['id']]} | {evidence} | 合成外部依赖尚未具备 |")
    lines += ["", "## 3. 原 F 门禁现状核对", "", "| 门禁 | 状态 | 阻塞条件 | 证据位置 |", "|---|---|---|---|"]
    for gid, note in [("F01", "合成权限尚未授予，等待授权"),
                      ("F08", "合成自然日样本未满足要求"),
                      ("F09", "合成独立样本不足 60 条")]:
        lines.append(f"| {gid} | blocked | {note} | {evidence} |")
    lines += ["", "## 4. 试点通过与收益声明判定表", "", "| 声明键 | 值 |", "|---|---|"]
    for key in ("basic_pilot_passed", "search_enhancement_passed", "search_benefit_claimed",
                "release_certificate_obtained", "basic_pilot_equals_search"):
        lines.append(f"| {key} | false |")
    lines += ["", "## 8. 搜索增强线（DE-29～36）真实状态", "", "| 工单 | 状态 | 证据 | 阻塞原因与备注 |", "|---|---|---|---|"]
    for i in range(29, 37):
        lines.append(f"| DE-{i:02d} | blocked | {evidence} | 合成评测条件未满足要求 |")
    lines += ["", "## 灰度阶梯定义", "合成验证输入：影子、逐条批准、限定自动。", "", "## 停止/撤权/更正演练", "合成验证输入：撤权后不发送。", "", "## S2 状态判定规则", "合成验证输入：证据不全不完成。", "", "## 剩余外部依赖与可选工作", "合成验证输入：未满足的依赖保留阻塞。"]
    write("docs/digital-employee/acceptance/FINAL-ACCEPTANCE.md", "\n".join(lines) + "\n")
    write("docs/digital-employee/acceptance/DE-44/ORGANIZATION-ACCEPTANCE.md",
          "Synthetic contract fixture.\n"
          "docs/digital-employee/acceptance/FINAL-ACCEPTANCE.md\n"
          "docs/DIGITAL-EMPLOYEE-EXECUTION-GUIDE.md\nnot_run_live\n")
    write("eval/loop/BACKLOG.json", json.dumps({"items": [], "deMapping": {"tasks": {
        k: {"status": v} for k, v in states.items() if v != "optional"}}}))
    return root
