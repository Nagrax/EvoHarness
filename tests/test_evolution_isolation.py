"""W02 回归：冻结划分、holdout 只读、污染登记与三臂开关。

这些测试不调用真实模型；它们锁住的是「实验协议」本身：只要划分/只读/开关被改坏，
测试就会失败，而不是等到一次昂贵的 live run 之后才发现泄漏。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from eval import evolution_experiment as ee
from agents import online_skill_eval as oe
from agents import skill_evolution as se
from agents import skill_tiers as st


def _item(task_id: str, family_index: int, index: int, *, sentinel: str = "") -> dict:
    question = f"question {task_id} unique-{family_index}-{index} " + " ".join(
        f"w{family_index}-{index}-{j}" for j in range(8)
    )
    if sentinel:
        question += f" {sentinel}"
    return {
        "task_id": task_id,
        "Question": question,
        "answer": f"answer-{task_id}",
        "problem_type": f"pt{family_index}",
        "Level": 1,
    }


def _pool(families: int = 6, per_family: int = 4, *, sentinel_families: set[int] | None = None) -> list[dict]:
    sentinel_families = sentinel_families or set()
    out = []
    for family in range(families):
        for index in range(per_family):
            sentinel = "HOLDOUT-SENTINEL" if family in sentinel_families else ""
            out.append(_item(f"t{family}-{index}", family, index, sentinel=sentinel))
    return out


def _contamination(path: Path, items: list[dict], task_ids: list[str] | None = None) -> dict:
    return ee.register_contamination(
        task_ids or [],
        reason="test contamination",
        evidence=["test"],
        path=path,
        items=items,
    )


def test_split_manifest_is_deterministic_and_append_only(tmp_path):
    items = _pool()
    path = tmp_path / "split-manifest.json"
    contamination = _contamination(tmp_path / "contamination.json", items)
    manifest = ee.load_or_create_split_manifest(
        path,
        items,
        dataset="gaia",
        seed=7,
        stream_families=2,
        holdout_size=2,
        promotion_size=2,
        dev_ratio=0.25,
        contamination=contamination,
    )
    counts = manifest["counts"]
    assert counts[ee.SPLIT_TRAIN] > 0
    assert counts[ee.SPLIT_DEV] > 0
    assert counts[ee.SPLIT_PROMOTION] > 0
    assert counts[ee.SPLIT_HOLDOUT] > 0
    assert sum(counts.values()) == len(manifest["splits"])
    assert manifest["manifestSha256"] == ee.compute_manifest_sha256(manifest)

    frozen = {task_id: dict(entry) for task_id, entry in manifest["splits"].items()}
    initial_revision = manifest["revision"]
    reloaded = ee.load_or_create_split_manifest(
        path,
        items,
        dataset="gaia",
        seed=7,
        stream_families=2,
        holdout_size=2,
        promotion_size=2,
        dev_ratio=0.25,
        contamination=contamination,
    )
    assert {task_id: dict(entry) for task_id, entry in reloaded["splits"].items()} == frozen

    # 新增样本只能追加到既有 train 家族，既有 dev/promotion/final 身份不能移动。
    train_family = next(entry["family"] for entry in frozen.values() if entry["split"] == ee.SPLIT_TRAIN)
    new_item = _item("t-new-train", 0, 99)
    new_item["problem_type"] = train_family.split(":")[0]
    new_item["Level"] = int(train_family.rsplit("L", 1)[1])
    grown = ee.load_or_create_split_manifest(
        path,
        items + [new_item],
        dataset="gaia",
        seed=7,
        stream_families=2,
        holdout_size=2,
        promotion_size=2,
        dev_ratio=0.25,
        contamination=contamination,
    )
    for task_id, entry in frozen.items():
        assert grown["splits"][task_id] == entry
    assert grown["splits"]["t-new-train"]["split"] == ee.SPLIT_TRAIN
    assert grown["revision"] == initial_revision + 1


def test_split_manifest_rejects_new_non_train_family(tmp_path):
    items = _pool()
    path = tmp_path / "split-manifest.json"
    contamination = _contamination(tmp_path / "contamination.json", items)
    manifest = ee.load_or_create_split_manifest(
        path,
        items,
        dataset="gaia",
        seed=7,
        stream_families=2,
        holdout_size=2,
        promotion_size=2,
        dev_ratio=0.25,
        contamination=contamination,
    )
    holdout_family = next(entry["family"] for entry in manifest["splits"].values() if entry["split"] == ee.SPLIT_HOLDOUT)
    new_item = _item("t-new-holdout-family", 99, 0)
    new_item["problem_type"] = holdout_family.split(":")[0]
    new_item["Level"] = int(holdout_family.rsplit("L", 1)[1])
    with pytest.raises(ee.SplitManifestDrift):
        ee.load_or_create_split_manifest(
            path,
            items + [new_item],
            dataset="gaia",
            seed=7,
            stream_families=2,
            holdout_size=2,
            promotion_size=2,
            dev_ratio=0.25,
            contamination=contamination,
        )


def test_holdout_snapshot_and_hash_guard(tmp_path, monkeypatch):
    arm = tmp_path / "ws" / "gated_evolution"
    cwd = arm / "cwd"
    (cwd / ".evoharness" / "skills" / "s1").mkdir(parents=True)
    (cwd / ".evoharness" / "skills" / "s1" / "SKILL.md").write_text("# s1\n", encoding="utf-8")
    (cwd / ".evoharness" / "skill-evolution").mkdir(parents=True)
    (cwd / ".evoharness" / "skill-evolution" / "usage.jsonl").write_text("{}\n", encoding="utf-8")
    home = arm / "home"
    (home / ".evoharness" / "sessions").mkdir(parents=True)
    (home / ".evoharness" / "sessions" / "s.json").write_text("{}", encoding="utf-8")

    original_before = ee._state_digest(cwd)
    snap = ee._prepare_holdout_workspace("gated_evolution", tmp_path / "ws", 1)
    assert (snap / "cwd" / ".evoharness" / "skills" / "s1" / "SKILL.md").is_file()
    assert (snap / "home" / ".evoharness" / "sessions" / "s.json").is_file()
    assert ee._state_digest(cwd) == original_before

    monkeypatch.setenv("EVOHARNESS_HOLDOUT_READONLY", "1")
    monkeypatch.chdir(snap / "cwd")
    snap_before = ee._state_digest(snap / "cwd")
    result = se.create_skill_file(name="new-skill", description="d", instructions="i")
    assert result["ok"] is False and "read-only" in result["error"]
    assert st.set_tier("s1", "core", trigger="test")["ok"] is False
    se.record_skill_load_issue(event="load_failed", file="x")
    se.record_skill_invocation(skill_name="s1", source="test", context="t")
    se.record_skill_usage_judgments([])
    se.record_usage_samples([], messages=[], latest_user="", latest_assistant="")
    assert ee._state_digest(snap / "cwd") == snap_before


def test_state_digest_detects_unguarded_write(tmp_path):
    cwd = tmp_path / "cwd"
    (cwd / ".evoharness" / "skills" / "s1").mkdir(parents=True)
    (cwd / ".evoharness" / "skills" / "s1" / "SKILL.md").write_text("# s1\n", encoding="utf-8")
    before = ee._state_digest(cwd)
    (cwd / ".evoharness" / "skills" / "s1" / "SKILL.md").write_text("# s1 changed\n", encoding="utf-8")
    assert ee._state_digest(cwd) != before


def test_sentinel_never_reaches_train_feedback_or_stream(tmp_path):
    items = _pool()
    base = ee.build_split_manifest(
        items,
        dataset="gaia",
        seed=11,
        stream_families=5,
        holdout_size=2,
        promotion_size=2,
        dev_ratio=0.25,
        contamination=_contamination(tmp_path / "contamination.json", items),
    )
    train_families = {
        entry["family"] for entry in base["splits"].values() if entry["split"] == ee.SPLIT_TRAIN
    }
    rest_family = next(family for family in {ee.family_label(it) for it in items} if family not in train_families)
    items = [
        {**item, "Question": item["Question"] + " HOLDOUT-SENTINEL"} if ee.family_label(item) == rest_family else item
        for item in items
    ]
    path = tmp_path / "split-manifest.json"
    contamination = _contamination(tmp_path / "contamination.json", items)
    manifest = ee.load_or_create_split_manifest(
        path,
        items,
        dataset="gaia",
        seed=11,
        stream_families=5,
        holdout_size=2,
        promotion_size=2,
        dev_ratio=0.25,
        contamination=contamination,
    )
    grouped = ee.manifest_items(manifest, items)
    stream_ids = {it["task_id"] for it in grouped[ee.SPLIT_TRAIN]}
    held_out = grouped[ee.SPLIT_PROMOTION] + grouped[ee.SPLIT_HOLDOUT]
    assert any("HOLDOUT-SENTINEL" in it["Question"] for it in held_out)
    assert all("HOLDOUT-SENTINEL" not in it["Question"] for it in grouped[ee.SPLIT_TRAIN])
    assert all("HOLDOUT-SENTINEL" not in it["Question"] for it in grouped[ee.SPLIT_DEV])
    for item in grouped[ee.SPLIT_TRAIN]:
        feedback = ee.simulated_feedback(item, "gaia")
        assert "HOLDOUT-SENTINEL" not in feedback
    assert not (stream_ids & {it["task_id"] for it in grouped[ee.SPLIT_HOLDOUT]})


def test_candidate_generation_sees_only_dev_failures():
    summary = {
        "failures": [
            {"split": "mutate_dev", "sample_id": "dev-1", "details": {"text": "dev failure"}},
            {"split": "promotion_test", "sample_id": "test-1", "details": {"text": "PROMOTION-SENTINEL"}},
        ]
    }
    filtered = oe._dev_only_rule_summary(summary)
    assert [item["sample_id"] for item in filtered["failures"]] == ["dev-1"]
    assert "PROMOTION-SENTINEL" not in json.dumps(filtered, ensure_ascii=False)


def test_arm_manifest_marks_actual_switches():
    manifest = ee._arm_manifest(
        ["no_evolution", "ungated_evolution", "gated_evolution"], gated_promotion=True
    )
    assert manifest["no_evolution"]["streamEvolve"] is False
    assert manifest["no_evolution"]["promotion"] == "disabled"
    assert manifest["ungated_evolution"]["streamEvolve"] is True
    assert manifest["ungated_evolution"]["irMode"] == "off"
    assert manifest["gated_evolution"]["irMode"] == "strict"
    assert manifest["gated_evolution"]["promotion"] == "apply_eval_promotions"
    disabled = ee._arm_manifest(["gated_evolution"], gated_promotion=False)
    assert disabled["gated_evolution"]["promotion"] == "disabled"


def test_evolve_from_agent_stops_when_evolution_disabled():
    class _A:
        def _online_evolution_enabled(self):
            return False

        async def extract_now(self, hint=""):
            raise AssertionError("disabled 时不应调用 extract_now")

    out = asyncio.run(ee._evolve_from_agent(_A(), "fb"))
    assert out["ok"] is False and out["via"] == "disabled"


def test_contamination_registry_excludes_family_from_new_holdout(tmp_path):
    items = _pool()
    contamination_path = tmp_path / "contamination.json"
    base = ee.build_split_manifest(
        items,
        dataset="gaia",
        seed=3,
        stream_families=3,
        holdout_size=3,
        promotion_size=2,
        dev_ratio=0.2,
        contamination=_contamination(contamination_path, items),
    )
    contaminated_task_id = next(
        task_id for task_id, entry in base["splits"].items() if entry["split"] == ee.SPLIT_HOLDOUT
    )
    contaminated_item = next(it for it in items if it["task_id"] == contaminated_task_id)
    contamination = ee.register_contamination(
        [contaminated_task_id],
        reason="historical pilot observed this holdout",
        evidence=["pilot"],
        path=contamination_path,
        items=items,
    )
    assert contaminated_item["task_id"] in contamination["taskIds"]
    assert ee.family_label(contaminated_item) in contamination["families"]

    manifest = ee.build_split_manifest(
        items,
        dataset="gaia",
        seed=3,
        stream_families=3,
        holdout_size=3,
        promotion_size=2,
        dev_ratio=0.2,
        contamination=contamination,
    )
    assert contaminated_item["task_id"] not in manifest["splits"]
    assert all(
        entry["family"] != ee.family_label(contaminated_item)
        for entry in manifest["splits"].values()
        if entry["split"] in (ee.SPLIT_HOLDOUT, ee.SPLIT_PROMOTION)
    )
    assert json.loads(contamination_path.read_text(encoding="utf-8"))["sha256"] == contamination["sha256"]


def test_cmd_run_wires_frozen_split_holdout_readonly_and_arms(tmp_path, monkeypatch):
    """无模型冒烟：验证 run 真的用了 manifest、只读 holdout 与三臂开关。"""
    import agents.main as main_mod

    items = _pool()
    monkeypatch.setattr(ee, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(ee, "_load_items", lambda dataset: items)
    monkeypatch.setattr(main_mod, "_resolve_api_config", lambda _v: ("http://fake", "key", True))
    monkeypatch.setattr(ee, "_seed_workspace", lambda arm, ws: (ws / arm / "cwd" / ".evoharness" / "skills").mkdir(parents=True, exist_ok=True))
    monkeypatch.setattr(ee, "_apply_gated_promotion", lambda arm, ws: {"ok": True, "arm": arm})

    calls = []

    def fake_run_single(item, dataset, arm, ws_root, *, max_turns, timeout_s, evolve, holdout=False, holdout_dir=None):
        calls.append({"arm": arm, "evolve": evolve, "holdout": holdout, "dir": holdout_dir})
        rec = {
            "task_id": item["task_id"],
            "family": ee.family_label(item),
            "correct": True,
            "tokens": {"input": 1, "output": 1},
            "side_query_tokens": {"input": 0, "output": 0},
            "latency_s": 0.1,
            "timed_out": False,
            "error": "",
            "skills_before": 0,
            "skills_after": 0,
        }
        if holdout:
            rec["holdout_readonly"] = {"enabled": True, "ok": True, "before": {}, "after": {}}
        return rec

    monkeypatch.setattr(ee, "_run_single", fake_run_single)
    args = ee.argparse.Namespace(
        dataset="gaia", rounds=1, tasks_per_round=1, holdout=1, arms="no_evolution,ungated_evolution,gated_evolution",
        max_turns=1, timeout=5, seed=13, workspace=str(tmp_path / "ws"), families=True,
        stream_families=2, stream_offset=0, gated_promotion=True,
        split_manifest=str(tmp_path / "manifest.json"), contamination=str(tmp_path / "contamination.json"),
        promotion_size=1, dev_ratio=0.25, dup_threshold=0.8,
    )
    code = ee._cmd_run(args)
    assert code == 0
    curve = json.loads((tmp_path / "curve.json").read_text(encoding="utf-8"))
    assert curve["split_manifest"]["sha256"]
    assert curve["arms_manifest"]["no_evolution"]["streamEvolve"] is False
    assert curve["arms_manifest"]["gated_evolution"]["promotion"] == "apply_eval_promotions"
    assert curve["holdout_readonly_ok_by_arm"] == {
        "no_evolution": True, "ungated_evolution": True, "gated_evolution": True,
    }
    assert {call["arm"] for call in calls if call["holdout"]} == {
        "no_evolution", "ungated_evolution", "gated_evolution",
    }
    assert all(call["evolve"] is False for call in calls if call["holdout"])
    assert all(call["evolve"] is False for call in calls if call["arm"] == "no_evolution" and not call["holdout"])
    assert all(call["evolve"] is True for call in calls if call["arm"] != "no_evolution" and not call["holdout"])
    assert all(call["dir"] is not None and Path(call["dir"]).is_dir() for call in calls if call["holdout"])