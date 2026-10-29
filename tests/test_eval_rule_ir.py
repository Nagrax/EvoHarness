"""评测规则编译升级回归测试：Decision Rules 区派生规则 + keyword_hit 判定 + async 入口 NameError 修复。

覆盖 agents/online_skill_eval.py 的三件事：
- _parse_decision_rules_section：'## Decision Rules' 区 → when/then/because 块列表
  （区格式契约 = M2 写出规范形态：bullet 可选、then/because 缩进在块内）
- _derived_rules_from_instructions + _derive_keywords：块 → keyword_hit 程序规则
  （hard=False 永不进 champion 硬门；提不出关键词的块跳过，绝不 emit 无法判定的规则）
- evaluate_online_skill_evolution_async：min_replay_samples → min_score_samples 的 NameError 回归修复

沙箱纪律：凡触 discover_skills 的测试一律 monkeypatch.chdir + patch Path.home
（用户级 ~/.evoharness/skills 是一等加载路径，不 patch 会被真机 home 污染）
+ reset_skill_cache（try/finally，防进程级缓存跨测试泄漏）。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from agents.online_skill_eval import (
    MAX_DERIVED_RULES,
    _active_skill_snapshots,
    _compile_eval_rules,
    _derived_rules_from_instructions,
    _derive_keywords,
    _evaluate_rule,
    _parse_decision_rules_section,
    _summarize_rule_outcomes,
    evaluate_online_skill_evolution_async,
)
from agents.skills import reset_skill_cache


FIXTURE_INSTRUCTIONS = """## Decision Rules

- When: 用户要求推荐书籍时
  Then: 每个推荐都必须附上推荐理由
  Because: 用户强调只列书名没有说服力

- When: 用户询问更新频率时
  Then: 需要给出稳定的更新节奏安排
  Because: 断更会导致读者流失
"""

# 仅共享 when 上下文词、不含任何 then 关键词的答卷（recall 侧 fixture）。
MISS_RESPONSE = "用户要求推荐书籍时，用户询问更新频率时，我都会认真作答。"

# 与 blueprint §1 #9 / 设计 §0 探针 [E] 逐 skill 一致的检入基线（11 个本地 skill；
# CI 只检出 tracked 子集，故 T12 按发现集 ∩ 基线键集比对）。
BASELINE_RULE_IDS = {
    "code_review": ["response_nonempty"],
    "create-readme": ["response_nonempty"],
    "frontend-design": ["response_nonempty"],
    "impeccable": ["response_nonempty"],
    "ui-ux-pro-max": ["response_nonempty", "json_parseable"],
    "using-superpowers": ["response_nonempty"],
    "vercel-react-best-practices": ["response_nonempty"],
    "web-design-guidelines": ["response_nonempty"],
    "webnovel-writing": ["response_nonempty"],
    "zhangxuefeng-perspective": ["response_nonempty", "lead_with_conclusion", "markdown_table"],
    "政府报告撰写-正式书面化与政策结合": ["response_nonempty"],
}


def _patch_home(monkeypatch, home: Path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))


def _derived_fixture_rules() -> list[dict]:
    return _derived_rules_from_instructions(FIXTURE_INSTRUCTIONS)


def _capped_section() -> str:
    parts = ["## Decision Rules\n\n"]
    for i in range(1, 12):
        parts.append(f"- When: 场景{i}\n")
        if i == 2:
            parts.append("\n")  # 缺 then 的块：跳过但占块序号
            continue
        if i == 10:
            parts.append("  Then: 所以的\n  Because: 全停用字提不出关键词\n\n")  # 提不出关键词：跳过
            continue
        parts.append(f"  Then: 回答第{i}条必须给出明确的处理步骤\n  Because: 理由{i}\n\n")
    return "".join(parts)


def test_async_eval_entry_runs_without_nameerror(tmp_path, monkeypatch):
    """修复前 async 入口实参求值期必现 NameError（min_replay_samples 未定义）；修复后返回 dict，
    且 status_map.json 落盘副作用只出现在沙箱内（绝不打真仓）。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".evoharness" / "skill-evolution").mkdir(parents=True)
    _patch_home(monkeypatch, tmp_path)  # home 向量隔离：~/.evoharness/skills 不许混入
    reset_skill_cache()
    try:
        report = asyncio.run(
            evaluate_online_skill_evolution_async(write_report=False, write_artifacts=False)
        )
    finally:
        reset_skill_cache()
    assert isinstance(report, dict)
    assert report["aggregate"]["skills"] == 0
    assert (tmp_path / ".evoharness" / "skill-evolution" / "online-eval" / "status_map.json").is_file()


def test_parse_decision_rules_section_canonical():
    text = (
        "# Skill\n\n正文。\n\n"
        "## Decision Rules\n\n"
        "- When: 用户要求推荐书籍时\n"
        "  Then: 每个推荐都必须附上推荐理由\n"
        "  Because: 用户强调只列书名没有说服力\n"
        "\n"
        "- When: 用户询问更新频率时\n"
        "  Then: 需要给出稳定的更新节奏安排\n"
        "  Because: 断更会导致读者流失\n"
    )
    assert _parse_decision_rules_section(text) == [
        {
            "when": ["用户要求推荐书籍时"],
            "then": ["每个推荐都必须附上推荐理由"],
            "because": ["用户强调只列书名没有说服力"],
        },
        {
            "when": ["用户询问更新频率时"],
            "then": ["需要给出稳定的更新节奏安排"],
            "because": ["断更会导致读者流失"],
        },
    ]


