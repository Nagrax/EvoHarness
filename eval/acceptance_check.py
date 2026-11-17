#!/usr/bin/env python3
"""DE-25 最终验收可执行判定脚本（FINAL-ACCEPTANCE.md 配套，离线零依赖）。

提供五组可执行判定（回归：tests/test_final_acceptance.py 逐组覆盖）：

1. 状态判定表核对：45 项 DE 工单每项 done/blocked/pending 都有存在的证据引用或
   明确阻塞原因；DE-26/27 标 optional；表与 tasks.json 工单集合一一对应；
   「已完成但无证据」项直接判违规（对照 acceptance 目录与 tasks.json，不硬编码结论）。
2. S2 done 声明守卫：样本不足 / 权限未给 / 等待自然周期 / 外部依赖未满足 /
   负责人未放行 / 无证据 —— 任一存在即禁止记 done（纯函数，可合成负例验证）。
3. 灰度阶梯准入：影子（零写副作用）→ 批准（逐条绑定决定）→ 限定自动（低风险
   集合 + 冻结置信阈值 + 负责人放行 + 配额），三阶段离线可判定；
   另含越权/误发急停判定（停发 + 撤权 + 复盘，保留只读/人工审批）。
4. F01/F08/F09 结构化现状核对：每项给出 状态/阻塞条件/证据位置，并核验证据
   文件存在且含诚实口径标记（"不宣称/残留"）。
5. 声明分离规则：basic_pilot_passed 与 search_enhancement_passed 必须单列；
   搜索未通过（DE-36 非 done）时不得声明搜索收益或发布证书；
   声明基础试点通过 ⇔ F08/F09 现状 done 且 DE-25 done（交叉核对防冒充）。

用法：
  python docs/digital-employee/acceptance/DE-25/acceptance_check.py            # 全部判定
  python docs/digital-employee/acceptance/DE-25/acceptance_check.py --json     # JSON 输出

退出码：0 全部判定通过；1 存在判定违规（绝不静默吞掉）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

DEFAULT_DOC = "docs/digital-employee/acceptance/FINAL-ACCEPTANCE.md"
TASKS_JSON = "docs/digital-employee/tasks.json"

STATUS_DONE = "done"
STATUS_BLOCKED = "blocked"
STATUS_PENDING = "pending"
STATUS_OPTIONAL = "optional"
ALLOWED_STATUS = {STATUS_DONE, STATUS_BLOCKED, STATUS_PENDING, STATUS_OPTIONAL}

# 主指南 §16：45 项 = 43 核心 + DE-26/27 可选；搜索增强线 DE-29～36。
SEARCH_LINE_IDS = tuple(f"DE-{n}" for n in range(29, 37))
F_GATE_IDS = ("F01", "F08", "F09")

PENDING_EXPLAIN_MARKERS = ("未实施", "实施中", "待实施", "待条件", "未开始")

# F 门禁证据文件中的诚实口径标记（存在性核验，防"证据文件被替换成空话"）。
F_GATE_HONESTY_MARKERS = {"F01": "不宣称", "F08": "不得宣称", "F09": "不得宣称"}

# 灰度阶梯（与产品既有语义对齐）：
# - 影子阶段允许的动作 = 观察与草稿/升级记录（DE-19 RecordedSink 语义，零渠道触达）；
# - 限定自动的低风险集合 = feishu AUTO_REPLY_ALLOWED_CATEGORIES（默认 faq）；
# - 自动置信阈值 = DE-19 ORG_FAQ_HOLDOUT["auto_min_confidence"]（0.92，与 F08/F09 golden 不共阈值）。
SHADOW_ALLOWED_ACTIONS = frozenset({"observe", "draft", "escalate_record"})
LOW_RISK_AUTO_CATEGORIES = frozenset({"faq"})
AUTO_MIN_CONFIDENCE = 0.92
# 动作词汇表：阶梯层用 "send"（发送动作）；决定层授权动作 = "send_as_bot"
# （DE-12/DE-19 authorization 口径）。
SEND_ACTION = "send"
AUTHORIZED_SEND_ACTION = "send_as_bot"

REQUIRED_APPROVAL_BINDING = frozenset(
    {"decision_id", "payload_hash", "target_revision", "grantee_id", "action"})

STOP_EVENT_KINDS = frozenset({
    "unauthorized_send",       # 越权发送（未批准/撤权后/白名单外）
    "wrong_content_sent",      # 误发（内容与批准载荷不符/发错对象）
    "severe_error",            # 严重错误（F09 口径）
    "permission_scope_change", # 权限范围变化（撤权/来源撤回/模式变更）
})
REVIEW_EVENT_KINDS = frozenset({"delivery_unknown", "receipt_mismatch", "duplicate_receipt"})


# ── 文档/表格解析 ─────────────────────────────────────────────────────────────

def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _section(text: str, marker: str) -> str:
    """取标题含 marker 的章节正文（到同级或更高级标题为止）。"""
    lines = text.splitlines()
    start = level = None
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("#") and marker in stripped:
            level = len(stripped) - len(stripped.lstrip("#"))
            start = i + 1
            break
    if start is None:
        raise LookupError(f"未找到章节：{marker}")
    body: list[str] = []
    for line in lines[start:]:
        stripped = line.lstrip()
        if stripped.startswith("#"):
            heading_level = len(stripped) - len(stripped.lstrip("#"))
            if heading_level <= level:
                break
        body.append(line)
    return "\n".join(body)


def _table_rows(section_text: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in section_text.splitlines():
        s = line.strip()
        if not (s.startswith("|") and s.endswith("|")):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if cells and all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
            continue  # 分隔行
        rows.append(cells)
    return rows


def _split_evidence(cell: str) -> list[str]:
    """证据列：repo 相对路径，用 `；`/`;` 分隔；反引号包裹可读。"""
    paths: list[str] = []
    for token in re.split(r"[；;]", cell):
        token = token.strip().strip("`").strip()
        if token and ("/" in token or "\\" in token):
            paths.append(token)
    return paths


def load_tasks_index(repo_root: Path) -> dict[str, dict]:
    data = json.loads(_read(repo_root / TASKS_JSON))
    items = data["tasks"] if isinstance(data, dict) else data
    return {t["id"]: t for t in items}


# ── 1) 状态判定表 ─────────────────────────────────────────────────────────────

def validate_status_row(row: list[str], *, task_index: dict, repo_root: Path) -> list[str]:
    if len(row) < 4:
        return [f"状态行需要 4 列（工单/状态/证据/阻塞原因与备注）：{row}"]
    tid, status, evidence, note = row[0], row[1], row[2], row[3]
    if tid not in task_index:
        return [f"未知工单 id：{tid}"]
    task = task_index[tid]
    violations: list[str] = []
    if status not in ALLOWED_STATUS:
        return [f"{tid}: 非法状态 {status!r}（允许 {sorted(ALLOWED_STATUS)}）"]
    optional_in_tasks = bool(task.get("optional"))
    if status == STATUS_OPTIONAL and not optional_in_tasks:
        violations.append(f"{tid}: 标记 optional 但 tasks.json 未标可选")
    if optional_in_tasks and status != STATUS_OPTIONAL:
        violations.append(f"{tid}: tasks.json 可选项但状态表未标 optional")
    paths = _split_evidence(evidence)
    for path in paths:
        if not (repo_root / path).is_file():
            violations.append(f"{tid}: 证据文件不存在 {path}")
    if status == STATUS_DONE and not paths:
        violations.append(f"{tid}: 已完成但无证据引用")
    if status == STATUS_BLOCKED and len(note.strip()) < 8:
        violations.append(f"{tid}: blocked 缺明确阻塞原因")
    if status == STATUS_PENDING and not any(m in note for m in PENDING_EXPLAIN_MARKERS):
        violations.append(f"{tid}: pending 缺未实施/实施中说明")
    return violations


def verify_status_table(repo_root: Path, doc_path: str | None = None) -> dict:
    root = Path(repo_root)
    text = _read(root / (doc_path or DEFAULT_DOC))
    rows = _table_rows(_section(text, "DE 工单状态判定表"))
    task_index = load_tasks_index(root)
    violations: list[str] = []
    seen: set[str] = set()
    statuses: dict[str, str] = {}
    for row in rows:
        if not row or row[0] in {"工单", "----"}:
            continue
        tid = row[0]
        if tid in seen:
            violations.append(f"状态表重复行：{tid}")
            continue
        seen.add(tid)
        statuses[tid] = row[1]
        violations.extend(validate_status_row(row, task_index=task_index, repo_root=root))
    missing = sorted(set(task_index) - seen)
    if missing:
        violations.append(f"状态表缺工单：{missing}")
    extra = sorted(seen - set(task_index))
    if extra:
        violations.append(f"状态表多出未知工单：{extra}")
    return {
        "task_total": len(task_index),
        "row_count": len(seen),
        "statuses": statuses,
        "violations": violations,
    }


# ── 2) S2 done 声明守卫 ───────────────────────────────────────────────────────

S2_FORBIDDEN_REASONS = {
    "done_without_evidence": "已宣称 done 但无存在证据",
    "external_dependency_unmet": "外部依赖未满足",
    "permission_not_granted": "真实权限未授予",
    "owner_release_missing": "负责人未显式放行",
    "sample_insufficient": "独立业务样本不足",
    "natural_cycle_pending": "自然周期未完成",
}


def validate_status_claim(claim: dict) -> tuple[bool, list[str]]:
    """S2 守卫：样本不足/权限未给/等待自然周期不得记 done。

    只有 claimed_status == "done" 受守卫；blocked/pending 恒允许（诚实记录）。
    claim 字段：
      claimed_status / evidence_paths / evidence_exists
      external_deps_unmet（未满足的外部依赖列表）
      required_permissions / permissions_granted
      owner_release_required / owner_release_recorded
      sample_min / sample_count（独立业务样本）
      natural_cycle_required / natural_cycle_met
    """
    claimed = (claim.get("claimed_status") or "").strip()
    if claimed != STATUS_DONE:
        return True, []
    reasons: list[str] = []
    evidence = claim.get("evidence_paths") or []
    if not evidence or not claim.get("evidence_exists", False):
        reasons.append("done_without_evidence")
    if claim.get("external_deps_unmet"):
        reasons.append("external_dependency_unmet")
    if (claim.get("required_permissions") or []) and not claim.get(
            "permissions_granted", False):
        reasons.append("permission_not_granted")
    if claim.get("owner_release_required", False) and not claim.get(
            "owner_release_recorded", False):
        reasons.append("owner_release_missing")
    sample_min = int(claim.get("sample_min", 0) or 0)
    sample_count = claim.get("sample_count")
    if sample_min > 0 and (sample_count is None or int(sample_count) < sample_min):
        reasons.append("sample_insufficient")
    if claim.get("natural_cycle_required", False) and not claim.get(
            "natural_cycle_met", False):
        reasons.append("natural_cycle_pending")
    return (not reasons), reasons


# ── 3) 灰度阶梯与急停 ─────────────────────────────────────────────────────────

def decide_stage_admission(
    stage: str,
    *,
    action: str,
    category: str = "",
    confidence: float | None = None,
    risk_flags: tuple | list = (),
    decision: dict | None = None,
    owner_release: dict | None = None,
    daily_sent: int = 0,
    daily_cap: int | None = None,
) -> tuple[bool, str]:
    """三阶段准入判定（纯函数）。

    - shadow：只允许观察/草稿/升级记录，任何写/发动作拒绝（零业务副作用）。
    - approval：send 必须逐条绑定决定（decision_id/payload_hash/target_revision/
      grantee_id/action），缺任一字段即拒绝。
    - auto：在 approval 基础上再限定低风险集合 + 冻结置信阈值 + 空风险标记 +
      负责人显式放行（含放行记录 ref）+ 已配置且未超的每日配额；任一不满足
      拒绝并回落批准/影子（fail-closed）。
    """
    stage = (stage or "").strip().lower()
    if stage == "shadow":
        if action in SHADOW_ALLOWED_ACTIONS:
            return True, "shadow_read_only"
        return False, "shadow_no_write_side_effect"
    if stage == "approval":
        if action != SEND_ACTION:
            return False, "approval_stage_rejects_non_send"
        if not decision:
            return False, "approval_required_per_item"
        missing = REQUIRED_APPROVAL_BINDING - set(decision)
        if missing:
            return False, f"approval_binding_incomplete:{sorted(missing)}"
        if decision.get("action") != AUTHORIZED_SEND_ACTION:
            return False, "approval_action_mismatch"
        return True, "approved_per_item"
    if stage == "auto":
        if action != SEND_ACTION:
            return False, "auto_stage_rejects_non_send"
        if not decision:
            return False, "approval_required_per_item"  # 自动仍以逐条批准为前提
        missing = REQUIRED_APPROVAL_BINDING - set(decision)
        if missing:
            return False, f"approval_binding_incomplete:{sorted(missing)}"
        if decision.get("action") != AUTHORIZED_SEND_ACTION:
            return False, "approval_action_mismatch"
        if category not in LOW_RISK_AUTO_CATEGORIES:
            return False, "category_not_in_low_risk_set"
        if risk_flags:
            return False, "risk_flags_present"
        if confidence is None or float(confidence) < AUTO_MIN_CONFIDENCE:
            return False, "confidence_below_frozen_threshold"
        if not owner_release or not owner_release.get("released") or not owner_release.get("ref"):
            return False, "owner_release_missing"
        if daily_cap is None or int(daily_cap) <= 0:
            return False, "auto_cap_unconfigured"
        if int(daily_sent) >= int(daily_cap):
            return False, "daily_cap_exceeded"
        return True, "auto_allowed_within_low_risk_set"
    return False, "unknown_stage"


def emergency_stop_decision(event: dict) -> dict:
    """越权/误发急停判定：任一停止类事件 → 停发 + 撤权 + 保留证据 + 复盘。

    回退口径 = tasks.json DE-25 rollback："急停自动发信与新执行，保留只读/
    人工审批"，即回退到 single_approval（批准阶段）而非全停（只读继续）。
    非停止类但对账异常事件 → 只标记复盘不停发；普通事件不触发。
    """
    kind = (event.get("kind") or "").strip()
    if kind in STOP_EVENT_KINDS:
        return {
            "stop_sending": True,
            "revoke_decisions": True,
            "preserve_evidence": True,
            "review_required": True,
            "resume_mode": "single_approval",
        }
    if kind in REVIEW_EVENT_KINDS:
        return {
            "stop_sending": False,
            "revoke_decisions": False,
            "preserve_evidence": True,
            "review_required": True,
            "resume_mode": "",
        }
    return {
        "stop_sending": False,
        "revoke_decisions": False,
        "preserve_evidence": True,
        "review_required": False,
        "resume_mode": "",
    }


# ── 4) F 门禁结构化现状 ───────────────────────────────────────────────────────

def f_gate_status(repo_root: Path, doc_path: str | None = None) -> dict:
    """F01/F08/F09 每项：状态/阻塞条件/证据位置（核验证据存在与诚实标记）。"""
    root = Path(repo_root)
    text = _read(root / (doc_path or DEFAULT_DOC))
    rows = _table_rows(_section(text, "原 F 门禁现状核对"))
    violations: list[str] = []
    seen: dict[str, dict] = {}
    for row in rows:
        if not row or row[0] in {"门禁"}:
            continue
        if len(row) < 4:
            violations.append(f"F 行需要 4 列（门禁/状态/阻塞条件/证据位置）：{row}")
            continue
        gid = row[0]
        status_cell = row[1]
        seen[gid] = {
            "id": gid,
            "status": status_cell.split("（")[0].split("(")[0].strip(),
            "status_detail": status_cell,
            "blocked_on": row[2],
            "evidence": _split_evidence(row[3]),
        }
    gates: list[dict] = []
    for gid in F_GATE_IDS:
        if gid not in seen:
            violations.append(f"缺 {gid} 现状行")
            continue
        entry = seen[gid]
        if entry["status"] not in {"done", "partial", "blocked", "pending"}:
            violations.append(f"{gid}: 非法状态 {entry['status']!r}")
        if not entry["evidence"]:
            violations.append(f"{gid}: 无证据位置")
        for path in entry["evidence"]:
            if not (root / path).is_file():
                violations.append(f"{gid}: 证据文件不存在 {path}")
        if entry["status"] != "done":
            if len(entry["blocked_on"].strip()) < 8:
                violations.append(f"{gid}: 非完成状态但阻塞条件缺失")
            marker = F_GATE_HONESTY_MARKERS.get(gid)
            if marker and not any(
                    marker in _read(root / path) for path in entry["evidence"]
                    if (root / path).is_file()):
                violations.append(f"{gid}: 证据文件缺诚实口径标记（{marker}）")
        gates.append(entry)
    return {"gates": gates, "violations": violations}


# ── 5) 声明分离规则 ───────────────────────────────────────────────────────────

REQUIRED_CLAIM_KEYS = (
    "basic_pilot_passed",
    "search_enhancement_passed",
    "search_benefit_claimed",
    "release_certificate_obtained",
    "basic_pilot_equals_search",
)


def verify_conclusion_claims(repo_root: Path, doc_path: str | None = None) -> dict:
    """基础试点/搜索增强声明必须单列，且与状态表交叉一致。

    规则（不硬编码结论，结论取自文档声明表）：
    - required keys 全部存在且取值 true/false；
    - search_enhancement_passed=false → 不得声明搜索收益/发布证书；
    - search_enhancement_passed=true ⇔ 状态表 DE-36=done；
    - basic_pilot_passed=true ⇔ F08/F09 现状 done 且状态表 DE-25=done；
    - basic_pilot_equals_search 必须为 false（两条线不得合并声明）。
    """
    root = Path(repo_root)
    doc = root / (doc_path or DEFAULT_DOC)
    text = _read(doc)
    rows = _table_rows(_section(text, "试点通过与收益声明判定表"))
    values: dict[str, bool] = {}
    violations: list[str] = []
    for row in rows:
        if not row or row[0] in {"声明键"}:
            continue
        if len(row) < 2:
            violations.append(f"声明行需要 ≥2 列：{row}")
            continue
        key, value = row[0], row[1].strip().lower()
        if value not in {"true", "false"}:
            violations.append(f"{key}: 声明值必须是 true/false，得到 {row[1]!r}")
            continue
        values[key] = value == "true"
    for key in REQUIRED_CLAIM_KEYS:
        if key not in values:
            violations.append(f"声明表缺键：{key}")
    search_passed = values.get("search_enhancement_passed")
    if search_passed is False:
        if values.get("search_benefit_claimed"):
            violations.append("搜索未通过却声明搜索收益")
        if values.get("release_certificate_obtained"):
            violations.append("搜索未通过却声明已取得发布证书")
    if search_passed is True:
        status_table = verify_status_table(root, doc_path=doc_path)
        if status_table["statuses"].get("DE-36") != STATUS_DONE:
            violations.append("声明搜索增强通过但状态表 DE-36 非 done")
    if values.get("basic_pilot_passed") is True:
        status_table = verify_status_table(root, doc_path=doc_path)
        fg = f_gate_status(root, doc_path=doc_path)
        for gate in fg["gates"]:
            if gate["id"] in {"F08", "F09"} and gate["status"] != "done":
                violations.append(
                    f"声明基础试点通过但 {gate['id']} 现状非 done（{gate['status']}）")
        if status_table["statuses"].get("DE-25") != STATUS_DONE:
            violations.append("声明基础试点通过但状态表 DE-25 非 done")
    if values.get("basic_pilot_equals_search"):
        violations.append("basic_pilot_equals_search 必须为 false（两条线分别验收）")
    return {"claims": values, "violations": violations}


def verify_search_line(repo_root: Path, doc_path: str | None = None) -> dict:
    """搜索增强线 DE-29～36 必须单独成节且逐项有状态/证据（不并入基础线）。"""
    root = Path(repo_root)
    text = _read(root / (doc_path or DEFAULT_DOC))
    section = _section(text, "搜索增强线（DE-29～36）真实状态")
    rows = _table_rows(section)
    task_index = load_tasks_index(root)
    violations: list[str] = []
    seen: set[str] = set()
    statuses: dict[str, str] = {}
    for row in rows:
        if not row or row[0] in {"工单"}:
            continue
        tid = row[0]
        if tid in seen:
            violations.append(f"搜索线重复行：{tid}")
            continue
        seen.add(tid)
        if len(row) >= 2:
            statuses[tid] = row[1]
        violations.extend(
            validate_status_row(row, task_index=task_index, repo_root=root))
    missing = sorted(set(SEARCH_LINE_IDS) - seen)
    if missing:
        violations.append(f"搜索增强线缺工单行：{missing}")
    extra = sorted(seen - set(SEARCH_LINE_IDS))
    if extra:
        violations.append(f"搜索增强线混入非搜索工单：{extra}")
    return {"statuses": statuses, "violations": violations}


# ── 主流程 ────────────────────────────────────────────────────────────────────

def run_all_checks(repo_root: Path, doc_path: str | None = None) -> dict:
    root = Path(repo_root)
    doc = root / (doc_path or DEFAULT_DOC)
    report: dict = {"doc": str(doc), "checks": {}}
    fatal: list[str] = []
    if not doc.is_file():
        report["fatal"] = [f"验收文档不存在：{doc}"]
        report["ok"] = False
        return report

    def run(name: str, fn) -> None:
        try:
            report["checks"][name] = fn(root, doc_path=doc_path)
        except LookupError as exc:
            fatal.append(f"{name}: {exc}")

    run("status_table", verify_status_table)
    run("f_gate_status", f_gate_status)
    run("conclusion_claims", verify_conclusion_claims)
    run("search_line", verify_search_line)
    # 演练与灰度章节存在性（可执行判定在 tests/test_final_acceptance.py 组合真实模块）
    text = _read(doc)
    for marker in ("灰度阶梯定义", "停止/撤权/更正演练", "S2 状态判定规则",
                   "剩余外部依赖与可选工作"):
        if marker not in text:
            fatal.append(f"缺章节：{marker}")
    report["fatal"] = fatal
    violations = {
        name: entry["violations"]
        for name, entry in report["checks"].items()
        if entry.get("violations")
    }
    report["violations"] = violations
    report["ok"] = not fatal and not violations
    return report


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="DE-25 最终验收可执行判定")
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]),
                        help="仓库根（默认按脚本位置推导）")
    parser.add_argument("--doc", default=None, help="FINAL-ACCEPTANCE.md 路径（相对 repo 根）")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args(argv)
    report = run_all_checks(Path(args.repo).resolve(), doc_path=args.doc)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"验收文档：{report['doc']}")
        status = verify_status_table(Path(args.repo).resolve(), doc_path=args.doc)
        print(f"状态表：{status['row_count']}/{status['task_total']} 项；"
              f"违规 {len(status['violations'])}")
        fg = f_gate_status(Path(args.repo).resolve(), doc_path=args.doc)
        for gate in fg["gates"]:
            print(f"F 门禁 {gate['id']}: {gate['status_detail']}")
        claims = verify_conclusion_claims(Path(args.repo).resolve(), doc_path=args.doc)
        print("声明：" + json.dumps(claims["claims"], ensure_ascii=False, sort_keys=True))
        for name, entries in report["violations"].items():
            for item in entries:
                print(f"[违规] {name}: {item}")
        for item in report["fatal"]:
            print(f"[缺失] {item}")
        print(f"结论：{'判定通过' if report['ok'] else '存在判定违规'}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
