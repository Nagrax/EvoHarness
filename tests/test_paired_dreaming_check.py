"""EVO-05 离线配对实验回归：同输入两臂确定性（哈希复现）、显式采用链、成本记录为真实台账格式。

harness：eval/paired_dreaming_check.py——off 臂（无夜间对账）vs on 臂（Dreaming 单轮 +
显式采用），合成知识库 + 确定性 fake source reader，不触网、不调真实模型。
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from eval import paired_dreaming_check as pdc
from evoharness.governance import budget
from evoharness.models import Base

DATE = "2026-11-21"


def _arm_file(tmp_path, sub, arm):
    """双臂产物在 <out>/<tag>-<digest8>/arm-dreaming-<on|off>.json（按运行隔离）。"""
    run_dirs = [p for p in (tmp_path / sub).iterdir() if p.is_dir()]
    assert len(run_dirs) == 1, run_dirs
    return json.loads((run_dirs[0] / f"arm-dreaming-{arm}.json").read_text(
        encoding="utf-8"))


def test_rerun_determinism_and_arm_difference(tmp_path):
    """同一输入两轮重跑：双臂产物哈希一致（确定性）；on/off 两臂哈希不同（臂有效）。"""
    first = pdc.run_experiment(out_dir=tmp_path / "a", tag="t", date_str=DATE)
    second = pdc.run_experiment(out_dir=tmp_path / "b", tag="t", date_str=DATE)
    assert first["input_digest"] == second["input_digest"]
    for arm in ("dreaming-off", "dreaming-on"):
        assert first["arms"][arm]["artifact_sha256"] == \
            second["arms"][arm]["artifact_sha256"], arm
    assert first["arms"]["dreaming-on"]["artifact_sha256"] != \
        first["arms"]["dreaming-off"]["artifact_sha256"]


def test_dreaming_arm_produces_candidates_and_explicit_adoption(tmp_path):
    """on 臂：漂移项产候选并经显式采用 superseded；off 臂：零候选、主张原样。"""
    outcome = pdc.run_experiment(out_dir=tmp_path / "r", tag="t", date_str=DATE)
    on_art = _arm_file(tmp_path, "r", "on")
    off_art = _arm_file(tmp_path, "r", "off")
    assert on_art["counts"]["candidates"] >= 1
    assert on_art["counts"]["adoptions"] == on_art["counts"]["candidates"]
    assert on_art["counts"]["superseded"] >= 1
    assert off_art["counts"]["candidates"] == 0
    assert off_art["counts"]["adoptions"] == 0
    assert off_art["counts"]["superseded"] == 0
    # off 臂所有主张保持 active；on 臂漂移主张被 supersede、新主张值来自漂移源
    assert all(c["state"] == "active" for c in off_art["claims"])
    superseded = [c for c in on_art["claims"] if c["state"] == "superseded"]
    assert len(superseded) == 1
    successors = [c for c in on_art["claims"] if c["supersedes_key"]]
    assert len(successors) == 1
    assert successors[0]["value"] == "5"  # 漂移源的当前值，不猜答案
    assert all(c["state"] == "active" for c in on_art["claims"]
               if c not in superseded and c not in successors)


def test_cost_records_follow_real_ledger_format(tmp_path):
    """成本记录 = 真实预算台账（budget.reserve/settle）的规范化投影，格式断言。"""
    outcome = pdc.run_experiment(out_dir=tmp_path / "r", tag="t", date_str=DATE)
    on_cost = outcome["arms"]["dreaming-on"]["cost"]
    off_cost = outcome["arms"]["dreaming-off"]["cost"]
    assert off_cost == {"records": [], "total_tokens": 0, "by_usage_status": {}}
    assert on_cost["records"]
    for rec in on_cost["records"]:
        assert set(rec) == {"category", "call_id", "kind", "reserved_tokens",
                            "used_tokens", "usage_status", "intent_status", "outcome"}
        assert rec["category"] == "dreaming" and rec["kind"] == "call"
        assert rec["reserved_tokens"] > 0 and rec["used_tokens"] >= 0
        assert rec["usage_status"] in {"recorded", "zero", "unknown", "not_applicable"}
        assert rec["intent_status"] in {"committed", "executing", "recorded",
                                        "unknown", "cancelled"}
        assert rec["outcome"] in {"ok", "failed", "cancelled"}
    assert on_cost["total_tokens"] == sum(r["used_tokens"] for r in on_cost["records"])
    assert on_cost["total_tokens"] > off_cost["total_tokens"]
    assert set(on_cost["by_usage_status"]) <= {"recorded", "zero", "unknown",
                                               "not_applicable"}


def test_ledger_cost_records_normalize_real_rows():
    """ledger_cost_records 直接读真实 BudgetReservation 行做规范化（非自造格式）。"""
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    run_id = "dreaming-nightly|2026-11-21|support"
    with factory.begin() as session:
        row = budget.reserve(session, run_id=run_id, category="dreaming",
                             tokens=340, call_id="dreaming|support|claim-1")
        budget.settle(session, row.reservation_id, used_tokens=340,
                      usage_status="recorded")
        records = pdc.ledger_cost_records(session, run_id=run_id)
    engine.dispose()
    assert len(records) == 1
    rec = records[0]
    assert rec == {"category": "dreaming", "call_id": "dreaming|support|claim-1",
                   "kind": "call", "reserved_tokens": 340, "used_tokens": 340,
                   "usage_status": "recorded", "intent_status": "recorded",
                   "outcome": "ok"}


def test_comparison_outputs_written_and_parseable(tmp_path):
    """comparison.json / comparison.md 落盘：双臂哈希、候选/成本差值、md 含双臂哈希。"""
    outcome = pdc.run_experiment(out_dir=tmp_path / "r", tag="t", date_str=DATE)
    run_dir = tmp_path / "r" / f"t-{outcome['input_digest'][:8]}"
    comparison = json.loads((run_dir / "comparison.json").read_text(encoding="utf-8"))
    assert set(comparison["arms"]) == {"dreaming-off", "dreaming-on"}
    for arm, bundle in comparison["arms"].items():
        assert bundle["artifact_sha256"] == outcome["arms"][arm]["artifact_sha256"]
        assert bundle["counts"]["claims"] >= 3
    assert comparison["delta"]["candidates_on_minus_off"] >= 1
    assert comparison["delta"]["tokens_on_minus_off"] > 0
    md = (run_dir / "comparison.md").read_text(encoding="utf-8")
    for arm in ("dreaming-off", "dreaming-on"):
        assert outcome["arms"][arm]["artifact_sha256"][:16] in md
        assert arm in md
    assert comparison["input_digest"] in md
    for name in ("arm-dreaming-on.json", "arm-dreaming-off.json",
                 "comparison.json", "comparison.md"):
        assert (run_dir / name).is_file()


def test_off_arm_baseline_report_is_none(tmp_path):
    """off 臂不做夜间对账：report 为 None（诚实表达"没跑"，不是空报告）。"""
    pdc.run_experiment(out_dir=tmp_path / "r", tag="t", date_str=DATE)
    off_art = _arm_file(tmp_path, "r", "off")
    assert off_art["report"] is None
    on_art = _arm_file(tmp_path, "r", "on")
    assert on_art["report"] is not None
    assert on_art["report"]["counts"]["completed"] is True