def test_parse_section_tolerates_variants():
    # 全角冒号 + 小写键 + 缺 Because → because==[]
    text = "### Decision Rules\n\n- when：用户催更时\n  then：解释排更原因\n"
    assert _parse_decision_rules_section(text) == [
        {"when": ["用户催更时"], "then": ["解释排更原因"], "because": []}
    ]

    # 中文别名区头 + 缺 Then
    text2 = "## 决策规则\n\n- When: 用户问背景时\n  Because: 先给背景再给答案\n"
    assert _parse_decision_rules_section(text2) == [
        {"when": ["用户问背景时"], "then": [], "because": ["先给背景再给答案"]}
    ]


def test_parse_section_boundaries():
    text = (
        "- When: 区外不解析\n"
        "  Then: 不应出现\n"
        "\n"
        "## Decision Rules\n"
        "\n"
        "- When: 区内第一块\n"
        "  Then: 回答需要包含结论\n"
        "    并给出理由\n"
        "\n"
        "## Other Section\n"
        "- When: 区后不解析\n"
        "  Then: 不应出现\n"
    )
    blocks = _parse_decision_rules_section(text)
    assert len(blocks) == 1
    assert blocks[0]["when"] == ["区内第一块"]
    assert blocks[0]["then"] == ["回答需要包含结论 并给出理由"]  # 缩进续行并入上一个键
    assert blocks[0]["because"] == []


def test_no_section_yields_no_rules():
    # 存量 parity 锚点：无区 instructions → 零派生规则（与 T12 真仓基线互证）。
    assert _parse_decision_rules_section("普通 instructions，没有 Decision Rules 区") == []
    assert _derived_rules_from_instructions("") == []
    assert _derived_rules_from_instructions("只有正文，没有区头") == []


def test_derived_rules_shape_and_cap():
    rules = _derived_rules_from_instructions(_capped_section())
    assert MAX_DERIVED_RULES == 7
    assert len(rules) == 7
    # rule_id 取区内块序号（1-based，区文本稳定则 id 稳定）：缺 then 的块 2 与
    # 提不出关键词的块 10 被跳过但占序号；块 9/11 因触顶不再产出。
    assert [rule["rule_id"] for rule in rules] == [
        "decision-rule-01",
        "decision-rule-03",
        "decision-rule-04",
        "decision-rule-05",
        "decision-rule-06",
        "decision-rule-07",
        "decision-rule-08",
    ]
    for index, rule in enumerate(rules):
        assert rule["kind"] == "programmatic"
        assert rule["hard"] is False  # 派生规则永不硬失败
        assert rule["params"]["mode"] == "keyword_hit"
        assert rule["params"]["keywords"]
        assert rule["params"]["when"].startswith("场景")
        assert rule["provenance"]["source"] == "skill_decision_rules"
        assert rule["provenance"]["because"].startswith("理由")
        assert rule["provenance"]["rule_index"] == [1, 3, 4, 5, 6, 7, 8][index]
        assert rule["label"].startswith(f"Decision rule {rule['provenance']['rule_index']}: ")
    json.dumps(rules, ensure_ascii=False)  # 编译产物 JSON 可序列化（eval_spec.json 通道）


def test_keyword_hit_true_on_fixture():
    rules = _derived_fixture_rules()
    assert len(rules) == 2
    response = "每个推荐都必须附上推荐理由；同时保持稳定的更新节奏安排。"
    for rule in rules:
        outcome = _evaluate_rule(rule, response)
        assert outcome["passed"] is True
        assert outcome["details"]["matched"]  # precision 侧


def test_keyword_hit_false_on_miss():
    rules = _derived_fixture_rules()
    for rule in rules:
        outcome = _evaluate_rule(rule, MISS_RESPONSE)
        assert outcome["passed"] is False
        assert outcome["details"]["matched"] == []  # recall 侧；T7+T8 合计 fixtures 上 precision=recall=1.0


def test_quoted_literal_tier():
    instructions = (
        "## Decision Rules\n\n"
        "- When: 用户纠结方案选择时\n"
        "  Then: 优先给出「短期方案」再谈长期\n"
        "  Because: 先落地再说\n"
    )
    rules = _derived_rules_from_instructions(instructions)
    assert len(rules) == 1
    assert rules[0]["params"]["keywords"] == ["短期方案"]  # Tier1 引号字面优先
    # when 共现词不作判据：字面短语已出现在 when 文本中 → 提不出关键词 → 空列表
    assert _derive_keywords("优先给出「短期方案」再谈长期", "用户纠结短期方案") == []
    assert _evaluate_rule(rules[0], "先给短期方案稳住需求")["passed"] is True
    assert _evaluate_rule(rules[0], "我会综合考虑长期规划")["passed"] is False


def test_derived_rules_never_hard():
    rules = _derived_fixture_rules()
    summary = _summarize_rule_outcomes(
        rules,
        [{"sample_id": "s1", "split": "promotion_test", "latest_assistant": MISS_RESPONSE}],
    )
    # 全错答卷：hard 计数只数 hard 真值 → 派生规则永不进 champion 硬门；
    # 但 pass_rate 诚实走低 → 进 watch 门槛（min_rule_pass_rate）。
    assert summary["hard_failures"] == 0
    assert summary["promotion_test_hard_failures"] == 0
    assert summary["pass_rate"] < 1.0


