"""E-04 回归：纵向进化实验的离线协议（划分防泄漏 / 计划 / 曲线 / CI / blocked 语义）。

真实模型运行需 API 预算，本文件只测零模型的离线部分：任务家族划分不得泄漏、计划字段齐备、
曲线与 CI 可复算、无预算时 run 明确报 blocked（不伪造结果）。
"""

from __future__ import annotations

import asyncio
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import eval.evolution_experiment as ee  # noqa: E402


def _item(tid, q):
    return {"task_id": tid, "Question": q, "answer": "x"}


def _pool(n=40):
    # 词汇高度分散，避免"题目之间天然近重复"压垮 holdout 选取（近重复过滤本身另有专测）。
    rng = random.Random(0)
    out = []
    for i in range(n):
        tokens = [f"w{rng.randrange(10 ** 7)}" for _ in range(14)]
        out.append(_item(f"t{i}", "query " + " ".join(tokens)))
    return out


def test_split_is_deterministic():
    pool = _pool(30)
    a = ee.split_task_stream(pool, holdout_size=8, seed=7)
    b = ee.split_task_stream(pool, holdout_size=8, seed=7)
    assert [x["task_id"] for x in a["holdout"]] == [x["task_id"] for x in b["holdout"]]
    assert [x["task_id"] for x in a["stream"]] == [x["task_id"] for x in b["stream"]]


def test_split_has_no_leakage_between_stream_and_holdout():
    pool = _pool(50)
    split = ee.split_task_stream(pool, holdout_size=10, seed=3)
    holdout_qs = [ee._question_of(h) for h in split["holdout"]]
    assert len(split["holdout"]) == 10
    for s in split["stream"]:
        for hq in holdout_qs:
            assert not ee.is_near_duplicate(ee._question_of(s), hq)
    # 两集不相交。
    assert not ({x["task_id"] for x in split["stream"]} & {x["task_id"] for x in split["holdout"]})


def test_split_rejects_near_duplicate_into_holdout():
    pool = [
        _item("a", "the capital city of france is paris"),
        _item("a2", "the capital city of france is paris"),          # 精确重复 → 去重
        _item("b", "what is the capital city of france"),
        _item("c", "compute the integral of x squared"),
        _item("d", "list the planets of the solar system"),
    ]
    split = ee.split_task_stream(pool, holdout_size=3, seed=1)
    holdout_qs = [ee._question_of(h) for h in split["holdout"]]
    # holdout 内部无近重复。
    for i in range(len(holdout_qs)):
        for j in range(i + 1, len(holdout_qs)):
            assert not ee.is_near_duplicate(holdout_qs[i], holdout_qs[j])
    # 精确重复只保留一份。
    ids = [x["task_id"] for x in split["stream"] + split["holdout"]]
    assert "a2" not in ids


def test_jaccard_and_near_duplicate_bounds():
    assert ee.jaccard("abc", "abc") == 1.0
    assert ee.is_near_duplicate("hello world", "hello world")
    assert not ee.is_near_duplicate("hello world", "completely different text here")


def test_build_plan_fields_and_budget():
    plan = ee.build_plan(dataset="gaia", n_rounds=12, tasks_per_round=25, holdout_size=25, stream_size=103)
    assert plan["arms"] == list(ee.ARMS)
    assert plan["checkpoints"] == [6, 12]
    assert plan["judge_version"] == ee.JUDGE_VERSION
    assert plan["simulated_user_feedback"] is True
    assert plan["budget_estimate"]["tokens_stream"] > 0
    assert plan["budget_estimate"]["tokens_holdout"] > 0


def test_curve_from_rounds_computes_ci_and_delta():
    rounds = [
        {"round": 1, "arm": "gated_evolution", "correct_flags": [1, 0, 1, 1], "tokens_M": 10.0},
        {"round": 2, "arm": "gated_evolution", "correct_flags": [1, 1, 1, 1], "tokens_M": 12.0},
        {"round": 1, "arm": "no_evolution", "correct_flags": [0, 0, 0, 0], "tokens_M": 9.0},
        {"round": 2, "arm": "no_evolution", "correct_flags": [1, 0, 0, 0], "tokens_M": 9.5},
    ]
    curve = ee.curve_from_rounds(rounds)
    assert len(curve["points"]) == 4
    assert curve["delta_first_to_last"]["gated_evolution"] == 25.0   # 75 → 100
    assert curve["delta_first_to_last"]["no_evolution"] == 25.0      # 0 → 25
    gated = [p for p in curve["points"] if p["arm"] == "gated_evolution"]
    assert gated[0]["pass_at_1"] == 75.0 and gated[0]["ci95"] is not None


def _fam_item(tid, q, pt, lvl):
    return {"task_id": tid, "Question": q, "answer": "x", "problem_type": pt, "Level": lvl}


