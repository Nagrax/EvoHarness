"""M4 compare_ir：经验标准 IR 的一致性与回归对比 CLI（三模式互斥必选）。

- --ir-only        对当前仓库全部 active skill 做 legacy→IR 固定枚举适配（R0-R8），
                   用 M1 validate_record 打分并报告 legacy_ir_error_count。
- --rules-parity   现行 _compile_eval_rules 输出 vs RULES_BASELINE（pre-M3 冻结基线，
                   HEAD=5c6ad55 实测采集：11 skill 共 14 条规则）对比。skill 有意新增
                   `## Decision Rules` 区导致派生规则出现时，用本模式打印的现行规则
                   JSON 有意更新 RULES_BASELINE 常量。
- --status-parity  在 tempfile 沙箱里整轮重跑评测核心（python -m agents.online_skill_eval），
                   对比 status_map + report aggregate 的 backward stability；写盘全部落在
                   沙箱（唯一例外：--save-baseline 写用户显式路径）。不进默认 pytest。

status-parity 验收流程：
  cp -r .evoharness <冻结副本>                                        # 改动前冻结数据侧
  python eval/experience_eval/compare_ir.py --status-parity --save-baseline before.json
  ... 合入 M2/M3 ...
  还原冻结副本（保证数据侧不变）
  python eval/experience_eval/compare_ir.py --status-parity --baseline before.json   # 期望 exit 0

legacy_ir_error_count 语义（legacy_ir_error_count() 是唯一权威出处）：
  对每个 (skill, record, raw_text) 调 M1 validate_record(record, raw_text)，
  error code 总数（任何 code 都计，无白名单）；records=None 时对 discover_skills()
  全集现算。适配修复用固定枚举 R0-R8（applied_repairs 是唯一事实源，禁止按校验
  结果反推增删）；R0-R8 与 M1 repair_record 的七码是两套并行词表、不同 provenance
  上下文（M2 的 ir_repairs 用 M1 码，本工具只打印 R0-R8），互不混用。

只读边界（如实声明）：本工具自身代码对仓库树零写入；但经 discover_skills() 触发的
harness 加载器自身可能条件性追加 .evoharness/skill-evolution/skill_load_errors.jsonl
（skills.py:121-148 / :190-199 → skill_evolution.py:43-63），CLI 对 discover 前后
该文件的行数差显性打印。--ir-only / --rules-parity 需在仓库根运行（skills 根 =
Path.cwd()/.evoharness/skills，user 级 = Path.home()/.evoharness/skills）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
if __package__ in (None, ""):
    sys.path.insert(0, str(REPO_ROOT))

# M1 契约钉死值（M4 §1.3）：M1 落地时 SCHEMA_VERSION 必须等于该字面量，漂移即配置错误。
IR_SCHEMA_VERSION = "experience-standard/v1"
STATUS_PARITY_SCHEMA = "evoharness-status-parity/1"
LEGACY_PRIMARY_TYPE = "heuristic"
LEGACY_ITEM_BASIS = "inferred"
LEGACY_ITEM_CONFIDENCE = 0.5
ADAPTATION_REPAIR_IDS = ("R0", "R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8")

# R6 的不变量是映射规则「R6 ⟺ when_to_use 非空」；命中集随 skill frontmatter 合法演变，
# 具体名单不进断言（tests/test_experience_eval_toolkit.py 断言规则本身）。
ADAPTATION_REPAIR_TABLE: dict[str, str] = {
    "R0": "补全 experience-standard/v1 十字段骨架（全部 section 与子字段键置空列表/默认值；evidence.confidence=0.0；identity.secondary_types=[]）",
    "R1": "schema_version ← SCHEMA_VERSION",
    "R2": "source.raw_text ← skill 正文全文（prompt_template）；validate_record 第二参传同一文本",
    "R3": "identity.title ← name",
    "R4": "identity.summary ← description（zhangxuefeng-perspective 的 `description: |` 块标量塌缩为 '|'，仍非空、放行）",
    "R5": "identity.primary_type ← heuristic",
    "R6": "when_to_use 非空时 → situation.triggers 单条 {text, basis=inferred, confidence=0.5}",
    "R7": "knowledge_gaps ← []",
    "R8": "indexing.tags ← frontmatter tags 逗号拆分的非空字符串列表",
}


def _skill_text_rule(rule_id: str, label: str, hard: bool, mode: str) -> dict[str, Any]:
    return {
        "rule_id": rule_id,
        "label": label,
        "kind": "programmatic",
        "hard": hard,
        "params": {"mode": mode},
        "provenance": {"source": "skill_text"},
    }


def _baseline_rule() -> dict[str, Any]:
    return {
        "rule_id": "response_nonempty",
        "label": "Non-empty response",
        "kind": "programmatic",
        "hard": True,
        "params": {"mode": "nonempty"},
        "provenance": {"source": "baseline"},
    }


# 仅基线规则的 9 个 skill（pre-M3 HEAD=5c6ad55 实测；与本会话 _compile_eval_rules
# dump 逐 skill 一致）。快照无 tags 键 → corpus 的 tags 段为空（online_skill_eval.py:506-514）。
_BASELINE_ONLY_SKILLS = (
    "code_review",
    "create-readme",
    "frontend-design",
    "impeccable",
    "using-superpowers",
    "vercel-react-best-practices",
    "web-design-guidelines",
    "webnovel-writing",
    "政府报告撰写-正式书面化与政策结合",
)

# 冻结 pre-M3 基线：采集约定 = _active_skill_snapshots() + _compile_eval_rules(snapshot,
# include_llm_rules=False)（即 `python -m agents.online_skill_eval` 的 __main__ 路径）。
RULES_BASELINE: dict[str, list[dict[str, Any]]] = {
    **{name: [_baseline_rule()] for name in _BASELINE_ONLY_SKILLS},
    "ui-ux-pro-max": [
        _baseline_rule(),
        _skill_text_rule("json_parseable", "Valid JSON output", True, "json_parseable"),
    ],
    "zhangxuefeng-perspective": [
        _baseline_rule(),
        _skill_text_rule("lead_with_conclusion", "Lead with conclusion", False, "lead_with_conclusion"),
        _skill_text_rule("markdown_table", "Markdown table present", False, "markdown_table"),
    ],
}


def load_golden_records() -> list[tuple[str, dict[str, Any]]]:
    """读 golden/*.json（utf-8），返回 (文件名, record) 列表，按文件名排序。"""
    records: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(GOLDEN_DIR.glob("*.json")):
        records.append((path.name, json.loads(path.read_text(encoding="utf-8"))))
    return records


def _empty_legacy_skeleton() -> dict[str, Any]:
    """R0 骨架：experience-standard/v1 的 empty_record 默认形态（十顶层键 + 全部子字段键）。"""
    return {
        "schema_version": "",
        "source": {"raw_text": "", "language": "", "source_type": ""},
        "identity": {"title": "", "summary": "", "primary_type": "", "secondary_types": []},
        "situation": {"goal": [], "triggers": [], "context": [], "entities": []},
        "guidance": {"principles": [], "decision_rules": [], "steps": [], "avoid": []},
        "applicability": {"conditions": [], "prerequisites": [], "exceptions": [], "boundaries": []},
        "outcome": {"expected": [], "success_signals": [], "failure_signals": []},
        "evidence": {"observations": [], "counterexamples": [], "validation_methods": [], "confidence": 0.0},
        "indexing": {"domains": [], "tasks": [], "tags": [], "aliases": [], "cross_domain_links": []},
        "knowledge_gaps": [],
    }


def _split_tags(raw: Any) -> list[str]:
    # frontmatter tags 是裸字符串（逗号分隔）；中英文逗号都拆，镜像 online_skill_evolution
    # _coerce_candidate 对 tags 字符串的处理。tags 是 B 校验里唯一允许裸字符串的列表。
    if not isinstance(raw, str):
        return []
    return [part.strip() for part in raw.replace("，", ",").split(",") if part.strip()]


def _skill_frontmatter_tags(skill_dir: str) -> list[str]:
    """SkillDefinition 不携带 tags（skills.py:30-41），R8 的 tags 从 SKILL.md frontmatter 直接读取。"""
    path = Path(skill_dir) / "SKILL.md" if skill_dir else None
    if not path or not path.is_file():
        return []
    try:
        from agents.frontmatter import parse_frontmatter

        meta = parse_frontmatter(path.read_text(encoding="utf-8-sig")).meta
    except Exception:
        return []
    return _split_tags(meta.get("tags", ""))


def apply_legacy_adaptation_repairs(
    *,
    name: str,
    description: str,
    when_to_use: str,
    body: str,
    tags: list[str],
) -> tuple[dict[str, Any], list[str]]:
    """把 legacy skill（name/description/when_to_use/正文/tags）映射成 experience-standard/v1 记录。

    返回 (record, applied_repairs)。applied_repairs 是唯一事实源：固定枚举 R0-R8、
    按先验映射顺序追加，禁止按 validate 结果反推增删。
    """
    record = _empty_legacy_skeleton()
    repairs = ["R0"]
    record["schema_version"] = IR_SCHEMA_VERSION
    repairs.append("R1")
    record["source"]["raw_text"] = str(body or "")
    repairs.append("R2")
    record["identity"]["title"] = str(name or "").strip()
    repairs.append("R3")
    record["identity"]["summary"] = str(description or "")
    repairs.append("R4")
    record["identity"]["primary_type"] = LEGACY_PRIMARY_TYPE
    repairs.append("R5")
    if str(when_to_use or "").strip():
        record["situation"]["triggers"] = [
            {"text": str(when_to_use), "basis": LEGACY_ITEM_BASIS, "confidence": LEGACY_ITEM_CONFIDENCE}
        ]
        repairs.append("R6")
    record["knowledge_gaps"] = []
    repairs.append("R7")
    clean_tags = [str(tag).strip() for tag in (tags or []) if isinstance(tag, str) and str(tag).strip()]
    record["indexing"]["tags"] = clean_tags
    repairs.append("R8")
    return record, repairs


def _require_experience_standard() -> Any:
    """惰性导入 M1；缺失或 SCHEMA_VERSION 漂移 → RuntimeError（CLI exit 2 / importorskip）。"""
    import importlib

    try:
        experience_standard = importlib.import_module("agents.experience_standard")
    except Exception as ex:
        raise RuntimeError(f"M1 agents.experience_standard 未就绪: {type(ex).__name__}: {ex}") from ex
    schema_version = getattr(experience_standard, "SCHEMA_VERSION", "")
    if schema_version != IR_SCHEMA_VERSION:
        raise RuntimeError(f"M1 SCHEMA_VERSION 漂移: {schema_version!r} != {IR_SCHEMA_VERSION!r}")
    return experience_standard


def build_skill_ir_inputs(skills: list[Any] | None = None) -> list[dict[str, Any]]:
    """对每个发现/给定的 skill 构造 legacy IR 输入。

    返回 {"name", "record", "raw_text", "repairs", "when_to_use"} 列表，构造纯函数级确定
    （连续两次逐字节一致）。M1 缺失时抛 RuntimeError。
    """
    _require_experience_standard()
    if skills is None:
        from agents.skills import discover_skills

        skills = discover_skills()
    inputs: list[dict[str, Any]] = []
    for skill in skills:
        name = str(getattr(skill, "name", "") or "")
        when_to_use = str(getattr(skill, "when_to_use", "") or "")
        record, repairs = apply_legacy_adaptation_repairs(
            name=name,
            description=str(getattr(skill, "description", "") or ""),
            when_to_use=when_to_use,
            body=str(getattr(skill, "prompt_template", "") or ""),
            tags=_skill_frontmatter_tags(str(getattr(skill, "skill_dir", "") or "")),
        )
        inputs.append(
            {
                "name": name,
                "record": record,
                "raw_text": record["source"]["raw_text"],
                "repairs": repairs,
                "when_to_use": when_to_use,
            }
        )
    return inputs


def legacy_ir_error_count(records: list[tuple[str, dict[str, Any], str]] | None = None) -> int:
    """唯一权威出处：对每个 (skill, record, raw_text) 调 M1 validate_record(record, raw_text)。

    error code 总数（任何 code 都计，无白名单）；records=None 时对 discover_skills() 全集现算。
    """
    experience_standard = _require_experience_standard()
    if records is None:
        records = [(e["name"], e["record"], e["raw_text"]) for e in build_skill_ir_inputs()]
    total = 0
    for _name, record, raw_text in records:
        total += len(experience_standard.validate_record(record, raw_text))
    return total


def _skill_load_errors_path() -> Path:
    from agents.skill_evolution import SKILL_LOAD_ERRORS_LOG, get_evolution_dir

    return get_evolution_dir() / SKILL_LOAD_ERRORS_LOG


def _count_load_error_lines(path: Path) -> int:
    if not path.is_file():
        return 0
    return len(path.read_text(encoding="utf-8", errors="replace").splitlines())


def cmd_ir_only() -> int:
    """--ir-only：0=总错误 0；1=有错误；2=配置错误（M1 缺失/无 skill）。"""
    try:
        _require_experience_standard()
    except RuntimeError as ex:
        print(f"CONFIG_ERROR: {ex}")
        return 2
    errors_path = _skill_load_errors_path()
    lines_before = _count_load_error_lines(errors_path)
    try:
        inputs = build_skill_ir_inputs()
    except RuntimeError as ex:
        print(f"CONFIG_ERROR: {ex}")
        return 2
    lines_after = _count_load_error_lines(errors_path)
    print(
        "loader_writes: skill_load_errors.jsonl "
        f"+{lines_after - lines_before} 行（harness 加载器自身行为，触发条件见 skills.py:121-148/:190-199）"
    )
    if not inputs:
        print("CONFIG_ERROR: 未发现任何 skill（需在仓库根运行，skills 根=<cwd>/.evoharness/skills）")
        return 2
    experience_standard = _require_experience_standard()
    total = 0
    for entry in inputs:
        errors = experience_standard.validate_record(entry["record"], entry["raw_text"])
        total += len(errors)
        print(f"- {entry['name']}: applied_repairs={entry['repairs']} ir_errors={len(errors)}")
        for error in errors:
            print(f"  {error}")
    print(f"skills={len(inputs)} legacy_ir_error_count={total}")
    return 0 if total == 0 else 1


def compute_current_eval_rules() -> dict[str, list[dict[str, Any]]]:
    """按评测核心同款约定采集现行规则：_active_skill_snapshots() + include_llm_rules=False。"""
    from agents.online_skill_eval import _active_skill_snapshots, _compile_eval_rules
    from agents.skills import reset_skill_cache

    reset_skill_cache()
    snapshots = _active_skill_snapshots()
    return {
        name: _compile_eval_rules(snapshot, include_llm_rules=False)
        for name, snapshot in sorted(snapshots.items())
    }


def diff_eval_rules(baseline: dict[str, list[dict[str, Any]]], current: dict[str, list[dict[str, Any]]]) -> list[str]:
    """逐 skill JSON 规范化（sort_keys）对比，返回人读 diff 行；交集外的 skill 忽略
    （CI 只检出 tracked 的 4 个 skill，取基线∩现行交集口径）。"""
    diffs: list[str] = []
    for name in sorted(set(baseline) & set(current)):
        baseline_json = json.dumps(baseline[name], ensure_ascii=False, sort_keys=True)
        current_json = json.dumps(current[name], ensure_ascii=False, sort_keys=True)
        if baseline_json == current_json:
            continue
        baseline_ids = [r.get("rule_id") for r in baseline[name] if isinstance(r, dict)]
        current_ids = [r.get("rule_id") for r in current[name] if isinstance(r, dict)]
        added = [rid for rid in current_ids if rid not in baseline_ids]
        removed = [rid for rid in baseline_ids if rid not in current_ids]
        detail = []
        if added:
            detail.append(f"added={added}")
        if removed:
            detail.append(f"removed={removed}")
        if not detail:
            detail.append("rule params changed in place")
        diffs.append(f"{name}: {'; '.join(detail)}")
        diffs.append(f"  baseline: {baseline_json}")
        diffs.append(f"  current : {current_json}")
    return diffs


def cmd_rules_parity() -> int:
    """--rules-parity：0=diff 空；1=有 diff；2=配置错误。stdout 总是打印现行规则 JSON 供更新基线。"""
    try:
        current = compute_current_eval_rules()
    except Exception as ex:
        print(f"CONFIG_ERROR: {type(ex).__name__}: {ex}")
        return 2
    if not current:
        print("CONFIG_ERROR: 未发现任何 skill（需在仓库根运行）")
        return 2
    print("current_eval_rules = " + json.dumps(current, ensure_ascii=False, sort_keys=True, indent=2))
    diffs = diff_eval_rules(RULES_BASELINE, current)
    if diffs:
        print(f"RULES_PARITY: DIFF（{len(diffs)} 行）")
        for line in diffs:
            print(line)
        print("若为有意变更，请用上方 current_eval_rules JSON 更新 RULES_BASELINE 常量。")
        return 1
    print("RULES_PARITY: MATCH (diff=0)")
    return 0


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


_STATUS_AGGREGATE_DICT_KEYS = ("statuses", "champion_statuses", "actions")
# report.aggregate 的全部统计键（online_skill_eval.py:1930-1949）；generated_at/run_id/
# artifacts 路径等时变字段不进对比范围。
_STATUS_AGGREGATE_KEYS = (
    "statuses",
    "champion_statuses",
    "replay_samples",
    "rule_outcomes",
    "rule_pass_rate",
    "skills",
    "online_ingests",
    "ok",
    "candidate_events",
    "accepted_events",
    "actions",
    "candidate_variants",
    "llm_rules",
    "llm_rule_outcomes",
    "llm_rule_pass_rate",
    "ok_rate",
    "acceptance_rate",
)


def _status_parity_observation(evolution_dir: Path) -> dict[str, Any]:
    """从一次评测运行的两个产物组装同表对比口径（M4 §4.5）。

    两个产物的落盘位置不同（实测）：status_map 在 get_evolution_dir()/online-eval/
    （online_skill_eval.py:2031），report 在 get_evolution_dir()/ 本级
    （:2115，report_path = root / "online_eval_report.json"）。

    注意 status_map.statuses（无答卷 skill 走 estimate_status_from_usage 估算）与
    report aggregate.statuses（状态机原始判定）是两个口径，各自同表对比、不可交叉比。
    """
    status_map = json.loads((evolution_dir / "online-eval" / "status_map.json").read_text(encoding="utf-8"))
    report = json.loads((evolution_dir / "online_eval_report.json").read_text(encoding="utf-8"))
    sm = status_map if isinstance(status_map, dict) else {}
    aggregate = report.get("aggregate") if isinstance(report.get("aggregate"), dict) else {}
    return {
        "status_map": {
            "statuses": sm.get("statuses", {}),
            "certified_statuses": sm.get("certified_statuses", {}),
        },
        "report_aggregate": {
            key: aggregate.get(key, {} if key in _STATUS_AGGREGATE_DICT_KEYS else 0)
            for key in _STATUS_AGGREGATE_KEYS
        },
    }


def _diff_status_observation(saved: dict[str, Any], current: dict[str, Any]) -> list[str]:
    diffs: list[str] = []
    for section in ("status_map", "report_aggregate"):
        old = saved.get(section) if isinstance(saved.get(section), dict) else {}
        new = current.get(section) if isinstance(current.get(section), dict) else {}
        for key in sorted(set(old) | set(new)):
            old_json = json.dumps(old.get(key), ensure_ascii=False, sort_keys=True)
            new_json = json.dumps(new.get(key), ensure_ascii=False, sort_keys=True)
            if old_json != new_json:
                diffs.append(f"{section}.{key}: baseline={old_json} current={new_json}")
    return diffs


def _home_skills_listing() -> list[str]:
    """F3 home 守卫：Path.home()/.evoharness/skills 的相对路径排序快照。"""
    home = Path.home() / ".evoharness" / "skills"
    if not home.is_dir():
        return []
    return sorted(
        str(p.relative_to(home)).replace("\\", "/") + ("/" if p.is_dir() else "")
        for p in home.rglob("*")
    )


_STATUS_PARITY_TIMEOUT_SECONDS = 600


def cmd_status_parity(*, baseline: Path | None, save_baseline: Path | None, keep_sandbox: bool) -> int:
    """--status-parity：0=一致/已存基线；1=有 diff（或 home 守卫触发）；2=配置错误。

    唯一写盘点全部在 tempfile 沙箱（--save-baseline 除外）；F3 双守卫阻断沙箱内的
    归档/清理路径外溢到真实用户级 skills。
    """
    eval_root = Path.cwd() / ".evoharness" / "skill-evolution" / "online-eval"
    if not (eval_root / "status_map.json").is_file():
        print(f"CONFIG_ERROR: 未找到 {eval_root / 'status_map.json'}（--status-parity 依赖真仓评测数据）")
        return 2
    sandbox = Path(tempfile.mkdtemp(prefix="evoharness-status-parity-"))
    try:
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
        shutil.copytree(REPO_ROOT / "agents", sandbox / "agents", ignore=ignore)
        shutil.copytree(Path.cwd() / ".evoharness", sandbox / ".evoharness", ignore=ignore)
        home_before = _home_skills_listing()
        env = {
            **os.environ,
            # F3 归档守卫：:569 时间短路恒 False → never-retrieved 归档全关（含 user 源——
            # EVOHARNESS_SKILL_PRUNE_PROJECT 只挡 project 源）；usage-prune 路径纵深防御。
            "EVOHARNESS_SKILL_ZOMBIE_DAYS": "999999",
            "EVOHARNESS_SKILL_USAGE_PRUNE_MIN_RETRIEVED": "999999",
        }
        proc = subprocess.run(
            [sys.executable, "-m", "agents.online_skill_eval"],
            cwd=str(sandbox),
            env=env,
            capture_output=True,
            text=True,
            timeout=_STATUS_PARITY_TIMEOUT_SECONDS,
        )
        home_after = _home_skills_listing()
        if home_after != home_before:
            moved = sorted(set(home_after) ^ set(home_before))
            print("HOME_GUARD: Path.home()/.evoharness/skills 在子进程前后发生变化：")
            for item in moved:
                print(f"  {item}")
            print("F3 守卫触发：user 级 skill 目录疑似被评测核心移动，检查 env 注入是否失效。")
            return 1
        if proc.returncode != 0:
            print(f"CONFIG_ERROR: 沙箱内 python -m agents.online_skill_eval 退出码 {proc.returncode}")
            for line in (proc.stderr or "").strip().splitlines()[-10:]:
                print(f"  {line}")
            return 2
        observation = _status_parity_observation(sandbox / ".evoharness" / "skill-evolution")
        if save_baseline is not None:
            payload = {"schema": STATUS_PARITY_SCHEMA, "saved_at": _utc_now(), **observation}
            save_baseline.parent.mkdir(parents=True, exist_ok=True)
            save_baseline.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
                newline="",
            )
            print(f"status parity baseline 已保存: {save_baseline}")
            return 0
        if baseline is not None:
            saved = json.loads(Path(baseline).read_text(encoding="utf-8"))
        else:
            print(
                "note: 未指定 --baseline，对照真仓磁盘上的上次运行持久化态；"
                "使用数据增长导致的 diff 是数据驱动而非回归。"
            )
            saved = _status_parity_observation(Path.cwd() / ".evoharness" / "skill-evolution")
        diffs = _diff_status_observation(saved, observation)
        if diffs:
            print(f"STATUS_PARITY: DIFF（{len(diffs)} 处）")
            for line in diffs:
                print(f"  {line}")
            return 1
        print("STATUS_PARITY: MATCH (diff=0)")
        return 0
    finally:
        if keep_sandbox:
            print(f"sandbox kept: {sandbox}")
        else:
            shutil.rmtree(sandbox, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="compare_ir",
        description="M4 评测工具包：legacy_ir_error_count / 规则基线 parity / status parity",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ir-only", action="store_true", help="对全部 active skill 做 legacy→IR 适配并打分")
    group.add_argument("--rules-parity", action="store_true", help="现行评测规则 vs RULES_BASELINE 冻结基线")
    group.add_argument("--status-parity", action="store_true", help="沙箱整轮重跑评测核心对比 backward stability")
    parser.add_argument("--baseline", type=Path, default=None, help="（--status-parity）对照基线文件")
    parser.add_argument("--save-baseline", type=Path, default=None, help="（--status-parity）保存基线到显式路径")
    parser.add_argument("--keep-sandbox", action="store_true", help="（--status-parity）保留沙箱目录供排查")
    args = parser.parse_args(argv)
    if args.status_parity:
        return cmd_status_parity(
            baseline=args.baseline,
            save_baseline=args.save_baseline,
            keep_sandbox=args.keep_sandbox,
        )
    if args.baseline or args.save_baseline or args.keep_sandbox:
        parser.error("--baseline/--save-baseline/--keep-sandbox 仅用于 --status-parity")
        return 2
    if args.ir_only:
        return cmd_ir_only()
    return cmd_rules_parity()


if __name__ == "__main__":
    raise SystemExit(main())