def test_section_format_does_not_trigger_template_rules():
    # 仅区头：编译结果只有基线规则
    rules = _compile_eval_rules(
        {"name": "t", "description": "", "when_to_use": "", "instructions": "## Decision Rules\n"},
        include_llm_rules=False,
    )
    assert [rule["rule_id"] for rule in rules] == ["response_nonempty"]

    # 规范区 + 块：只多出派生规则；区文本（When/Then/Because 等）不误触发
    # json_parseable / markdown_table / must_cite_sources 等关键词模板规则（risk#4 反触发锚点）。
    rules2 = _compile_eval_rules(
        {"name": "t", "description": "", "when_to_use": "", "instructions": FIXTURE_INSTRUCTIONS},
        include_llm_rules=False,
    )
    assert [rule["rule_id"] for rule in rules2] == [
        "response_nonempty",
        "decision-rule-01",
        "decision-rule-02",
    ]


def test_legacy_real_skills_parity(tmp_path, monkeypatch):
    """真仓 11 skill 的 legacy parity：存量 skill 无 Decision Rules 区 → 编译输出逐 skill 等于检入基线。

    按 F-5 裁决用「发现集 ∩ 基线键集」比对（CI 只检出 tracked 子集；本地 11 skill 时退化为全量）。
    SKILL.md 被合法改动致本测试失败时须有意识更新基线——回归网设计意图。"""
    repo_root = Path(__file__).resolve().parents[1]
    if not (repo_root / ".evoharness" / "skills").is_dir():
        pytest.skip("repo-local .evoharness/skills not present")
    monkeypatch.chdir(repo_root)
    _patch_home(monkeypatch, tmp_path)  # 隔离用户级 skill，发现面 = 项目级（设计 §0.1 [E] 口径）
    reset_skill_cache()
    try:
        snapshots = _active_skill_snapshots()
    finally:
        reset_skill_cache()
    discovered = set(snapshots)
    assert discovered & set(BASELINE_RULE_IDS), "repo project-level skills should be discovered"
    for name in discovered:
        rules = _compile_eval_rules(snapshots[name], include_llm_rules=False)
        assert all(not str(rule["rule_id"]).startswith("decision-rule-") for rule in rules)
        json.dumps(rules, ensure_ascii=False)  # 整体可序列化（eval_spec.json 写入通道）
    for name in sorted(discovered & set(BASELINE_RULE_IDS)):
        rules = _compile_eval_rules(snapshots[name], include_llm_rules=False)
        assert sorted(str(rule["rule_id"]) for rule in rules) == sorted(BASELINE_RULE_IDS[name])


# ─── 僵尸归档门可达 + M2/M4 接线（零模型、临时目录） ──────────────


def test_never_retrieved_unobserved_skill_reaches_archive_gate(tmp_path, monkeypatch):
    """回归：retrieved==0 的滞留 skill 恒判 unobserved；归档门须纳入该状态，
    否则僵尸归档分支（只认 incubating）不可达。"""
    import agents.online_skill_eval as ose
    from agents.skills import create_skill

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".evoharness" / "skill-evolution").mkdir(parents=True)
    _patch_home(monkeypatch, tmp_path)
    reset_skill_cache()
    assert create_skill(name="zombie-skill", description="d", instructions="i", actor="test")["ok"]
    calls: list[tuple] = []

    def fake_archive(name, stats, *, first_seen=""):
        calls.append((name, int(stats.get("retrieved", 0) or 0)))
        return False  # 只断言"进入判定"；是否真归档由 maybe_archive 的试用期/来源门决定

    monkeypatch.setattr(ose, "maybe_archive_never_retrieved_skill", fake_archive)
    try:
        report = asyncio.run(
            evaluate_online_skill_evolution_async(write_report=False, write_artifacts=False)
        )
    finally:
        reset_skill_cache()
    rows = {item["skill"]: item for item in report["skills"]}
    assert rows["zombie-skill"]["status"] == "unobserved"
    assert calls == [("zombie-skill", 0)]


