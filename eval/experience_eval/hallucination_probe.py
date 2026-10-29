"""M4 hallucination_probe：no-model grounding 探针 + 可选模型冒烟。

no-model 探针：8 轮 coding 域对话窗口 + 三档条目（real / fabricated_zero_overlap /
fabricated_low_overlap），判定一律走 M1 grounding_flag(entry, WINDOW_TEXT)，
captured = not grounded。硬阈值：fabrication_capture_rate_zero_overlap >= 0.9、
false_flag_rate <= 0.1；low_overlap 档只报告不设门（改写型编造超出 no-model
判定能力，交给评测层 no_unfounded_claims llm_binary 轨）。

fixture 防腐自检：M1 default_tokenize 可用时走独立 token 集交集复核
（零重叠条目确实零重叠、real 条目确有重叠）；缺失时 integrity 退化为与测量
同路径并如实标注（tokenizer_available=False）。fixture_integrity_ok=False → exit 3。

模型冒烟（--model-smoke，可选）：复用 harness 自身客户端，恰一次
extract_online_skill_candidate 调用；无 SDK/无 key/无 client 时退化为明确报告的 skip。
键解析：默认（env=None）= cwd 向上到 REPO_ROOT 的 .env 自动发现 + os.environ
（顺序镜像 agents/main.py:98-123 的 APIKEY/API 与 :380 的 MODEL）；
显式传入 env 映射（含 {}）时只看该映射——测试隔离入口。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

REPO_ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ""):
    sys.path.insert(0, str(REPO_ROOT))

ZERO_OVERLAP_CAPTURE_GATE = 0.9
FALSE_FLAG_GATE = 0.1

# 8 轮对话窗口（4 user + 4 assistant，coding 域）；条目三档的重叠分布以
# agents.skills._token_list 分词器实测锁定（real 9/14/13 个重叠 token、
# zero_overlap 0/0/0、low_overlap 7/8），tests/test_probe_fixture_integrity 复核。
FIXTURE_MESSAGES: list[dict[str, str]] = [
    {"role": "user", "content": "帮我修一下 utils/date.py 里时区转换的 bug，改完先跑 tests/test_date.py 这几个单测。"},
    {"role": "assistant", "content": "已修复：parse_utc 现在显式携带 tzinfo。运行 python -m pytest tests/test_date.py -q，4 个用例全部通过。"},
    {"role": "user", "content": "以后凡是改代码都要先用 git diff 复查一遍再提交。"},
    {"role": "assistant", "content": "记住了：提交前先用 git diff 逐块复查改动。本次 diff 已复查，只有时区转换一处修改。"},
    {"role": "user", "content": "测试失败的时候优先怀疑最近一次改动，不要一上来就怪测试环境。"},
    {"role": "assistant", "content": "明白。以后测试失败先检查最近改动，再考虑环境因素。"},
    {"role": "user", "content": "还有，拿不准的需求不要猜，先列成清单问清楚再动手。"},
    {"role": "assistant", "content": "好的：需求不明确时先列出问题清单向你确认，再开始实现。"},
]

WINDOW_TEXT: str = " ".join(message["content"] for message in FIXTURE_MESSAGES)


@dataclass
class ProbeCase:
    text: str
    kind: str  # "real" | "fabricated_zero_overlap" | "fabricated_low_overlap"
    basis: str = "explicit"
    confidence: float = 1.0


@dataclass
class ProbeReport:
    window_token_count: int
    real_count: int
    fabricated_zero_overlap_count: int
    fabricated_low_overlap_count: int
    real_flagged: int  # 被判 ungrounded 的真实条目数（误拦）
    fabricated_zero_overlap_flagged: int
    fabricated_low_overlap_flagged: int
    fabrication_capture_rate_zero_overlap: float
    fabrication_capture_rate_low_overlap: float
    false_flag_rate: float
    fixture_integrity_ok: bool
    tokenizer_available: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_probe_cases() -> list[ProbeCase]:
    """三档条目按 ground-truth 标签构造；zero/low 两档都是窗口里没有的事实。"""
    return [
        ProbeCase("改代码后先用 git diff 复查再提交", "real"),
        ProbeCase("测试失败优先怀疑最近一次改动而不是测试环境", "real"),
        ProbeCase("拿不准的需求先列成问题清单确认后再动手", "real"),
        ProbeCase("发布前执行数据库迁移脚本并备份对象存储", "fabricated_zero_overlap"),
        ProbeCase("容器镜像构建完成后推送到镜像仓库并触发灰度发布", "fabricated_zero_overlap"),
        ProbeCase("文档导航重组后需要重新生成站点索引", "fabricated_zero_overlap"),
        ProbeCase("测试失败时先运行全量测试套件确认是环境问题", "fabricated_low_overlap"),
        ProbeCase("提交前用 git status 检查暂存区即可无需逐块复查", "fabricated_low_overlap"),
    ]


def _load_m1() -> tuple[Callable[[str, str], bool], Callable[[str], list[str]] | None]:
    """惰性导入 M1 的 grounding_flag（必需）与 default_tokenize（可选诊断接口）。"""
    try:
        import agents.experience_standard as experience_standard
    except Exception as ex:
        raise RuntimeError(f"M1 agents.experience_standard 未就绪: {type(ex).__name__}: {ex}") from ex
    grounding_flag = getattr(experience_standard, "grounding_flag", None)
    if not callable(grounding_flag):
        raise RuntimeError("M1 agents.experience_standard.grounding_flag 缺失")
    tokenizer = getattr(experience_standard, "default_tokenize", None)
    return grounding_flag, (tokenizer if callable(tokenizer) else None)


def _capture_rate(flagged: int, total: int) -> float:
    return (flagged / total) if total else 0.0


def run_no_model_probe(*, grounding_flag: Callable[[str, str], bool] | None = None) -> ProbeReport:
    """三档条目全部经 grounding_flag(entry, WINDOW_TEXT) 布尔判定；captured = not grounded。

    grounding_flag=None 时惰性取 M1 的实现（同时取 default_tokenize 做 fixture 独立复核）；
    M1 缺失 → RuntimeError（CLI 捕获后 exit 2）。
    """
    tokenizer: Callable[[str], list[str]] | None = None
    if grounding_flag is None:
        grounding_flag, tokenizer = _load_m1()
    cases = build_probe_cases()
    real = [case for case in cases if case.kind == "real"]
    zero = [case for case in cases if case.kind == "fabricated_zero_overlap"]
    low = [case for case in cases if case.kind == "fabricated_low_overlap"]

    real_flagged = sum(1 for case in real if not grounding_flag(case.text, WINDOW_TEXT))
    zero_flagged = sum(1 for case in zero if not grounding_flag(case.text, WINDOW_TEXT))
    low_flagged = sum(1 for case in low if not grounding_flag(case.text, WINDOW_TEXT))

    if tokenizer is not None:
        # 独立路径复核：直接用 default_tokenize 的 token 集交集，不经 grounding_flag 的布尔逻辑。
        window_tokens = set(tokenizer(WINDOW_TEXT))
        fixture_integrity_ok = all(
            not (set(tokenizer(case.text)) & window_tokens) for case in zero
        ) and all(set(tokenizer(case.text)) & window_tokens for case in real)
    else:
        # tokenizer 缺失：integrity 退化为与测量同路径的自洽复核，tokenizer_available=False 如实标注。
        fixture_integrity_ok = zero_flagged == len(zero) and real_flagged == 0

    return ProbeReport(
        window_token_count=len(set(tokenizer(WINDOW_TEXT))) if tokenizer is not None else 0,
        real_count=len(real),
        fabricated_zero_overlap_count=len(zero),
        fabricated_low_overlap_count=len(low),
        real_flagged=real_flagged,
        fabricated_zero_overlap_flagged=zero_flagged,
        fabricated_low_overlap_flagged=low_flagged,
        fabrication_capture_rate_zero_overlap=_capture_rate(zero_flagged, len(zero)),
        fabrication_capture_rate_low_overlap=_capture_rate(low_flagged, len(low)),
        false_flag_rate=_capture_rate(real_flagged, len(real)),
        fixture_integrity_ok=fixture_integrity_ok,
        tokenizer_available=tokenizer is not None,
    )


def evaluate_check_gates(report: ProbeReport) -> list[str]:
    """--check 的硬门；fabrication_capture_rate_low_overlap 有意不在此设门（只报告不设门）。"""
    failures: list[str] = []
    if report.fabrication_capture_rate_zero_overlap < ZERO_OVERLAP_CAPTURE_GATE:
        failures.append(
            f"fabrication_capture_rate_zero_overlap={report.fabrication_capture_rate_zero_overlap:.3f} < {ZERO_OVERLAP_CAPTURE_GATE}"
        )
    if report.false_flag_rate > FALSE_FLAG_GATE:
        failures.append(f"false_flag_rate={report.false_flag_rate:.3f} > {FALSE_FLAG_GATE}")
    return failures


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                values[key] = value
    except OSError:
        pass
    return values


def _discover_env_file() -> dict[str, str]:
    """cwd 起逐级向上到 REPO_ROOT（cwd 不在 REPO_ROOT 下时到文件系统根）的 .env 迷你发现。"""
    current = Path.cwd().resolve()
    depth = 0
    while True:
        candidate = current / ".env"
        if candidate.is_file():
            return _read_env_file(candidate)
        if current == REPO_ROOT or current.parent == current or depth > 64:
            return {}
        current = current.parent
        depth += 1


def _resolve_smoke_keys(env: Mapping[str, str] | None) -> dict[str, str]:
    # env=None：.env 自动发现 + os.environ 兜底（镜像 main.py 启动时把 .env 载入 os.environ 的语义，
    # 已在环境里的键优先）；env 显式传入（含 {}）→ 只看该映射，不读 os.environ、不读任何 .env。
    if env is not None:
        return {str(key): str(value) for key, value in env.items()}
    merged = _discover_env_file()
    merged.update({str(key): str(value) for key, value in os.environ.items()})
    return merged


def _pick_env(merged: Mapping[str, str], *names: str) -> str:
    for name in names:
        value = str(merged.get(name, "") or "").strip()
        if value:
            return value
    return ""


def _ir_text_entries(record: dict[str, Any]) -> list[dict[str, Any]]:
    """experience-standard/v1 的 text 条目路径（与 B 校验器 item_paths 同款；
    decision_rules 的 when/then/because 与 knowledge_gaps 不算 text 条目）。"""
    paths = {
        "situation": ("goal", "triggers", "context", "entities"),
        "guidance": ("principles", "steps", "avoid"),
        "applicability": ("conditions", "prerequisites", "exceptions", "boundaries"),
        "outcome": ("expected", "success_signals", "failure_signals"),
        "evidence": ("observations", "counterexamples", "validation_methods"),
    }
    entries: list[dict[str, Any]] = []
    for section, fields in paths.items():
        block = record.get(section) if isinstance(record.get(section), dict) else {}
        for field_name in fields:
            values = block.get(field_name, [])
            if isinstance(values, list):
                entries.extend(value for value in values if isinstance(value, dict))
    return entries


def _summarize_ir_candidate(record: dict[str, Any]) -> dict[str, Any]:
    entries = _ir_text_entries(record)
    with_basis = [entry for entry in entries if entry.get("basis") in ("explicit", "inferred")]
    explicit = [entry for entry in with_basis if entry.get("basis") == "explicit"]
    summary: dict[str, Any] = {
        "extracted": True,
        "candidate_shape": "ir",
        "entries_total": len(entries),
        "entries_with_basis": len(with_basis),
    }
    if not explicit:
        summary["explicit_grounding_pass_rate"] = None
        return summary
    try:
        from agents.experience_standard import grounding_flag

        grounded = sum(1 for entry in explicit if grounding_flag(str(entry.get("text", "")), WINDOW_TEXT))
    except Exception as ex:
        summary["explicit_grounding_pass_rate"] = None
        summary["note"] = f"grounding_flag unavailable: {type(ex).__name__}: {ex}"
        return summary
    summary["explicit_grounding_pass_rate"] = grounded / len(explicit)
    return summary


def run_model_smoke(*, max_tokens: int = 2400, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """可选模型冒烟：复用 harness 自身客户端，恰一次 extract_online_skill_candidate 调用。

    输出：{"skipped": ...} | {"extracted": false} |
         {"extracted": true, "candidate_shape": "legacy|ir", "entries_total": n,
          "entries_with_basis": n, "explicit_grounding_pass_rate": float|None}
    """
    merged = _resolve_smoke_keys(env)
    api_key = _pick_env(merged, "APIKEY", "EVOHARNESS_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")
    if not api_key:
        return {"skipped": "no_api_key"}
    api_base = _pick_env(merged, "API", "EVOHARNESS_API_BASE", "OPENAI_BASE_URL", "ANTHROPIC_BASE_URL")
    model = _pick_env(merged, "MODEL") or "deepseek-chat"
    try:
        from agents.agent import Agent
    except Exception as ex:
        return {"skipped": f"agents_agent_import_failed: {type(ex).__name__}: {ex}"}
    # Anthropic 兼容 base（main.py:_is_anthropic_compatible_base_url 的简化镜像）走
    # anthropic_base_url 参数名，其余走 api_base（OpenAI-compatible）。
    anthropic_style = bool(api_base) and "/anthropic" in api_base.lower()
    agent = Agent(
        model=model,
        api_base=None if anthropic_style else (api_base or None),
        anthropic_base_url=api_base if anthropic_style else None,
        api_key=api_key,
    )
    side_query = agent._build_side_query(max_tokens=max_tokens)
    if side_query is None:
        # agent.py:_build_side_query 末尾 return None 的边角：既无 anthropic 也无 openai client。
        return {"skipped": "no_llm_client", "model": model}
    from agents.online_skill_evolution import extract_online_skill_candidate

    candidate = asyncio.run(
        extract_online_skill_candidate(messages=[dict(message) for message in FIXTURE_MESSAGES], side_query=side_query)
    )
    if candidate is None:
        # 候选为 None 属合法结果（证据弱返回空）。
        return {"extracted": False}
    payload = asdict(candidate)
    record = payload.get("ir_record") if isinstance(payload.get("ir_record"), dict) else None
    if record is not None:
        return _summarize_ir_candidate(record)
    # legacy 形状（M2 未落地）：evidence 是自由字符串，grounding 判定跳过并如实报告。
    evidence = payload.get("evidence")
    entries_total = len(evidence) if isinstance(evidence, list) else (1 if str(evidence or "").strip() else 0)
    return {
        "extracted": True,
        "candidate_shape": "legacy",
        "entries_total": entries_total,
        "entries_with_basis": 0,
        "explicit_grounding_pass_rate": None,
        "note": "legacy candidate：evidence 为自由字符串（M2 未落地），grounding 判定跳过",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hallucination_probe",
        description="M4 幻觉探针：no-model grounding 三档速率 + 可选模型冒烟",
    )
    parser.add_argument("--check", action="store_true", help="硬门：zero-overlap 捕获率>=0.9、false_flag<=0.1")
    parser.add_argument("--model-smoke", action="store_true", help="可选模型冒烟（恰一次候选抽取调用）")
    parser.add_argument("--max-tokens", type=int, default=2400, help="（--model-smoke）side query max_tokens")
    args = parser.parse_args(argv)

    if args.model_smoke:
        try:
            result = run_model_smoke(max_tokens=args.max_tokens)
        except Exception as ex:
            result = {"skipped": f"model_smoke_failed: {type(ex).__name__}: {ex}"}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.check:
            return 0 if "skipped" not in result else 1
        return 0

    try:
        report = run_no_model_probe()
    except RuntimeError as ex:
        print(f"CONFIG_ERROR: {ex}", file=sys.stderr)
        return 2
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    if not report.fixture_integrity_ok:
        print("FIXTURE_INTEGRITY: FAIL", file=sys.stderr)
        return 3
    if args.check:
        failures = evaluate_check_gates(report)
        for line in failures:
            print(f"GATE_FAIL: {line}", file=sys.stderr)
        return 1 if failures else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