def test_family_key_and_assign_families():
    it = {"task_id": "a", "problem_type": "file", "Level": 1}
    assert ee.family_label(it) == "file:L1"
    assert ee.assign_families([it]) == {"a": "file:L1"}
    # 无 Level 时退化为纯 problem_type。
    assert ee.family_label({"task_id": "b", "problem_type": "text"}) == "text"


def test_split_by_families_is_family_disjoint_and_leak_free():
    rng = random.Random(1)
    pool = []
    for fam, (pt, lvl) in enumerate([("file", 1), ("text", 2), ("mm", 1)]):
        for i in range(6):
            q = "q " + " ".join(f"w{rng.randrange(10 ** 7)}" for _ in range(12))
            pool.append(_fam_item(f"t{fam}-{i}", q, pt, lvl))
    split = ee.split_by_families(pool, stream_families=2, holdout_size=4)
    sf = {ee.family_label(it) for it in split["stream"]}
    hf = {ee.family_label(it) for it in split["holdout"]}
    assert hf and not (sf & hf)          # 流与 holdout 的家族不相交
    assert len(split["holdout"]) == 4
    for s in split["stream"]:            # 无跨集近重复（防泄漏）
        for h in split["holdout"]:
            assert not ee.is_near_duplicate(ee._question_of(s), ee._question_of(h))


def test_simulated_feedback_names_family_and_ground_truth():
    msg = ee.simulated_feedback(
        {"task_id": "a", "answer": "42", "problem_type": "file", "Level": 2}, "gaia"
    )
    assert "simulated user feedback" in msg
    assert "file:L2" in msg and "42" in msg


def test_evolve_from_agent_uses_extract_now_when_ok(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    class _A:
        async def extract_now(self, hint=""):
            skill = tmp_path / ".evoharness" / "skills" / "s1"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("# s1\n", encoding="utf-8")
            return {"ok": True}

        def _recent_dialog_messages(self, *, max_messages=8):
            raise AssertionError("extract_now 成功时不应走回退")

        def _build_side_query(self, max_tokens=0):
            raise AssertionError("extract_now 成功时不应走回退")

    out = asyncio.run(ee._evolve_from_agent(_A(), "fb"))
    assert out["via"] == "extract_now" and out["ok"] is True
    assert out["write_observed"] is True and out["skills_after"] == 1


def test_evolve_from_agent_falls_back_to_online_ingest():
    """pilot3 证据：extract_now 会报 no pending window 并静默空转；必须走回退而不是放弃。"""

    class _FakeAgent:
        use_openai = True

        def __init__(self):
            self._openai_messages = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]

        async def extract_now(self, hint=""):
            return {"ok": False, "error": "no pending online skill extraction window"}

        def _recent_dialog_messages(self, *, max_messages=8):
            return [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}][-max_messages:]

        def _build_side_query(self, max_tokens=0):
            return None  # 无模型配置 → 回退被触发但无法继续（可判定，不是静默）

        async def _confirm_online_skill_write(self, summary):
            return True

    out = asyncio.run(ee._evolve_from_agent(_FakeAgent(), "feedback"))
    assert out["via"] == "online_ingest"          # 回退路径确实被走到
    assert out["ok"] is False and "side_query" in out["error"]


def test_is_balance_error_detects_429_and_402():
    assert ee._is_balance_error({"error": "Error code: 429 - {'error': {'message': 'insufficient balance'}}"})
    assert ee._is_balance_error({"error": "HTTP 402 payment required"})
    assert ee._is_balance_error({"error": "insufficient_quota"})
    assert not ee._is_balance_error({"error": ""})
    assert not ee._is_balance_error({"error": "watchdog: task exceeded 600s, abandoned"})


def test_run_is_blocked_without_model_budget(tmp_path, monkeypatch, capsys):
    import agents.main as main_mod

    monkeypatch.setattr(main_mod, "_resolve_api_config", lambda _v: (None, None, False))
    code = ee._cmd_run(ee.argparse.Namespace(dataset="gaia"))
    assert code == 3
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["status"] == "blocked"
    assert "offline_available" in payload


def test_plan_cli_writes_offline(tmp_path, monkeypatch, capsys):
    data = tmp_path / "gaia.json"
    data.write_text(json.dumps(_pool(60)), encoding="utf-8")
    monkeypatch.setattr(ee, "DATA_FILES", {"gaia": data})
    monkeypatch.setattr(ee, "RESULTS_DIR", tmp_path / "evolution")
    ns = ee.argparse.Namespace(
        dataset="gaia", rounds=6, tasks_per_round=10, holdout=8, seed=5,
    )
    assert ee._cmd_plan(ns) == 0
    written = json.loads((tmp_path / "evolution" / "plan.json").read_text(encoding="utf-8"))
    assert written["split"]["holdout"] == 8
    assert written["split"]["holdout_task_ids"]