def test_report_aggregate_includes_funnel_summary(tmp_path, monkeypatch):
    """M2 接线：summarize_funnel 摘要写入 report.aggregate 增补字段。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".evoharness" / "skill-evolution").mkdir(parents=True)
    _patch_home(monkeypatch, tmp_path)
    reset_skill_cache()
    try:
        report = asyncio.run(
            evaluate_online_skill_evolution_async(write_report=False, write_artifacts=False)
        )
    finally:
        reset_skill_cache()
    aggregate = report["aggregate"]
    assert "funnel" in aggregate
    funnel = aggregate["funnel"]
    assert funnel["samples"] == 0 and funnel["evaluated"] == 0
    assert set(funnel["overall"]) == {"pass", "fail", "na"}
    assert "RED" in funnel["layer_counts"]
    # 缺省（无 gates_path）绝不写 release_gate 字段——默认语义不变。
    assert "release_gate" not in aggregate


def test_report_aggregate_release_gate_opt_in(tmp_path, monkeypatch):
    """M4 显式入口：给出 gates 文件才评估并写 aggregate["release_gate"]。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".evoharness" / "skill-evolution").mkdir(parents=True)
    _patch_home(monkeypatch, tmp_path)
    gates_pass = tmp_path / "gates_pass.json"
    gates_pass.write_text(
        json.dumps([{"metric": "skills", "op": ">=", "threshold": 0}]), encoding="utf-8"
    )
    gates_fail = tmp_path / "gates_fail.json"
    gates_fail.write_text(
        json.dumps([{"metric": "skills", "op": ">=", "threshold": 5}]), encoding="utf-8"
    )
    reset_skill_cache()
    try:
        report_ok = asyncio.run(evaluate_online_skill_evolution_async(
            write_report=False, write_artifacts=False, gates_path=str(gates_pass)
        ))
    finally:
        reset_skill_cache()
    assert report_ok["aggregate"]["release_gate"] == {"passed": True, "violations": []}
    reset_skill_cache()
    try:
        report_bad = asyncio.run(evaluate_online_skill_evolution_async(
            write_report=False, write_artifacts=False, gates_path=str(gates_fail)
        ))
    finally:
        reset_skill_cache()
    release_gate = report_bad["aggregate"]["release_gate"]
    assert release_gate["passed"] is False
    assert release_gate["violations"] == [
        {"metric": "skills", "value": 0, "op": ">=", "threshold": 5}
    ]


def test_never_retrieved_gate_archives_never_used_skill(tmp_path, monkeypatch):
    """回归：僵尸归档门 = "从未被真正使用"（used==0 且 relevant==0，超期）。

    原先只看 retrieved（默认 0）会漏掉 retrieved 为 1/2 却从未真正使用的滞留僵尸；
    反过来只要用过/相关过（used>0 或 relevant>0）就绝不归档；超过检索上限（默认 2）
    的未使用 skill 交给 usage prune，本门不重复清理。归档后 pruned 标志必须粘滞回
    skill_usage_stats.json，否则下次评测重算回落 unobserved、反复归档。
    """
    import agents.skill_evolution as se

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("EVOHARNESS_SKILL_ZOMBIE_MAX_RETRIEVED", raising=False)
    monkeypatch.delenv("EVOHARNESS_SKILL_PRUNE_PROJECT", raising=False)
    (tmp_path / ".evoharness" / "skill-evolution").mkdir(parents=True)
    old = "2000-01-01T00:00:00Z"

    def make(name: str) -> str:
        d = tmp_path / ".evoharness" / "skills" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\n---\nbody", encoding="utf-8")
        return str(d)

    # retrieved 1/2 但从未被使用（used=0/relevant=0）→ 归档（原先被误留的滞留僵尸）
    for name, retrieved in (("cold-1", 1), ("cold-2", 2)):
        assert se.maybe_archive_never_retrieved_skill(
            name,
            {"source": "user", "retrieved": retrieved, "used": 0, "relevant": 0, "skill_dir": make(name)},
            first_seen=old,
        ) is True

    # 用过 / 相关过 → 绝不归档（保留原回归意图：不误伤真实使用的孵化期 skill）
    assert se.maybe_archive_never_retrieved_skill(
        "warm-used",
        {"source": "user", "retrieved": 2, "used": 1, "relevant": 0, "skill_dir": make("warm-used")},
        first_seen=old,
    ) is False
    assert se.maybe_archive_never_retrieved_skill(
        "warm-rel",
        {"source": "user", "retrieved": 2, "used": 0, "relevant": 1, "skill_dir": make("warm-rel")},
        first_seen=old,
    ) is False

    # 超过检索上限（默认 2）的未使用 skill 不归此门（由 usage prune 负责）
    assert se.maybe_archive_never_retrieved_skill(
        "busy", {"source": "user", "retrieved": 3, "used": 0, "relevant": 0, "skill_dir": make("busy")}, first_seen=old
    ) is False

    # 自进化产出的 project 级默认纳入归档判定（僵尸目标群体恰是 project 级 skill）
    assert se.maybe_archive_never_retrieved_skill(
        "proj", {"source": "project", "retrieved": 0, "used": 0, "relevant": 0, "skill_dir": make("proj")}, first_seen=old
    ) is True

    # retrieved 0 且超期 → 归档 + pruned 写回（状态粘滞）
    stats = {"source": "user", "retrieved": 0, "used": 0, "relevant": 0, "skill_dir": make("cold")}
    assert se.maybe_archive_never_retrieved_skill("cold", stats, first_seen=old) is True
    usage_path = tmp_path / ".evoharness" / "skill-evolution" / "skill_usage_stats.json"
    persisted = json.loads(usage_path.read_text(encoding="utf-8"))
    assert persisted["cold"]["pruned"] is True
    assert persisted["cold"]["prune_reason"] == "never-retrieved"
    assert se.load_skill_stats()["cold"]["pruned"] is True


def test_zombie_gate_respects_manual_invocations(tmp_path, monkeypatch):
    """回归（finding 1）：manual /<skill> 调用过（invocations>0）即算被真实使用，绝不归档。

    只看 retrieved/used/relevant 会误归档"从未被检索但被人工调用"的 user-invocable skill；
    同字段下 invocations 从 6 改 0 结论翻转，证明它是决定因素。
    """
    import agents.skill_evolution as se

    monkeypatch.chdir(tmp_path)
    old = "2000-01-01T00:00:00Z"

    def make(name: str) -> str:
        d = tmp_path / ".evoharness" / "skills" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\n---\nbody", encoding="utf-8")
        return str(d)

    base = {"source": "project", "retrieved": 0, "used": 0, "relevant": 0, "self_evolved": True}
    assert se.maybe_archive_never_retrieved_skill(
        "manual", {**base, "invocations": 6, "skill_dir": make("manual")}, first_seen=old
    ) is False
    assert se.maybe_archive_never_retrieved_skill(
        "auto", {**base, "invocations": 0, "skill_dir": make("auto")}, first_seen=old
    ) is True


