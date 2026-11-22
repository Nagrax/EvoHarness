"""人工门工作台回归（eval/human_gate.py）。

覆盖点：为什么这些断言值得写——
- 这三条工具服务于 H1/H2/H3 人工门，最容易出现的坏情况是"看起来完成了"：
  记录缺元数据、判定不在固定词表、净耗时与墙钟矛盾、盲审任务单泄漏模型结论。
  所以每类都必须 fail-closed，且盲审材料必须真的盲。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "eval" / "human_gate.py"
sys.path.insert(0, str(REPO))

import eval.human_gate as hg  # noqa: E402


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(SCRIPT), *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


# ------------------------------------------------------------------ H1

def _h1_record(**over) -> dict:
    base = {
        "case_id": "case-001",
        "started_at": "2026-11-22T01:00:00+00:00",
        "ended_at": "2026-11-22T01:12:00+00:00",
        "net_seconds": 600,
        "operator": "op-A",
        "tooling": "browser",
        "notes": "",
    }
    base.update(over)
    return base


def test_h1_plan_writes_plan_with_required_fields(tmp_path: Path) -> None:
    plan = hg.h1_plan(tmp_path, None)
    assert plan["required_fields"] == list(hg.H1_TIMING_FIELDS)
    assert (tmp_path / "h1-plan.json").is_file()
    # 口径必须写明排除项，否则人工计时无法复现
    assert plan["timing_definition"]["exclude"]


def test_h1_check_accepts_valid_record(tmp_path: Path) -> None:
    p = tmp_path / "h1.json"
    p.write_text(
        json.dumps({"repo_revision": "abc", "records": [_h1_record(), _h1_record(case_id="c2")]}),
        encoding="utf-8",
    )
    out = hg.h1_check(p)
    assert out["sample_size"] == 2
    assert out["meets_min_sample"] is False  # 低于 10 条必须如实体现
    assert out["distinct_operators"] == 1


def test_h1_check_rejects_missing_field(tmp_path: Path) -> None:
    bad = _h1_record()
    del bad["operator"]
    p = tmp_path / "h1.json"
    p.write_text(json.dumps({"repo_revision": "abc", "records": [bad]}), encoding="utf-8")
    with pytest.raises(SystemExit, match="缺少字段 operator"):
        hg.h1_check(p)


def test_h1_check_rejects_net_seconds_exceeding_wall_clock(tmp_path: Path) -> None:
    # 净耗时大于墙钟跨度 = 数据自相矛盾，必须拒绝
    bad = _h1_record(net_seconds=7200)
    p = tmp_path / "h1.json"
    p.write_text(json.dumps({"repo_revision": "abc", "records": [bad]}), encoding="utf-8")
    with pytest.raises(SystemExit, match="时间自洽性不成立"):
        hg.h1_check(p)


def test_h1_check_rejects_missing_records_meta(tmp_path: Path) -> None:
    p = tmp_path / "h1.json"
    p.write_text(json.dumps({"records": [_h1_record()]}), encoding="utf-8")
    with pytest.raises(SystemExit, match="缺少记录级元数据"):
        hg.h1_check(p)


# ------------------------------------------------------------------ H2

def _attribution_fixture(tmp_path: Path, n: int = 4) -> Path:
    """造一个最小 attributions 目录，结构对齐 eval/runs/attribution-*。"""
    attr = tmp_path / "attr"
    for i in range(n):
        arm = "arm-A" if i % 2 == 0 else "arm-B"
        d = attr / "cases" / arm
        d.mkdir(parents=True, exist_ok=True)
        case = {
            "kind": "evo07-attribution-case",
            "case_id": f"qq-{i:04d}",
            "arm": arm,
            "role": "holdout",
            "grade": {"valid_json": True, "passed": False, "errors": ["unit_price"]},
            "proposal": {"category": "SKILL_ISSUE", "confidence": 0.9},
            "agreement": {},
        }
        (d / f"qq-{i:04d}.json").write_text(json.dumps(case, ensure_ascii=False), encoding="utf-8")
    return attr


def test_h2_plan_is_blind(tmp_path: Path) -> None:
    attr = _attribution_fixture(tmp_path)
    hg.h2_plan(attr, tmp_path, sample=0, seed=0)
    plan = json.loads((tmp_path / "h2-plan.json").read_text(encoding="utf-8"))
    assert plan["sampled"] == 4
    for item in plan["items"]:
        # 盲审核心：逐 case 材料里不能出现模型给的类别/置信度/建议动作
        assert "proposal" not in item
        assert "category" not in item
        assert "confidence" not in item
        # 允许选项本身要给出（审阅人必须知道可选词表），但不携带模型倾向
        assert item["allowed_verdicts"] == list(hg.ATTRIBUTION_CATEGORIES)
        assert set(item) == {
            "case_id", "arm", "role", "frozen_grade", "failure_evidence",
            "trace_ref", "case_sha256", "allowed_verdicts",
        }
    # 词表只应出现在 allowed_verdicts 里；若逐 case 出现其它类别值即为泄漏
    for item in plan["items"]:
        item_blob = json.dumps(
            {k: v for k, v in item.items() if k != "allowed_verdicts"}, ensure_ascii=False
        )
        for cat in hg.ATTRIBUTION_CATEGORIES:
            assert cat not in item_blob, f"{cat} 泄漏进 case {item['case_id']} 的盲审材料"


def test_h2_plan_sampling_is_deterministic_and_bounded(tmp_path: Path) -> None:
    attr = _attribution_fixture(tmp_path, n=8)
    out1 = tmp_path / "o1"
    out2 = tmp_path / "o2"
    hg.h2_plan(attr, out1, sample=3, seed=0)
    hg.h2_plan(attr, out2, sample=3, seed=0)
    p1 = json.loads((out1 / "h2-plan.json").read_text(encoding="utf-8"))
    p2 = json.loads((out2 / "h2-plan.json").read_text(encoding="utf-8"))
    assert p1["sampled"] == 3
    assert [i["case_id"] for i in p1["items"]] == [i["case_id"] for i in p2["items"]]
    assert (out1 / "h2-checklist.md").is_file()


def test_h2_check_computes_agreement(tmp_path: Path) -> None:
    attr = _attribution_fixture(tmp_path)
    review = tmp_path / "review.json"
    review.write_text(
        json.dumps(
            {
                "reviews": [
                    # 2 条与模型一致，1 条不一致，1 条未知 case
                    {"case_id": "qq-0000", "verdict": "SKILL_ISSUE",
                     "reviewer": "R", "reviewed_at": "2026-11-22T00:00:00+00:00", "rationale": "x"},
                    {"case_id": "qq-0002", "verdict": "SKILL_ISSUE",
                     "reviewer": "R", "reviewed_at": "2026-11-22T00:00:00+00:00", "rationale": "x"},
                    {"case_id": "qq-0001", "verdict": "CASE_ISSUE",
                     "reviewer": "R", "reviewed_at": "2026-11-22T00:00:00+00:00", "rationale": "x"},
                    {"case_id": "qq-9999", "verdict": "UNCERTAIN",
                     "reviewer": "R", "reviewed_at": "2026-11-22T00:00:00+00:00", "rationale": "x"},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    out = hg.h2_check(review, attr)
    ag = out["agreement"]
    assert ag["judged"] == 3
    assert ag["agree"] == 2
    assert ag["disagree"] == 1
    assert ag["unknown_case"] == 1
    assert ag["agreement_rate"] == pytest.approx(2 / 3, abs=1e-4)


def test_h2_check_rejects_verdict_outside_vocabulary(tmp_path: Path) -> None:
    review = tmp_path / "r.json"
    review.write_text(
        json.dumps({"reviews": [{"case_id": "c", "verdict": "MAYBE", "reviewer": "R",
                                 "reviewed_at": "2026-11-22T00:00:00+00:00", "rationale": "x"}]}),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="不在固定五类"):
        hg.h2_check(review, None)


def test_h2_check_rejects_empty_rationale(tmp_path: Path) -> None:
    # 无理由的判定不能当独立真值
    review = tmp_path / "r.json"
    review.write_text(
        json.dumps({"reviews": [{"case_id": "c", "verdict": "SKILL_ISSUE", "reviewer": "R",
                                 "reviewed_at": "2026-11-22T00:00:00+00:00", "rationale": "  "}]}),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="rationale 不能为空"):
        hg.h2_check(review, None)


def test_h2_check_tolerates_utf8_bom(tmp_path: Path) -> None:
    # PowerShell 写的文件常带 BOM，工具必须能吃
    review = tmp_path / "r.json"
    body = json.dumps({"reviews": [{"case_id": "c", "verdict": "SKILL_ISSUE", "reviewer": "R",
                                    "reviewed_at": "2026-11-22T00:00:00+00:00", "rationale": "x"}]})
    review.write_text(body, encoding="utf-8-sig")
    out = hg.h2_check(review, None)
    assert out["reviewed"] == 1


# ------------------------------------------------------------------ H3

def _make_daily_db(path: Path, sent_dates: list[tuple[str, str]], mode: str = "single_approval") -> None:
    conn = __import__("sqlite3").connect(path)
    with conn:
        conn.execute("create table daily_reports (logical_date text, status text, delivered_at text)")
        conn.execute("create table system_state (key text, value text)")
        conn.execute("insert into system_state values (?,?)", ("deployment_mode", mode))
        for logical, delivered in sent_dates:
            conn.execute("insert into daily_reports values (?,?,?)", (logical, "sent", delivered))
    conn.close()


def test_h3_check_counts_natural_days_and_consecutive_run(tmp_path: Path) -> None:
    db = tmp_path / "org.db"
    _make_daily_db(
        db,
        [
            ("2026-11-19", "2026-11-19 00:30:01.000000"),  # 自然
            ("2026-11-20", "2026-11-20 00:30:01.000000"),  # 自然
            ("2026-11-21", "2026-11-21 00:30:01.000000"),  # 自然
            ("2026-11-10", "2026-11-09 08:56:06.000000"),  # 补发，不计自然
        ],
    )
    out = hg.h3_check(db)
    assert out["deployment_mode"] == "single_approval"
    assert out["natural_days_total"] == 3
    assert out["backfill_days"] == 1
    assert out["consecutive_days"] == 3
    assert out["remaining_days"] == 4
    assert out["verdict"] == "in_progress"


def test_h3_check_reports_complete_at_seven(tmp_path: Path) -> None:
    db = tmp_path / "org.db"
    days = [f"2026-11-{d:02d}" for d in range(10, 17)]
    _make_daily_db(db, [(d, f"{d} 00:30:01.000000") for d in days])
    out = hg.h3_check(db)
    assert out["consecutive_days"] == 7
    assert out["remaining_days"] == 0
    assert out["verdict"] == "complete"


def test_h3_check_is_read_only(tmp_path: Path) -> None:
    # 只读核对：不能改动数据库（避免验收动作本身制造新证据）
    db = tmp_path / "org.db"
    _make_daily_db(db, [("2026-11-19", "2026-11-19 00:30:01.000000")])
    before = db.read_bytes()
    hg.h3_check(db)
    assert db.read_bytes() == before


# ------------------------------------------------------------------ CLI

def test_cli_h3_rejects_missing_db(tmp_path: Path) -> None:
    r = _run("h3-check", "--db", str(tmp_path / "nope.db"))
    assert r.returncode != 0
    assert "REJECTED" in (r.stderr + r.stdout)
