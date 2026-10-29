"""M4 评测工具包测试：golden 互锁、legacy 适配确定性、规则基线 parity、幻觉探针、CLI 只读保证。

skip 纪律（M4 §6）：
- M1（agents.experience_standard）未落地时，依赖它的测试 importorskip；
- 数据类测试在本机未发现 skill / 无 .evoharness 数据时 pytest.skip；
- --status-parity 不进 pytest（CLI-only：整轮评测 + 沙箱 + F3 双守卫）。

沙箱纪律（蓝图 U1）：凡触发 discover_skills 的测试一律把 Path.home patch 到空临时
目录（隔离用户级 skills 污染发现面）+ chdir 仓库根 + reset_skill_cache()；
子进程 CLI 测试注入 HOME/USERPROFILE 指向空临时目录。
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval.experience_eval import compare_ir, hallucination_probe  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
GOLDEN_DIR = REPO_ROOT / "eval" / "experience_eval" / "golden"
GOLDEN_ZH = "experience-standard-zh-simulation.v1.json"
GOLDEN_CODING = "experience-standard-coding-agent.v1.json"

# B 校验器自己的形状集（experience_standard.py:103-170）：text 条目路径 + decision_rules
# 三键 + knowledge_gaps 两键 + indexing 五字段；不把 when/then/because 与 gaps 误当 text 条目遍历。
_TEXT_ENTRY_PATHS = {
    "situation": ("goal", "triggers", "context", "entities"),
    "guidance": ("principles", "steps", "avoid"),
    "applicability": ("conditions", "prerequisites", "exceptions", "boundaries"),
    "outcome": ("expected", "success_signals", "failure_signals"),
    "evidence": ("observations", "counterexamples", "validation_methods"),
}
_REQUIRED_SUBFIELDS = {
    **{section: list(fields) for section, fields in _TEXT_ENTRY_PATHS.items()},
    "guidance": ["principles", "decision_rules", "steps", "avoid"],
    "indexing": ["domains", "tasks", "tags", "aliases", "cross_domain_links"],
}
_TOP_LEVEL_FIELDS = (
    "schema_version",
    "source",
    "identity",
    "situation",
    "guidance",
    "applicability",
    "outcome",
    "evidence",
    "indexing",
    "knowledge_gaps",
)


def _load_golden(name: str) -> dict:
    return json.loads((GOLDEN_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture
def repo_skills_env(tmp_path, monkeypatch):
    """U1 沙箱：Path.home 指向空临时目录 + cwd=仓库根 + 前后刷新 skill 缓存。"""
    from agents.skills import reset_skill_cache

    home = tmp_path / "isolated-home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls, _home=home: _home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.chdir(REPO_ROOT)
    reset_skill_cache()
    yield
    reset_skill_cache()


def _repo_skill_count() -> int:
    from agents.skills import discover_skills

    return len(discover_skills())


# ─── golden 互锁 ──────────────────────────────────────────────────────


def test_golden_zh_simulation_validates():
    # zh golden = B examples/constitutive-model-selection.v1.json 内容同构移植（CRLF→LF），
    # 校验器互锁不经适配：validate_record(record, record["source"]["raw_text"]) 必须 0 errors。
    es = pytest.importorskip("agents.experience_standard")
    record = _load_golden(GOLDEN_ZH)
    assert es.validate_record(record, record["source"]["raw_text"]) == []


def test_golden_coding_agent_validates():
    es = pytest.importorskip("agents.experience_standard")
    record = _load_golden(GOLDEN_CODING)
    assert es.validate_record(record, record["source"]["raw_text"]) == []
    # 空规则形状被接受而非缺键：decision_rules==[] 是合法值。
    assert record["guidance"]["decision_rules"] == []


def test_golden_coverage_contract():
    # 十字段齐全 + 条目形状遍历限定为校验器自己的形状集 + 类型并集 >=4 + >=1 个 decision_rules==[]。
    records = [_load_golden(GOLDEN_ZH), _load_golden(GOLDEN_CODING)]
    for record in records:
        assert all(key in record for key in _TOP_LEVEL_FIELDS)
        for section, fields in _REQUIRED_SUBFIELDS.items():
            assert isinstance(record[section], dict), section
            assert all(field in record[section] for field in fields), (section, fields)
        assert record["schema_version"] == "experience-standard/v1"
        for section, fields in _TEXT_ENTRY_PATHS.items():
            for field in fields:
                for entry in record[section][field]:
                    assert isinstance(entry.get("text"), str) and entry["text"].strip(), (section, field)
                    assert entry["basis"] in ("explicit", "inferred"), (section, field)
                    assert isinstance(entry["confidence"], (int, float)) and 0 <= entry["confidence"] <= 1
        for rule in record["guidance"]["decision_rules"]:
            assert set(rule) >= {"when", "then", "because"}
            for part in ("when", "then", "because"):
                for entry in rule[part]:
                    assert isinstance(entry.get("text"), str) and entry["text"].strip()
        for gap in record["knowledge_gaps"]:
            assert isinstance(gap.get("question"), str) and gap["question"].strip()
            assert isinstance(gap.get("why_needed"), str)
        for field in ("domains", "tasks", "tags", "aliases", "cross_domain_links"):
            values = record["indexing"][field]
            assert isinstance(values, list)
            assert all(isinstance(value, str) and value.strip() for value in values), field
    type_values = set()
    empty_rule_records = 0
    for record in records:
        type_values.add(record["identity"]["primary_type"])
        type_values.update(record["identity"]["secondary_types"])
        if record["guidance"]["decision_rules"] == []:
            empty_rule_records += 1
    assert len(type_values) >= 4
    assert empty_rule_records >= 1


# ─── legacy 适配（--ir-only 的内核）──────────────────────────────────


def test_ir_only_zero_errors_on_repo_skills(repo_skills_env):
    es = pytest.importorskip("agents.experience_standard")
    if not _repo_skill_count():
        pytest.skip("no skills discovered")
    inputs = compare_ir.build_skill_ir_inputs()
    assert inputs
    total = 0
    for entry in inputs:
        errors = es.validate_record(entry["record"], entry["raw_text"])
        assert errors == [], f"{entry['name']}: {errors}"
        total += len(errors)
    assert total == 0
    assert total == compare_ir.legacy_ir_error_count()


def test_ir_only_repairs_fixed_and_deterministic(repo_skills_env):
    # R6 ⟺ when_to_use 非空；无 R6 的恰为 {R0..R5,R7,R8}（R0 计入）；两次构造逐字节一致。
    pytest.importorskip("agents.experience_standard")
    if not _repo_skill_count():
        pytest.skip("no skills discovered")
    first = compare_ir.build_skill_ir_inputs()
    second = compare_ir.build_skill_ir_inputs()
    for entry in first:
        repairs = set(entry["repairs"])
        has_r6 = "R6" in repairs
        assert has_r6 == bool(entry["when_to_use"].strip()), entry["name"]
        expected = {"R0", "R1", "R2", "R3", "R4", "R5", "R7", "R8"} | ({"R6"} if has_r6 else set())
        assert repairs == expected, entry["name"]
    dump_first = [json.dumps(e["record"], ensure_ascii=False, sort_keys=True) for e in first]
    dump_second = [json.dumps(e["record"], ensure_ascii=False, sort_keys=True) for e in second]
    assert dump_first == dump_second
    assert [e["repairs"] for e in first] == [e["repairs"] for e in second]


# ─── 规则基线 parity（不需要 M1）────────────────────────────────────


def test_rules_parity_frozen_baseline(repo_skills_env):
    if not _repo_skill_count():
        pytest.skip("no skills discovered")
    current = compare_ir.compute_current_eval_rules()
    assert current
    # M3 后零变化守卫：交集口径（CI 只检出 4 个 tracked skill 时退化为 4 skill 对比）。
    assert compare_ir.diff_eval_rules(compare_ir.RULES_BASELINE, current) == []


def test_rules_cap_invariant(repo_skills_env):
    if not _repo_skill_count():
        pytest.skip("no skills discovered")
    current = compare_ir.compute_current_eval_rules()
    assert current
    for name, rules in current.items():
        assert len(rules) <= 8, f"{name}: {len(rules)} rules（online_skill_eval.py:315 截断上限）"


# ─── 幻觉探针 ────────────────────────────────────────────────────────


def test_probe_zero_overlap_capture_meets_hard_gate():
    pytest.importorskip("agents.experience_standard")
    report = hallucination_probe.run_no_model_probe()
    assert report.fabricated_zero_overlap_count == 3
    assert report.fabrication_capture_rate_zero_overlap >= 0.9


def test_probe_false_flag_rate_within_threshold():
    pytest.importorskip("agents.experience_standard")
    report = hallucination_probe.run_no_model_probe()
    assert report.real_count == 3
    assert report.false_flag_rate <= 0.1


def test_probe_low_overlap_reported_not_gated():
    pytest.importorskip("agents.experience_standard")
    report = hallucination_probe.run_no_model_probe()
    assert report.fabricated_low_overlap_count == 2
    assert 0.0 <= report.fabrication_capture_rate_low_overlap <= 1.0
    # 无阈值分支的程序化断言：--check 硬门函数的逻辑体（去掉 docstring）只引用
    # zero_overlap 与 false_flag，low_overlap 速率不在任何比较里（改写型编造超出
    # no-model 判定能力，只观测）。
    gate_tree = ast.parse(inspect.getsource(hallucination_probe.evaluate_check_gates))
    gate_func = gate_tree.body[0]
    if (
        gate_func.body
        and isinstance(gate_func.body[0], ast.Expr)
        and isinstance(gate_func.body[0].value, ast.Constant)
        and isinstance(gate_func.body[0].value.value, str)
    ):
        gate_func.body = gate_func.body[1:]  # 剥离 docstring，只看逻辑体
    assert "low_overlap" not in ast.unparse(gate_func)


def test_probe_fixture_integrity():
    es = pytest.importorskip("agents.experience_standard")
    report = hallucination_probe.run_no_model_probe()
    tokenizer = getattr(es, "default_tokenize", None)
    if not callable(tokenizer):
        assert report.tokenizer_available is False
        return
    assert report.tokenizer_available is True
    assert report.fixture_integrity_ok is True
    # 独立复核：零重叠条目与窗口的 token 交集确实为空，real 条目确实非空。
    window_tokens = set(tokenizer(hallucination_probe.WINDOW_TEXT))
    for case in hallucination_probe.build_probe_cases():
        overlap = set(tokenizer(case.text)) & window_tokens
        if case.kind == "fabricated_zero_overlap":
            assert not overlap, case.text
        elif case.kind == "real":
            assert overlap, case.text


# ─── CLI（脚本直跑端到端 + 只读保证）────────────────────────────────


def test_cli_ir_only_script_mode(tmp_path, monkeypatch):
    # 脚本直跑 sys.path 自举端到端验证（决策书验收命令形态）；子进程注入 HOME/USERPROFILE
    # 指向空临时目录（U1），使其只发现项目级 skill。
    pytest.importorskip("agents.experience_standard")
    from agents.skills import reset_skill_cache

    home = tmp_path / "cli-home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls, _home=home: _home))
    monkeypatch.chdir(REPO_ROOT)
    reset_skill_cache()
    if not _repo_skill_count():
        reset_skill_cache()
        pytest.skip("no skills discovered")
    reset_skill_cache()
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    proc = subprocess.run(
        [sys.executable, "eval/experience_eval/compare_ir.py", "--ir-only"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        timeout=300,
    )
    # 显式按 UTF-8 解码：子进程输出含 CJK，若父进程按本地编码（cp936）解码且子进程因
    # PYTHONIOENCODING=utf-8 输出 UTF-8，会触发 reader 线程 UnicodeDecodeError（stdout=None）。
    # 关键断言串为 ASCII，errors="replace" 下不受解码差异影响。
    stdout = (proc.stdout or b"").decode("utf-8", errors="replace")
    stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
    assert proc.returncode == 0, f"stdout={stdout}\nstderr={stderr}"
    assert "legacy_ir_error_count=0" in stdout


def test_cli_readonly_guarantee(tmp_path, monkeypatch):
    # 对 .evoharness/ 现存子树取 (relpath, size, sha1) 指纹；豁免 skill_load_errors.jsonl
    # （加载器的条件性追加通道，skills.py:121-148/:190-199），改做 append-only 断言。
    pytest.importorskip("agents.experience_standard")
    evo_root = REPO_ROOT / ".evoharness"
    if not evo_root.is_dir():
        pytest.skip(".evoharness absent")
    home = tmp_path / "readonly-home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls, _home=home: _home))
    monkeypatch.chdir(REPO_ROOT)
    from agents.skills import reset_skill_cache

    reset_skill_cache()

    def fingerprint() -> dict[str, tuple[int, str]]:
        out: dict[str, tuple[int, str]] = {}
        for path in sorted(evo_root.rglob("*")):
            if path.is_file():
                rel = path.relative_to(REPO_ROOT).as_posix()
                if rel.endswith("skill_load_errors.jsonl"):
                    continue
                out[rel] = (path.stat().st_size, hashlib.sha1(path.read_bytes()).hexdigest())
        return out

    log_path = evo_root / "skill-evolution" / "skill_load_errors.jsonl"
    log_before = log_path.read_bytes() if log_path.is_file() else None
    files_before = fingerprint()
    compare_ir.cmd_ir_only()
    compare_ir.cmd_rules_parity()
    files_after = fingerprint()
    assert files_before == files_after
    log_after = log_path.read_bytes() if log_path.is_file() else None
    if log_after != log_before:
        # 只允许追加：原字节是新字节前缀，且新增行均为合法 JSONL、event ∈ {load_failed, load_warning}。
        assert log_before is None or log_after.startswith(log_before)
        added = log_after[len(log_before or b"") :].decode("utf-8")
        for line in added.splitlines():
            if line.strip():
                row = json.loads(line)
                assert row.get("event") in {"load_failed", "load_warning"}, row


# ─── 静态守卫与降级路径 ─────────────────────────────────────────────


def test_m4_sources_no_bear_reference():
    # 数据目录混淆守卫：M4 三个 .py 源码不得含 ".bear"（.bear/skill-evolution 是旧 BearCode
    # 遗留，agents/ 零引用；M4 一律经 get_evolution_dir()/discover_skills() 解析路径）。
    import eval.experience_eval as package

    for module in (package, compare_ir, hallucination_probe):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert ".bear" not in source, module.__name__


def test_model_smoke_degrades_without_keys(tmp_path, monkeypatch):
    # env={} 哨兵：os.environ 里的 APIKEY 不被读取；agents.agent 惰性导入未触发=零 SDK/零网络。
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APIKEY", "sentinel")
    monkeypatch.delitem(sys.modules, "agents.agent", raising=False)
    result = hallucination_probe.run_model_smoke(env={})
    assert result == {"skipped": "no_api_key"}
    assert "agents.agent" not in sys.modules
    # 默认路径：env=None，tmp_path 下无 .env、环境键清空 → 同样 skipped（自动发现空路径）。
    for key in (
        "APIKEY",
        "EVOHARNESS_API_KEY",
        "API",
        "EVOHARNESS_API_BASE",
        "MODEL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    result_default = hallucination_probe.run_model_smoke()
    assert result_default == {"skipped": "no_api_key"}
    assert "agents.agent" not in sys.modules