def test_zombie_gate_protects_handwritten_project_skill(tmp_path, monkeypatch):
    """回归（finding 3）：僵尸门仅归档自进化产出，手写 project skill（self_evolved=False）不归档。"""
    import agents.skill_evolution as se

    monkeypatch.chdir(tmp_path)
    old = "2000-01-01T00:00:00Z"

    def make(name: str) -> str:
        d = tmp_path / ".evoharness" / "skills" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\n---\nbody", encoding="utf-8")
        return str(d)

    common = {"source": "project", "retrieved": 0, "used": 0, "relevant": 0, "invocations": 0}
    assert se.maybe_archive_never_retrieved_skill(
        "handwritten", {**common, "self_evolved": False, "skill_dir": make("handwritten")}, first_seen=old
    ) is False
    assert se.maybe_archive_never_retrieved_skill(
        "evolved", {**common, "self_evolved": True, "skill_dir": make("evolved")}, first_seen=old
    ) is True


def test_usage_prune_allows_project_source(tmp_path, monkeypatch):
    """回归（finding 2）：usage prune 来源口径与僵尸门统一，project 级自进化 skill 同样纳入。

    原先只纳 user，project 僵尸的 retrieved∈[3,39] 且 used==0 落在两门之间永不清理。
    """
    import agents.skill_evolution as se

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("EVOHARNESS_SKILL_PRUNE_PROJECT", raising=False)

    def make(name: str) -> str:
        d = tmp_path / ".evoharness" / "skills" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\n---\nbody", encoding="utf-8")
        return str(d)

    # 检索够多却从未被使用 → 归档（project 源此前恒不触发）
    assert se._maybe_prune_stale_skill(
        "proj-heavy", {"source": "project", "retrieved": 45, "used": 0, "skill_dir": make("proj-heavy")}
    ) is True
    # 用过 → 不归档
    assert se._maybe_prune_stale_skill(
        "proj-used", {"source": "project", "retrieved": 45, "used": 1, "skill_dir": make("proj-used")}
    ) is False
    # 非 user/project 来源且无 env 开关 → 不归档
    assert se._maybe_prune_stale_skill(
        "remote", {"source": "remote", "retrieved": 45, "used": 0, "skill_dir": make("remote")}
    ) is False


def test_usage_prune_respects_invocations_and_self_evolved(tmp_path, monkeypatch):
    """回归（finding 1）：usage-prune 与僵尸门对称，加 invocations==0 与 self_evolved==True 守卫。

    放宽 source 到 project 后，若缺这两个守卫，手动调用过（invocations>0）或手写的
    project skill 会被 usage-prune 误归档；同字段下翻转 invocations/self_evolved 结论
    翻转，证明两者都是决定因素。
    """
    import agents.skill_evolution as se

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("EVOHARNESS_SKILL_PRUNE_PROJECT", raising=False)

    def make(name: str) -> str:
        d = tmp_path / ".evoharness" / "skills" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\n---\nbody", encoding="utf-8")
        return str(d)

    base = {"source": "project", "retrieved": 45, "used": 0}
    # 手动 /<skill> 调用过（invocations>0）→ 绝不归档
    assert se._maybe_prune_stale_skill(
        "manual", {**base, "invocations": 6, "skill_dir": make("manual")}
    ) is False
    # 手写 project skill（self_evolved=False）→ 绝不归档
    assert se._maybe_prune_stale_skill(
        "handwritten", {**base, "self_evolved": False, "skill_dir": make("handwritten")}
    ) is False
    # 自进化且未手动调用（invocations==0）→ 归档（正常僵尸路径不被守卫误挡）
    assert se._maybe_prune_stale_skill(
        "evolved", {**base, "invocations": 0, "self_evolved": True, "skill_dir": make("evolved")}
    ) is True


