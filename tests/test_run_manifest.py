"""W01 回归：运行级不可变数据与完整分母（run manifest / run_id 专属产物 / resume 校验 / score 分母恢复）。

复现并钉死的旧缺陷（eval/run_benchmark.py 改版前）：
- 同 tag 共用一个 predictions.jsonl：两次运行记录互相混入；模型/配置变化后 resume 无校验。
- manifest 在运行结束后才生成：中断后 planned 只能用 attempted 近似，未跑题从分母消失。
- score 以记录数当 planned；CI 只重采样已尝试记录（计划 3/对 1 → CI [100,100] 假区间）。
- 重复记录可抬高分类计数与分母（同一正确题记两次 → 200%）。
- 旧 tag 共享产物（predictions.jsonl）必须保持只读兼容，不被新代码改写。

全部用临时目录 + monkeypatch run_task（零模型、零网络、零 API 请求）端到端跑 run/score 路径。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import eval.run_benchmark as rb  # noqa: E402

DATA_3 = [
    {"task_id": "t1", "Question": "q1", "answer": "cat"},
    {"task_id": "t2", "Question": "q2", "answer": "dog"},
    {"task_id": "t3", "Question": "q3", "answer": "bird"},
]
DATA_5 = DATA_3 + [
    {"task_id": "t4", "Question": "q4", "answer": "fish"},
    {"task_id": "t5", "Question": "q5", "answer": "sun"},
]

RUN_DEFAULTS = dict(limit=None, workers=2, no_fold=False, max_turns=40, timeout_s=900, fold_threshold=None)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _install_bench(tmp_path, monkeypatch, data):
    data_file = tmp_path / "data.json"
    data_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(rb, "DATA_FILES", {"gaia": data_file, "hle": data_file})
    monkeypatch.setattr(rb, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(rb, "SCRATCH_DIR", tmp_path / "runs" / "scratch")
    monkeypatch.setenv("MODEL", "test-model")


def _record(item, **over):
    base = {
        "task_id": str(item.get("task_id")),
        "question": str(item.get("Question") or "")[:500],
        "gold": str(item.get("answer")),
        "prediction": str(item.get("answer")),
        "has_final_marker": True,
        "timed_out": False,
        "error": "",
        "folds": 0,
        "tokens": {"input": 10, "output": 5},
        "latency_s": 0.1,
        "problem_type": "p",
        "level": 1,
        "answer_type": "",
    }
    base.update(over)
    return base


def _fake_run_task(monkeypatch, *, fail_ids=(), overrides=None, calls=None):
    """替换 run_task：按题单返回合成记录；fail_ids 注入执行器崩溃（模拟中断，零 API）。"""
    overrides = overrides or {}

    async def fake_run_task(item, dataset, **kwargs):
        tid = str(item.get("task_id"))
        if calls is not None:
            calls.append(tid)
        if tid in fail_ids:
            raise RuntimeError(f"injected executor crash: {tid}")
        return _record(item, **overrides.get(tid, {}))

    monkeypatch.setattr(rb, "run_task", fake_run_task)


def _launch(**kwargs):
    return asyncio.run(rb.run("gaia", **{**RUN_DEFAULTS, **kwargs}))


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _interrupted_run_id(tmp_path) -> str:
    """中断的 run 不会写 latest_run.json；run_store 里恰好只有一个进行中的 run。"""
    store = tmp_path / "runs" / "gaia" / rb.RUN_STORE_DIR
    run_ids = sorted(d.name for d in store.iterdir() if d.is_dir())
    assert len(run_ids) == 1
    return run_ids[0]


# ── 验收1：同 tag 两次运行互不混入记录；中断后按配置校验续跑 ─────────────────


def test_manifest_frozen_before_first_task(tmp_path, monkeypatch):
    """manifest（planned/题单/协议指纹/分母政策/平台不可用规则）在首题之前已冻结落盘。"""
    _install_bench(tmp_path, monkeypatch, DATA_3)
    _fake_run_task(monkeypatch)
    _launch(limit=1)
    tag_dir = tmp_path / "runs" / "gaia"
    run_id = json.loads((tag_dir / "latest_run.json").read_text(encoding="utf-8"))["run_id"]
    mf = json.loads((tag_dir / rb.RUN_STORE_DIR / run_id / "manifest.json").read_text(encoding="utf-8"))
    # run_store 内 manifest 是首题前冻结的原始清单：完成后不回写（status 保持 running），
    # 完成态由 runs/<run_id>.json 不可变最终清单承载。
    assert mf["status"] == "running"
    assert "completed_at" not in mf and "predictions_sha256" not in mf
    assert mf["budget"]["planned"] == 1
    assert mf["data"]["task_ids"] == ["t1"]
    assert mf["protocol_fingerprint"]
    assert mf["primary_attempt_policy"] == rb.PRIMARY_ATTEMPT_POLICY
    assert mf["platform_unavailable_rule"]["regex"] == rb.PLATFORM_UNAVAILABLE_RE.pattern
    assert mf["data"]["sha256"] == rb._sha256_file(tmp_path / "data.json")
    final = json.loads((tag_dir / "runs" / f"{run_id}.json").read_text(encoding="utf-8"))
    assert final["metrics"]["planned"] == 1 and final["metrics"]["attempted"] == 1


def test_two_runs_same_tag_do_not_mix(tmp_path, monkeypatch):
    """同 tag 第二次运行（题单变化）必须新 run_id：各自产物隔离，先跑 run 的产物字节不变。"""
    _install_bench(tmp_path, monkeypatch, DATA_3)
    _fake_run_task(monkeypatch)
    _launch(limit=1)  # run A
    tag_dir = tmp_path / "runs" / "gaia"
    run_a = json.loads((tag_dir / "latest_run.json").read_text(encoding="utf-8"))["run_id"]
    file_a = tag_dir / rb.RUN_STORE_DIR / run_a / "predictions.jsonl"
    bytes_a = file_a.read_bytes()
    manifest_a_bytes = (tag_dir / rb.RUN_STORE_DIR / run_a / "manifest.json").read_bytes()

    _launch(limit=2)  # run B：题单变化 → 协议指纹不同 → 新 run，不得混入 A 的记录
    run_b = json.loads((tag_dir / "latest_run.json").read_text(encoding="utf-8"))["run_id"]
    assert run_b != run_a
    recs_b = _read_jsonl(tag_dir / rb.RUN_STORE_DIR / run_b / "predictions.jsonl")
    assert {r["task_id"] for r in recs_b} == {"t1", "t2"}
    assert all(r["run_id"] == run_b for r in recs_b)
    assert file_a.read_bytes() == bytes_a
    assert (tag_dir / rb.RUN_STORE_DIR / run_a / "manifest.json").read_bytes() == manifest_a_bytes
    final_a = json.loads((tag_dir / "runs" / f"{run_a}.json").read_text(encoding="utf-8"))
    assert final_a["metrics"]["attempted"] == 1

    rb.score("gaia")
    summary = json.loads((tag_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["provenance"]["source_run_id"] == run_b
    assert summary["planned"] == 2 and summary["attempted"] == 2


def test_rerun_same_config_after_completion_creates_new_run(tmp_path, monkeypatch):
    """同配置重复运行：已完成的 run 不可续跑，新 run 独立产物，旧产物字节不变。"""
    _install_bench(tmp_path, monkeypatch, DATA_3[:1])
    _fake_run_task(monkeypatch)
    _launch()
    tag_dir = tmp_path / "runs" / "gaia"
    id_a = json.loads((tag_dir / "latest_run.json").read_text(encoding="utf-8"))["run_id"]
    file_a = tag_dir / rb.RUN_STORE_DIR / id_a / "predictions.jsonl"
    bytes_a = file_a.read_bytes()
    _launch()
    id_b = json.loads((tag_dir / "latest_run.json").read_text(encoding="utf-8"))["run_id"]
    assert id_b != id_a
    assert file_a.read_bytes() == bytes_a
    recs_b = _read_jsonl(tag_dir / rb.RUN_STORE_DIR / id_b / "predictions.jsonl")
    assert [r["task_id"] for r in recs_b] == ["t1"]
    assert all(r["run_id"] == id_b for r in recs_b)


def test_resume_after_interrupt_same_config_continues(tmp_path, monkeypatch):
    """中断后同配置续跑：同一 run_id、不重跑已完成题、补齐剩余题并冻结最终 manifest。"""
    _install_bench(tmp_path, monkeypatch, DATA_3)
    calls: list[str] = []
    _fake_run_task(monkeypatch, fail_ids={"t2", "t3"}, calls=calls)
    with pytest.raises(RuntimeError):
        _launch()
    tag_dir = tmp_path / "runs" / "gaia"
    run_id = _interrupted_run_id(tmp_path)
    mf = json.loads((tag_dir / rb.RUN_STORE_DIR / run_id / "manifest.json").read_text(encoding="utf-8"))
    assert mf["status"] == "running"
    assert mf["budget"]["planned"] == 3  # manifest 在首题之前已冻结，中断后 planned 仍可恢复
    pred_file = tag_dir / rb.RUN_STORE_DIR / run_id / "predictions.jsonl"
    assert [r["task_id"] for r in _read_jsonl(pred_file)] == ["t1"]

    calls.clear()
    _fake_run_task(monkeypatch, calls=calls)
    _launch()
    assert json.loads((tag_dir / "latest_run.json").read_text(encoding="utf-8"))["run_id"] == run_id
    assert [r["task_id"] for r in _read_jsonl(pred_file)] == ["t1", "t2", "t3"]
    assert calls == ["t2", "t3"]  # 断点续跑不重复执行已完成的 t1
    final = json.loads((tag_dir / "runs" / f"{run_id}.json").read_text(encoding="utf-8"))
    assert final["status"] == "completed"
    m = final["metrics"]
    assert m["planned"] == 3 and m["attempted"] == 3 and m["not_attempted"] == 0


def test_resume_rejects_config_mismatch(tmp_path, monkeypatch):
    """配置变化（协议指纹不同）后续跑：必须新 run，不继承旧记录、不改旧产物。"""
    _install_bench(tmp_path, monkeypatch, DATA_3)
    _fake_run_task(monkeypatch, fail_ids={"t2", "t3"})
    with pytest.raises(RuntimeError):
        _launch()
    tag_dir = tmp_path / "runs" / "gaia"
    run_a = _interrupted_run_id(tmp_path)
    store_a = tag_dir / rb.RUN_STORE_DIR / run_a
    bytes_a = (store_a / "predictions.jsonl").read_bytes()
    manifest_a_bytes = (store_a / "manifest.json").read_bytes()

    _fake_run_task(monkeypatch)
    _launch(timeout_s=800)  # timeout 变化 → 指纹不同 → 新 run
    run_b = json.loads((tag_dir / "latest_run.json").read_text(encoding="utf-8"))["run_id"]
    assert run_b != run_a
    assert (store_a / "predictions.jsonl").read_bytes() == bytes_a
    assert (store_a / "manifest.json").read_bytes() == manifest_a_bytes
    recs_b = _read_jsonl(tag_dir / rb.RUN_STORE_DIR / run_b / "predictions.jsonl")
    assert sorted(r["task_id"] for r in recs_b) == ["t1", "t2", "t3"]
    assert all(r["run_id"] == run_b for r in recs_b)


def test_resume_rejects_corrupt_or_foreign_trace(tmp_path, monkeypatch):
    """续跑校验：轨迹含非法 JSON 或题单外记录时显式报错，绝不静默混合。"""
    _install_bench(tmp_path, monkeypatch, DATA_3)
    _fake_run_task(monkeypatch, fail_ids={"t2", "t3"})
    with pytest.raises(RuntimeError):
        _launch()
    tag_dir = tmp_path / "runs" / "gaia"
    run_id = _interrupted_run_id(tmp_path)
    pred_file = tag_dir / rb.RUN_STORE_DIR / run_id / "predictions.jsonl"

    with pred_file.open("a", encoding="utf-8") as f:
        f.write("{not-json\n")
    _fake_run_task(monkeypatch)
    with pytest.raises(ValueError, match="非法 JSON"):
        _launch()

    lines = [x for x in pred_file.read_text(encoding="utf-8").splitlines() if x.strip() and x != "{not-json"]
    pred_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pred_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(_record({"task_id": "ghost", "answer": "x"}), ensure_ascii=False) + "\n")
    with pytest.raises(ValueError, match="题单外"):
        _launch()


# ── 验收2：planned=3、只写1道正确 → 主成功率 1/3；CI 保留未尝试失败；分类加总=planned ──


def test_score_recovers_planned_from_manifest_and_fills_not_attempted(tmp_path, monkeypatch):
    _install_bench(tmp_path, monkeypatch, DATA_3)
    _fake_run_task(monkeypatch, fail_ids={"t2", "t3"})
    with pytest.raises(RuntimeError):
        _launch()
    tag_dir = tmp_path / "runs" / "gaia"
    run_id = _interrupted_run_id(tmp_path)

    rb.score("gaia")
    summary = json.loads((tag_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["planned"] == 3  # planned 来自源 manifest，不是 attempted
    assert summary["attempted"] == 1
    assert summary["correct"] == 1
    assert summary["pass_at_1"] == 33.3  # 1/3：未尝试不消失
    assert summary["not_attempted"] == 2
    assert summary["category_sum"] == 3  # 分类加总对齐 planned
    assert summary["pass_at_1_ci95"] == rb.bootstrap_ci95([True, False, False])
    assert summary["pass_at_1_ci95"] != [100.0, 100.0]  # 不再是「只重采样已尝试」的假区间
    assert summary["task_categories"] == {"t1": "correct", "t2": "not_attempted", "t3": "not_attempted"}
    assert summary["planned_source"] == "run_manifest"
    prov = summary["provenance"]
    assert prov["source_run_id"] == run_id
    assert prov["source_manifest_sha256"]
    assert prov["predictions_sha256"]  # 评分绑定原始轨迹哈希


# ── 验收3：失败类别不从分母消失；平台不可用有预先规则/题单/证据；重评分不改原始文件 ──


def test_failure_categories_stay_in_denominator_with_platform_evidence(tmp_path, monkeypatch):
    _install_bench(tmp_path, monkeypatch, DATA_5)
    overrides = {
        "t2": {"timed_out": True},
        "t3": {"error": "boom"},
        "t4": {"has_final_marker": False},
        "t5": {"error": "403 team_model_access_denied"},
    }
    _fake_run_task(monkeypatch, overrides=overrides)
    _launch()
    tag_dir = tmp_path / "runs" / "gaia"
    run_id = json.loads((tag_dir / "latest_run.json").read_text(encoding="utf-8"))["run_id"]

    rb.score("gaia")
    s = json.loads((tag_dir / "summary.json").read_text(encoding="utf-8"))
    assert s["planned"] == 5
    assert s["timed_out"] == 1 and s["errored"] == 1 and s["no_answer"] == 1
    assert s["platform_unavailable"] == 1 and s["not_attempted"] == 0
    assert s["effective_denominator"] == 4  # 5 - 1 平台不可用；超时/错误/无答案仍在分母
    assert s["pass_at_1"] == 25.0  # 1 正确 / 4
    assert s["category_sum"] == 5  # 分类加总对齐 planned
    assert s["platform_unavailable_tasks"] == [{"task_id": "t5", "error": "403 team_model_access_denied"}]
    mf = json.loads((tag_dir / rb.RUN_STORE_DIR / run_id / "manifest.json").read_text(encoding="utf-8"))
    assert mf["platform_unavailable_rule"]["regex"] == rb.PLATFORM_UNAVAILABLE_RE.pattern
    assert mf["data"]["task_ids"] == ["t1", "t2", "t3", "t4", "t5"]


def test_rescoring_never_modifies_original_artifacts(tmp_path, monkeypatch):
    """重评分与旧 tag 兼容：原始轨迹/manifest/最终清单字节不变；旧共享产物只读可评分。"""
    _install_bench(tmp_path, monkeypatch, DATA_3)
    _fake_run_task(monkeypatch)
    _launch()
    tag_dir = tmp_path / "runs" / "gaia"
    run_id = json.loads((tag_dir / "latest_run.json").read_text(encoding="utf-8"))["run_id"]
    store_run = tag_dir / rb.RUN_STORE_DIR / run_id
    before = {
        "predictions": _sha(store_run / "predictions.jsonl"),
        "manifest": _sha(store_run / "manifest.json"),
        "final_manifest": _sha(tag_dir / "runs" / f"{run_id}.json"),
    }
    rb.score("gaia")
    rb.score("gaia")
    after = {
        "predictions": _sha(store_run / "predictions.jsonl"),
        "manifest": _sha(store_run / "manifest.json"),
        "final_manifest": _sha(tag_dir / "runs" / f"{run_id}.json"),
    }
    assert before == after  # 重评分不改原始文件
    assert len(list((tag_dir / "scores").glob("*.json"))) == 2  # 每次评分生成新派生物

    legacy_dir = tmp_path / "runs" / "gaia-nofold"
    legacy_dir.mkdir(parents=True)
    legacy_records = [_record({"task_id": "x1", "answer": "cat"}), _record({"task_id": "x2", "answer": "dog"})]
    (legacy_dir / "predictions.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in legacy_records) + "\n", encoding="utf-8"
    )
    legacy_bytes = (legacy_dir / "predictions.jsonl").read_bytes()
    rb.score("gaia")  # gaia 循环同时覆盖 gaia-nofold tag（旧产物只读兼容）
    assert (legacy_dir / "predictions.jsonl").read_bytes() == legacy_bytes
    legacy_summary = json.loads((legacy_dir / "summary.json").read_text(encoding="utf-8"))
    assert legacy_summary["planned_source"] == "records_fallback"
    assert legacy_summary["planned"] == 2


# ── 验收4：重复记录不抬高分母；唯一产物能反查每题 ────────────────────────────


def test_duplicate_attempts_never_inflate_denominator(tmp_path, monkeypatch):
    _install_bench(tmp_path, monkeypatch, DATA_3[:2])  # planned = t1, t2
    _fake_run_task(monkeypatch, overrides={"t1": {"prediction": "dog"}})  # t1 首次答错
    _launch()
    tag_dir = tmp_path / "runs" / "gaia"
    run_id = json.loads((tag_dir / "latest_run.json").read_text(encoding="utf-8"))["run_id"]
    pred_file = tag_dir / rb.RUN_STORE_DIR / run_id / "predictions.jsonl"
    assert [r["task_id"] for r in _read_jsonl(pred_file)] == ["t1", "t2"]

    # 注入 t1 的「更好」重试（答对，带真实 attempt 元数据，模拟 harness 写入的第二次尝试）：
    # 主尝试必须仍是首次记录（first_recorded），禁止事后挑最好。
    retry = _record({"task_id": "t1", "answer": "cat"})
    retry["run_id"] = run_id
    retry["attempt_id"] = f"{run_id}-a0003"
    with pred_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(retry, ensure_ascii=False) + "\n")

    rb.score("gaia")
    s = json.loads((tag_dir / "summary.json").read_text(encoding="utf-8"))
    assert s["count"] == 3  # 原始记录数（重复保留可审计）
    assert s["attempts_total"] == 3 and s["duplicate_attempts"] == 1
    assert s["attempted"] == 2 and s["planned"] == 2  # 分母不因重复抬高
    assert s["correct"] == 1  # t1 主尝试=首次（错），t2 对
    assert s["pass_at_1"] == 50.0  # 1/2，不是 2/3 也不是 200%
    assert s["category_sum"] == 2
    assert s["task_categories"]["t1"] == "incorrect"
    assert s["task_categories"]["t2"] == "correct"

    # 唯一产物反查每题：manifest 题单 ↔ task_categories 一一对应；轨迹含 run_id/attempt_id。
    mf = json.loads((tag_dir / rb.RUN_STORE_DIR / run_id / "manifest.json").read_text(encoding="utf-8"))
    assert set(s["task_categories"]) == set(mf["data"]["task_ids"])
    final = json.loads((tag_dir / "runs" / f"{run_id}.json").read_text(encoding="utf-8"))
    assert final["run_id"] == run_id
    recs_all = _read_jsonl(pred_file)
    assert all(r.get("attempt_id") and r.get("run_id") == run_id for r in recs_all)


def test_score_hard_fails_on_unknown_task_and_corrupt_json(tmp_path, monkeypatch):
    _install_bench(tmp_path, monkeypatch, DATA_3[:1])
    _fake_run_task(monkeypatch)
    _launch()
    tag_dir = tmp_path / "runs" / "gaia"
    pred_file = tag_dir / rb.RUN_STORE_DIR / json.loads(
        (tag_dir / "latest_run.json").read_text(encoding="utf-8")
    )["run_id"] / "predictions.jsonl"

    with pred_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(_record({"task_id": "ghost", "answer": "x"}), ensure_ascii=False) + "\n")
    with pytest.raises(ValueError, match="不在源 manifest 题单中"):
        rb.score("gaia")
    lines = pred_file.read_text(encoding="utf-8").splitlines()
    pred_file.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    with pred_file.open("a", encoding="utf-8") as f:
        f.write("{broken json\n")
    with pytest.raises(ValueError, match="非法 JSON"):
        rb.score("gaia")


def test_summarize_rejects_duplicate_manifest_task_ids():
    with pytest.raises(ValueError, match="重复 task_id"):
        rb.summarize_records([], "gaia", planned_task_ids=["a", "a"])


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