def test_usage_prune_production_path_carries_invocations_and_self_evolved(tmp_path, monkeypatch):
    """回归（finding 1）：usage-prune 真实链路（record_skill_usage_judgments）必须携带
    invocations 与 self_evolved，否则 _maybe_prune_stale_skill 的守卫因字段缺失恒不触发。

    before：judgments（与 judge_retrieved_skill_usage 同形，无这两字段）直接入 stats，
    手写 / 手动调用过的 project skill 会被误归档；after：字段从 usage 统计（invoke/create
    事件）与 provenance 补齐 → 守卫实效。三例同字段翻转结论翻转，证明两字段都是真实
    链路上的决定因素（②③隔离出 self_evolved 与 invocations 各自的作用）。
    """
    import agents.skill_evolution as se
    from agents.skills import create_skill

    monkeypatch.chdir(tmp_path)
    evo_root = tmp_path / ".evoharness" / "skill-evolution"
    evo_root.mkdir(parents=True)
    # 压低检索门槛：单轮 judgment 即达 usage-prune 判定（不依赖默认 40 次检索）。
    monkeypatch.setenv("EVOHARNESS_SKILL_USAGE_PRUNE_MIN_RETRIEVED", "1")
    monkeypatch.delenv("EVOHARNESS_SKILL_PRUNE_OTHER_SOURCES", raising=False)
    monkeypatch.delenv("EVOHARNESS_SKILL_PRUNE_PROJECT", raising=False)
    _patch_home(monkeypatch, tmp_path / "home")
    (tmp_path / "home").mkdir()
    reset_skill_cache()

    def make(name: str) -> str:
        d = tmp_path / ".evoharness" / "skills" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\n---\nbody", encoding="utf-8")
        return str(d)

    def judgment(name: str, skill_dir: str) -> dict:
        # 与 judge_retrieved_skill_usage 产出同形：无 invocations/self_evolved 字段。
        return {
            "name": name, "source": "project", "skill_dir": skill_dir,
            "retrieved": True, "relevant": False, "used": False,
            "score": 0.5, "reason": "",
        }

    # ① 手写 project skill（无 create 事件、无 provenance）→ self_evolved=False → 不归档
    handwritten = make("handwritten")
    result = se.record_skill_usage_judgments([judgment("handwritten", handwritten)])
    assert result["pruned"] == []
    assert (tmp_path / ".evoharness" / "skills" / "handwritten").is_dir()
    persisted = json.loads((evo_root / "skill_usage_stats.json").read_text(encoding="utf-8"))
    assert persisted["handwritten"]["self_evolved"] is False
    assert persisted["handwritten"]["invocations"] == 0

    # ② 自进化 project skill（create 事件 → self_evolved=True）且未手动调用 → 正常归档
    assert create_skill(name="evolved", description="d", instructions="i", actor="test")["ok"]
    evolved_dir = str(tmp_path / ".evoharness" / "skills" / "evolved")
    result = se.record_skill_usage_judgments([judgment("evolved", evolved_dir)])
    assert result["pruned"] == ["evolved"]
    assert not (tmp_path / ".evoharness" / "skills" / "evolved").is_dir()

    # ③ 手动 /<skill> 调用过（invocations>0）→ 不归档（隔离 invocations 为决定因素）
    assert create_skill(name="manual", description="d", instructions="i", actor="test")["ok"]
    with (evo_root / "usage.jsonl").open("a", encoding="utf-8") as f:
        for _ in range(6):
            f.write(json.dumps({
                "event": "invoke", "time": "2000-01-02T00:00:00Z", "skill": "manual",
                "source": "project", "context": "inline", "args_preview": "",
            }, ensure_ascii=False, sort_keys=True) + "\n")
    manual_dir = str(tmp_path / ".evoharness" / "skills" / "manual")
    result = se.record_skill_usage_judgments([judgment("manual", manual_dir)])
    assert result["pruned"] == []
    assert (tmp_path / ".evoharness" / "skills" / "manual").is_dir()
    persisted = json.loads((evo_root / "skill_usage_stats.json").read_text(encoding="utf-8"))
    assert persisted["manual"]["invocations"] == 6


def test_zombie_gate_protects_handwritten_user_skill(tmp_path, monkeypatch):
    """回归（finding 2）："仅归档自进化"的保护对 user 级同样生效，user 级手写不归档。

    原实现 `source == 'project'` 才保护 self_evolved=False，user 级手写仍会被僵尸门
    归档；同字段下翻转 self_evolved 结论翻转。
    """
    import agents.skill_evolution as se

    monkeypatch.chdir(tmp_path)
    old = "2000-01-01T00:00:00Z"

    def make(name: str) -> str:
        d = tmp_path / ".evoharness" / "skills" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\n---\nbody", encoding="utf-8")
        return str(d)

    common = {"source": "user", "retrieved": 0, "used": 0, "relevant": 0, "invocations": 0}
    assert se.maybe_archive_never_retrieved_skill(
        "handwritten-user", {**common, "self_evolved": False, "skill_dir": make("handwritten-user")}, first_seen=old
    ) is False
    assert se.maybe_archive_never_retrieved_skill(
        "evolved-user", {**common, "self_evolved": True, "skill_dir": make("evolved-user")}, first_seen=old
    ) is True


def test_prune_other_sources_env_gates_nondefault_sources(tmp_path, monkeypatch):
    """回归（finding 3）：env 名实一致——project/user 已默认纳入，env 只 gate 其他来源。

    新的 EVOHARNESS_SKILL_PRUNE_OTHER_SOURCES 生效；旧名 EVOHARNESS_SKILL_PRUNE_PROJECT
    作兼容别名仍被识别。
    """
    import agents.skill_evolution as se

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("EVOHARNESS_SKILL_PRUNE_OTHER_SOURCES", raising=False)
    monkeypatch.delenv("EVOHARNESS_SKILL_PRUNE_PROJECT", raising=False)

    def make(name: str) -> str:
        d = tmp_path / ".evoharness" / "skills" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\n---\nbody", encoding="utf-8")
        return str(d)

    remote = {"source": "remote", "retrieved": 45, "used": 0}
    # 其他来源在无开关时不纳（旧名也不设）
    assert se._maybe_prune_stale_skill(
        "remote-off", {**remote, "skill_dir": make("remote-off")}
    ) is False
    # 新名开启 → 其他来源纳入
    monkeypatch.setenv("EVOHARNESS_SKILL_PRUNE_OTHER_SOURCES", "1")
    assert se._maybe_prune_stale_skill(
        "remote-on", {**remote, "skill_dir": make("remote-on")}
    ) is True
    # 旧名兼容别名仍生效
    monkeypatch.delenv("EVOHARNESS_SKILL_PRUNE_OTHER_SOURCES", raising=False)
    monkeypatch.setenv("EVOHARNESS_SKILL_PRUNE_PROJECT", "1")
    assert se._maybe_prune_stale_skill(
        "remote-legacy", {**remote, "skill_dir": make("remote-legacy")}
    ) is True


def test_manual_invocation_prevents_zombie_archive_e2e(tmp_path, monkeypatch):
    """回归（finding 1 端到端）：沙箱中 user-invocable project skill + usage.jsonl 6 条 invoke。

    修复前 status='pruned'、目录被移走；修复后 invocations 进入僵尸门输入、绝不误归档。
    """
    from agents.skills import create_skill

    monkeypatch.chdir(tmp_path)
    evo_root = tmp_path / ".evoharness" / "skill-evolution"
    evo_root.mkdir(parents=True)
    _patch_home(monkeypatch, tmp_path)
    reset_skill_cache()
    assert create_skill(
        name="manual-skill", description="d", instructions="i", actor="test", user_invocable=True
    )["ok"]
    # 回填 usage.jsonl：create 事件改为超期，并追加 6 条 invoke（invocations=6）
    usage_path = evo_root / "usage.jsonl"
    rows = [json.loads(line) for line in usage_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for row in rows:
        if row.get("event") == "create":
            row["time"] = "2000-01-01T00:00:00Z"
    for _ in range(6):
        rows.append({
            "event": "invoke", "time": "2000-01-02T00:00:00Z", "skill": "manual-skill",
            "source": "project", "context": "inline", "args_preview": "",
        })
    usage_path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in rows) + "\n", encoding="utf-8"
    )
    reset_skill_cache()
    try:
        report = asyncio.run(
            evaluate_online_skill_evolution_async(write_report=False, write_artifacts=False)
        )
    finally:
        reset_skill_cache()
    row = {item["skill"]: item for item in report["skills"]}["manual-skill"]
    assert row["invocations"] == 6
    assert row["status"] != "pruned"
    assert (tmp_path / ".evoharness" / "skills" / "manual-skill").is_dir()


def test_load_skill_stats_cache_matches_uncached_and_invalidates(tmp_path, monkeypatch):
    """回归（finding 1）：usage-tracking 热路径 load_skill_stats 的进程内缓存不改回填、能按改动失效。

    load_skill_stats 每轮被 record_skill_usage_judgments 调用；昂贵部分（usage.jsonl +
    history/*.jsonl）按 mtime+size 缓存。本测试证明：①命中缓存与未缓存逐字段一致（回填
    invocations/created_at/snapshots 不变）；②源文件追加后缓存立刻失效、反映新值。
    """
    import agents.skill_evolution as se

    monkeypatch.chdir(tmp_path)
    evo_root = tmp_path / ".evoharness" / "skill-evolution"
    evo_root.mkdir(parents=True)
    # 清掉可能残留的进程级缓存，保证从冷态起算。
    se._usage_index_cache_key = None
    se._usage_index_cache = None

    def append_usage(rows: list[dict]) -> None:
        with (evo_root / "usage.jsonl").open("a", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    append_usage([
        {"event": "create", "time": "2000-01-01T00:00:00Z", "skill": "alpha", "file": "a"},
        {"event": "invoke", "time": "2000-01-02T00:00:00Z", "skill": "alpha"},
    ])
    (evo_root / "history").mkdir()
    (evo_root / "history" / "alpha.jsonl").write_text("{}\n{}\n", encoding="utf-8")
    (evo_root / "skill_usage_stats.json").write_text(
        json.dumps({"alpha": {"retrieved": 3, "relevant": 1, "used": 0, "pruned": False}}),
        encoding="utf-8",
    )

    first = se.load_skill_stats()
    assert first["alpha"]["invocations"] == 1
    assert first["alpha"]["created"] == 1
    assert first["alpha"]["snapshots"] == 2
    assert first["alpha"]["retrieved"] == 3

    # 命中缓存（源未变）与强制未缓存重算，逐字段一致 → 缓存只是省重读、不改回填。
    cached_hit = se.load_skill_stats()
    assert cached_hit == first
    se._usage_index_cache_key = None
    se._usage_index_cache = None
    assert se.load_skill_stats() == first

    # 缓存返回副本：mutate 命中结果不污染后续命中。
    cached_hit["alpha"]["invocations"] = 999
    assert se.load_skill_stats()["alpha"]["invocations"] == 1

    # 源文件追加 → 缓存失效、字段反映新值（usage→invocations；history→snapshots）。
    append_usage([{"event": "invoke", "time": "2000-01-03T00:00:00Z", "skill": "alpha"}])
    with (evo_root / "history" / "alpha.jsonl").open("a", encoding="utf-8") as f:
        f.write("{}\n")
    updated = se.load_skill_stats()
    assert updated["alpha"]["invocations"] == 2
    assert updated["alpha"]["snapshots"] == 3


def test_zombie_gate_stats_include_invocations_and_self_evolved(tmp_path, monkeypatch):
    """接线回归：eval 循环构造的 zombie_stats 必须带入 invocations 与 self_evolved。"""
    import agents.online_skill_eval as ose
    from agents.skills import create_skill

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".evoharness" / "skill-evolution").mkdir(parents=True)
    _patch_home(monkeypatch, tmp_path)
    reset_skill_cache()
    assert create_skill(name="probe-skill", description="d", instructions="i", actor="test")["ok"]
    captured: dict[str, dict] = {}

    def fake_archive(name, stats, *, first_seen=""):
        captured[name] = dict(stats)
        return False

    monkeypatch.setattr(ose, "maybe_archive_never_retrieved_skill", fake_archive)
    try:
        asyncio.run(evaluate_online_skill_evolution_async(write_report=False, write_artifacts=False))
    finally:
        reset_skill_cache()
    stats = captured["probe-skill"]
    assert stats["invocations"] == 0
    # create_skill 写了 create 事件 → 有 created_at，自进化（非手写）
    assert stats["self_evolved"] is True


def test_truncated_eval_rules_flags_dropped_base_rules():
    """rules[:8] 截断代价显式可见：派生规则占满 8 槽时，基础硬规则被标注为截断。"""
    from agents.online_skill_eval import _truncated_eval_rules

    # 7 条派生规则（_capped_section）+ corpus 触发 json_parseable / markdown_table：
    # 编译产物 = [response_nonempty] + 7 派生 = 8；两个基础规则被静默截掉。
    skill = {
        "name": "t",
        "description": "输出 JSON 并说明表格结果",
        "when_to_use": "",
        "instructions": _capped_section(),
    }
    rules = _compile_eval_rules(skill, include_llm_rules=False)
    assert len(rules) == 8
    truncation = _truncated_eval_rules(skill, rules, include_llm_rules=False)
    assert truncation["cap"] == 8 and truncation["derived"] == 7
    assert truncation["truncated"] is True
    assert set(truncation["dropped_rule_ids"]) == {"json_parseable", "markdown_table"}

    # 未饱和：派生规则少、基础规则都在产物里 → 无截断
    ok_skill = {
        "name": "t2",
        "description": "输出 JSON 并说明表格结果",
        "when_to_use": "",
        "instructions": FIXTURE_INSTRUCTIONS,
    }
    ok_rules = _compile_eval_rules(ok_skill, include_llm_rules=False)
    ok_truncation = _truncated_eval_rules(ok_skill, ok_rules, include_llm_rules=False)
    assert ok_truncation["truncated"] is False
    assert ok_truncation["dropped_rule_ids"] == []


def test_summarize_funnel_counts_red_failure_on_insufficient_evidence():
    """回归：证据不足（NA）样本的 RED 失败现在也计入 failed_layer_counts。"""
    from agents.online_skill_eval import summarize_funnel

    summary = summarize_funnel(
        [{"sample_id": "s1", "rule_id": "safety_refusal", "passed": False}],
        [{"sample_id": "s1", "latest_user": "", "latest_assistant": ""}],
    )
    assert summary["overall"]["na"] == 1
    assert summary["failed_layer_counts"].get("RED") == 1


def test_module_entrypoint_exit_zero(tmp_path):
    """回归：`python -m agents.online_skill_eval` 直跑入口不得 NameError。

    __main__ 块此前位于 summarize_funnel / _evaluate_release_gate 定义之前，core 调用
    summarize_funnel 时必现 NameError、退出码 1；修复（入口置于文件末尾）后退出码 0。
    """
    import os
    import subprocess

    home = tmp_path / "entry-home"
    home.mkdir()
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env.pop("EVOHARNESS_RELEASE_GATES", None)
    proc = subprocess.run(
        [sys.executable, "-m", "agents.online_skill_eval"],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "Online skill eval" in proc.stdout

def test_load_skill_stats_cache_invalidated_by_same_size_same_mtime_rewrite(tmp_path, monkeypatch):
    """回归（finding 3）：usage.jsonl 同 inode 等长原地改写且 mtime 相同，缓存键含内容哈希 → 仍失效。"""
    import os
    import agents.skill_evolution as se

    monkeypatch.chdir(tmp_path)
    evo_root = tmp_path / ".evoharness" / "skill-evolution"
    evo_root.mkdir(parents=True)
    se._usage_index_cache_key = None
    se._usage_index_cache = None

    usage_path = evo_root / "usage.jsonl"
    # 两条事件名 "create"/"invoke" 等长，保证等长改写；内容不同 → 聚合字段应随之改变。
    usage_path.write_text('{"event": "create", "skill": "alpha"}' + "\n", encoding="utf-8")
    st = usage_path.stat()

    first = se.load_skill_stats()
    assert first["alpha"]["created"] == 1
    assert first["alpha"]["invocations"] == 0

    usage_path.write_text('{"event": "invoke", "skill": "alpha"}' + "\n", encoding="utf-8")
    # 恢复 mtime，构造「等长 + 同 mtime」——旧 (mtime_ns,size) 键无法区分，会命中陈旧聚合。
    os.utime(usage_path, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert usage_path.stat().st_size == st.st_size

    second = se.load_skill_stats()
    assert second["alpha"]["invocations"] == 1
    assert second["alpha"]["created"] == 0
