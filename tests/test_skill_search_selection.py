"""DE-33 选择回归：确定性活动前沿、加权父代抽样、搜索冠军与生产隔离。

对应 tasks.json DE-33 acceptance_scenarios 第 1/3/4 条与
SKILL-EVOLUTION-SEARCH §10.3/§10.4，S1 输入直接取
``docs/digital-employee/search-design-fixtures.json`` 的 frontier_example：

S1（fixtures A/B/C/D 例）：
1. 前沿 = {C, D}、权重 C:D = 2:1、概率 2/3 与 1/3、搜索冠军 = C；池保留全部
   A/B/C/D（历史不删除）；
2. 低均分互补候选仍能探索：D 均分 0.8 < C 的 0.833，仍凭 Case-3 领先进入前沿；
3. 前沿不是完整 Pareto 集、不是均分 top-k：追加候选 E（Pareto 非支配、均分
   高于 D、仅领先 Case-1）仍被排除——看 Case 领先覆盖贡献，不是看支配关系
   或均分排名；领先 Case-2 的 B 也因覆盖贡献为零被排除。

S3（确定性）：
4. 池输入排列与 Case 完成顺序（向量键序）不改变前沿/权重/概率/冠军；
5. RNG 状态恢复后给出同一父代序列；seed 相同 → 序列相同；
6. 无权重（空前沿/全零权重）不除零、不采样，返回明确原因。

S4（作用域边界）：
7. pool/selection 仅产出一次优化任务的结构化记录；池落盘只进 DE-29 搜索
   命名空间（`<evolution>/search/<tenant>/<space>/<lineage>/<search_id>/`），
   生产扫描根（~/.evoharness/skills 与 <cwd>/.evoharness/skills）不含池路径，
   discover_skills 看不到任何池条目；
8. 池条目只存 hash/引用/分数向量（无候选正文），不改生产 experimental 技能
   文件，不写 usage/tier 表（usage.jsonl / deployment_tiers.json /
   tier_audit.jsonl 不出现），champion 结果不含任何 tier/晋级字段。

全部离线（fixtures + 合成评分向量），不依赖真实模型。
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents import skill_tiers as st
from agents.skill_search import pool as pl
from agents.skill_search import selection as se
from agents.skills import discover_skills, reset_skill_cache

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = json.loads(
    (REPO_ROOT / "docs" / "digital-employee" / "search-design-fixtures.json").read_text(encoding="utf-8")
)

EVOLUTION_DIR_ENVS = ("EVOHARNESS_EVOLUTION_DIR", "EVOHARNESS_SKILLS_ROOT")

FRONTIER_SPEC = FIXTURES["frontier_example"]
CASE_IDS = list(FRONTIER_SPEC["case_ids"])          # case-1/2/3
CANDIDATE_SCORES = {c["id"]: list(c["scores"]) for c in FRONTIER_SPEC["candidates"]}
EXPECTED = FRONTIER_SPEC["expected"]


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    for env in EVOLUTION_DIR_ENVS:
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("EVOHARNESS_IR_MODE", "off")
    reset_skill_cache()
    st.reset_tier_cache()
    yield tmp_path
    reset_skill_cache()
    st.reset_tier_cache()


def _build_fixture_pool(*, search_id: str = "s-de33-frontier") -> pl.CandidatePool:
    """按 fixtures 建池：A 为种子，B/C/D 依次以直接父代逐 Case 比较入池。"""
    pool = pl.CandidatePool(search_id=search_id, case_ids=CASE_IDS, epsilon=FIXTURES["epsilon"])
    seeded = pool.register_seed(
        candidate_id="A",
        score_vector=dict(zip(CASE_IDS, CANDIDATE_SCORES["A"])),
        content_hash="bundle-A",
        protocol_hash="proto-1",
    )
    assert seeded["ok"] is True, seeded
    chain = [("B", "A"), ("C", "B"), ("D", "C")]
    for cid, parent in chain:
        result = pool.admit(
            candidate_id=cid,
            parent_id=parent,
            score_vector=dict(zip(CASE_IDS, CANDIDATE_SCORES[cid])),
            content_hash=f"bundle-{cid}",
            protocol_hash="proto-1",
        )
        assert result["ok"] is True, (cid, result)
    return pool


# ── S1：fixtures A/B/C/D 例 ──────────────────────────────────────────────────


def test_s1_fixture_frontier_weights_and_champion():
    pool = _build_fixture_pool()
    frontier = se.compute_active_frontier(pool.candidates, case_ids=CASE_IDS, epsilon=FIXTURES["epsilon"])
    assert frontier["ok"] is True
    assert frontier["frontier"] == EXPECTED["frontier"] == ["C", "D"]
    assert frontier["weights"] == EXPECTED["weights"] == {"C": 2, "D": 1}
    for cid, expected_p in (("C", 2 / 3), ("D", 1 / 3)):
        assert abs(frontier["probabilities"][cid] - expected_p) < 1e-12
    assert frontier["weight_fractions"] == EXPECTED["probabilities"] == {"C": "2/3", "D": "1/3"}
    # 池保留全部历史（A/B 不因不领先而删除）
    assert [c["candidate_id"] for c in pool.usable_candidates()] == EXPECTED["pool_retains"]
    assert frontier["lead_sets"]["C"] == ["case-1", "case-2"]
    assert frontier["lead_sets"]["D"] == ["case-3"]

    champion = se.select_search_champion(pool.candidates, case_ids=CASE_IDS, epsilon=FIXTURES["epsilon"])
    assert champion["ok"] is True
    assert champion["search_champion"] == EXPECTED["search_champion"] == "C"
    # 冠军从全池选：A/B 也在参与排名中
    assert [r["candidate_id"] for r in champion["ranking"]] == ["C", "D", "B", "A"]


def test_s1_low_mean_complementary_candidate_still_explored_by_sampler():
    pool = _build_fixture_pool()
    frontier = se.compute_active_frontier(pool.candidates, case_ids=CASE_IDS)
    scores = {c["candidate_id"]: c["average_score"] for c in pool.usable_candidates()}
    # D 均分低于 C，仍进入前沿并获得 1/3 探索概率（凭 Case-3 领先）
    assert scores["D"] < scores["C"]
    assert "D" in frontier["frontier"]
    assert frontier["weights"]["D"] == 1
    # B 领先 Case-2 但覆盖贡献为 0（C 已覆盖），不进前沿
    assert "case-2" in frontier["lead_sets"]["B"]
    assert "B" not in frontier["frontier"]

    sampler = se.ParentSampler(seed=20261114)
    draws = {sampler.draw(frontier)["parent_id"] for _ in range(64)}
    assert draws == {"C", "D"}  # 只在活动前沿上抽父代


def test_s1_frontier_is_not_pareto_set_nor_mean_topk():
    """追加候选 E：Pareto 非支配、均分高于 D，仅领先 Case-1 → 仍被前沿排除。"""
    pool = _build_fixture_pool()
    e_scores = [1.0, 0.9, 0.55]  # E vs C: case-3 更高；E vs D: case-1/2 更高 → 无支配
    admitted = pool.admit(
        candidate_id="E",
        parent_id="A",  # E 支配直接父代 A → 合法入池
        score_vector=dict(zip(CASE_IDS, e_scores)),
        content_hash="bundle-E",
        protocol_hash="proto-1",
    )
    assert admitted["ok"] is True
    scores = {c["candidate_id"]: c["average_score"] for c in pool.usable_candidates()}
    assert scores["E"] > scores["D"]  # 均分 top-3 若按均分应含 E

    frontier = se.compute_active_frontier(pool.candidates, case_ids=CASE_IDS)
    assert frontier["ok"] is True
    assert frontier["frontier"] == ["C", "D"]
    # E 是 Pareto 非支配（无候选支配它），仍不在前沿 → 前沿 ⊊ Pareto 集
    vectors = {c["candidate_id"]: c["score_vector"] for c in pool.usable_candidates()}
    for other, vec in vectors.items():
        if other == "E":
            continue
        dominated = all(vec[cid] >= vectors["E"][cid] for cid in CASE_IDS) and any(
            vec[cid] > vectors["E"][cid] for cid in CASE_IDS
        )
        assert not dominated, f"{other} 不应支配 E"
    assert "E" not in frontier["frontier"]
    # 均分高于 D 的 E 被排除 → 选择不看均分排名，看 Case 领先覆盖贡献
    assert frontier["weights"] == {"C": 2, "D": 1}


# ── S3：确定性 ───────────────────────────────────────────────────────────────


def test_s3_pool_input_permutation_and_case_order_do_not_change_results():
    baseline_pool = _build_fixture_pool(search_id="s-perm-base")
    baseline_frontier = se.compute_active_frontier(baseline_pool.candidates, case_ids=CASE_IDS)
    baseline_champion = se.select_search_champion(baseline_pool.candidates, case_ids=CASE_IDS)

    # 1) 打乱池条目列表顺序（同 created_sequence，仅呈现顺序不同）
    shuffled_records = list(reversed(copy.deepcopy(baseline_pool.candidates)))
    perm_frontier = se.compute_active_frontier(shuffled_records, case_ids=CASE_IDS)
    perm_champion = se.select_search_champion(shuffled_records, case_ids=CASE_IDS)
    assert perm_frontier["frontier"] == baseline_frontier["frontier"]
    assert perm_frontier["weights"] == baseline_frontier["weights"]
    assert perm_frontier["probabilities"] == baseline_frontier["probabilities"]
    assert perm_champion["search_champion"] == baseline_champion["search_champion"]

    # 2) 重建池：每次 admit 时向量键序打乱（模拟 Case 完成顺序不同）
    other_pool = pl.CandidatePool(search_id="s-perm-other", case_ids=CASE_IDS, epsilon=FIXTURES["epsilon"])
    case_cycle = [CASE_IDS[2], CASE_IDS[0], CASE_IDS[1]]
    assert other_pool.register_seed(
        candidate_id="A",
        score_vector={cid: CANDIDATE_SCORES["A"][CASE_IDS.index(cid)] for cid in case_cycle},
        content_hash="bundle-A",
        protocol_hash="proto-1",
    )["ok"] is True
    for cid, parent in [("B", "A"), ("C", "B"), ("D", "C")]:
        case_cycle = case_cycle[1:] + case_cycle[:1]  # 每代轮转 Case 完成顺序
        result = other_pool.admit(
            candidate_id=cid,
            parent_id=parent,
            score_vector={cid_: CANDIDATE_SCORES[cid][CASE_IDS.index(cid_)] for cid_ in case_cycle},
            content_hash=f"bundle-{cid}",
            protocol_hash="proto-1",
        )
        assert result["ok"] is True, (cid, result)
    other_frontier = se.compute_active_frontier(other_pool.candidates, case_ids=CASE_IDS)
    other_champion = se.select_search_champion(other_pool.candidates, case_ids=CASE_IDS)
    assert other_frontier["frontier"] == baseline_frontier["frontier"]
    assert other_frontier["weights"] == baseline_frontier["weights"]
    assert other_frontier["probabilities"] == baseline_frontier["probabilities"]
    assert other_champion["search_champion"] == baseline_champion["search_champion"]

    # 3) 池 save/load 往返后结果不变（落盘不改变搜索语义）
    restored = pl.CandidatePool.from_dict(copy.deepcopy(baseline_pool.to_dict()))
    restored_frontier = se.compute_active_frontier(restored.candidates, case_ids=CASE_IDS)
    assert restored_frontier["frontier"] == baseline_frontier["frontier"]
    assert restored_frontier["weights"] == baseline_frontier["weights"]


def test_s3_rng_state_restore_gives_same_parent_sequence():
    frontier = se.compute_active_frontier(_build_fixture_pool().candidates, case_ids=CASE_IDS)

    sampler = se.ParentSampler(seed=42)
    first = sampler.draw(frontier)
    second = sampler.draw(frontier)
    checkpoint = sampler.get_state()  # 保存 RNG 状态
    third = sampler.draw(frontier)
    fourth = sampler.draw(frontier)

    restored = se.ParentSampler(state=checkpoint)  # 恢复到第二次抽取之后
    replay_third = restored.draw(frontier)
    replay_fourth = restored.draw(frontier)

    assert replay_third["parent_id"] == third["parent_id"]
    assert replay_fourth["parent_id"] == fourth["parent_id"]
    assert replay_third["random_value"] == third["random_value"]
    assert replay_third["draw_index"] == third["draw_index"] == 3
    assert replay_fourth["rng_state_after"]["internal_state"] == fourth["rng_state_after"]["internal_state"]
    # 相同 seed 从零开始 → 整条序列一致
    twin = se.ParentSampler(seed=42)
    assert [twin.draw(frontier)["parent_id"] for _ in range(4)] == [
        first["parent_id"], second["parent_id"], third["parent_id"], fourth["parent_id"],
    ]
    # 抽样记录完整状态（排序、算法版本、seed、draw index 随事务保存）
    for record in (first, second, third, fourth):
        assert record["sampler_version"] == se.SAMPLER_ALGORITHM_VERSION
        assert record["seed"] == 42
        assert record["rng_state_before"]["draw_index"] == record["draw_index"] - 1


def test_s3_zero_weight_does_not_divide():
    empty_frontier = se.compute_active_frontier([], case_ids=CASE_IDS)
    assert empty_frontier["ok"] is False
    assert empty_frontier["error"] == se.FRONTIER_EMPTY
    sampler = se.ParentSampler(seed=1)
    blocked = sampler.draw(empty_frontier)
    assert blocked["ok"] is False
    assert blocked["error"] == "frontier_not_ok"
    assert blocked["parent_id"] == ""

    pool = _build_fixture_pool()
    for record in pool.candidates:
        pool.mark_unusable(record["candidate_id"], reason="protocol invalidated")
    unusable_frontier = se.compute_active_frontier(pool.candidates, case_ids=CASE_IDS)
    assert unusable_frontier["ok"] is False
    draw = sampler.draw(unusable_frontier)
    assert draw["ok"] is False
    assert draw["error"] in {"frontier_not_ok", "no_weight_total_zero"}

    # 空冠军同样 fail-closed
    champion = se.select_search_champion([], case_ids=CASE_IDS)
    assert champion["ok"] is False
    assert champion["search_champion"] == ""

    # 合成的全零权重前沿直接打到抽样器：不抛 ZeroDivisionError
    zero = sampler.draw({"ok": True, "frontier": [], "weights": {}, "probabilities": {}})
    assert zero["ok"] is False
    assert zero["error"] == "no_weight_total_zero"

    # 合法非空池中 best_i 由池内 max 定义（恒有覆盖者）→ 前沿永远可全覆盖，
    # "数据不完整"只出现在空池/全不可用池（上面已断言），不会对部分覆盖降级采样
    partial_pool = pl.CandidatePool(search_id="s-partial", case_ids=["case-1", "case-2", "case-3"])
    assert partial_pool.register_seed(
        candidate_id="A",
        score_vector={"case-1": 1.0, "case-2": 1.0, "case-3": 0.0},
        content_hash="b-A",
        protocol_hash="p",
    )["ok"] is True
    assert partial_pool.admit(
        candidate_id="B",
        parent_id="A",
        score_vector={"case-1": 0.0, "case-2": 0.0, "case-3": 1.0},
        content_hash="b-B",
        protocol_hash="p",
    )["ok"] is True  # A/B 互不支配 → 都在池中
    partial_frontier = se.compute_active_frontier(partial_pool.candidates, case_ids=partial_pool.case_ids)
    assert partial_frontier["ok"] is True
    assert set(partial_frontier["frontier"]) == {"A", "B"}
    assert partial_frontier["weights"] == {"A": 2, "B": 1}


# ── S4：作用域边界（生产隔离） ───────────────────────────────────────────────


def _write_experimental_skill(root: Path) -> Path:
    skill_dir = root / ".evoharness" / "skills" / "legacy-experimental"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: legacy-experimental\ndescription: 原受限试用技能\n---\n\n# 步骤\n原行为保持不变。\n",
        encoding="utf-8",
    )
    return skill_dir


def test_s4_pool_entries_stay_out_of_production_discover_skills_usage_and_tiers(sandbox):
    skill_dir = _write_experimental_skill(sandbox)
    skill_file = skill_dir / "SKILL.md"
    before_text = skill_file.read_text(encoding="utf-8")
    reset_skill_cache()
    baseline_skills = [s.name for s in discover_skills()]
    assert baseline_skills == ["legacy-experimental"]

    pool = _build_fixture_pool(search_id="s-de33-isolation")
    frontier = se.compute_active_frontier(pool.candidates, case_ids=CASE_IDS)
    champion = se.select_search_champion(pool.candidates, case_ids=CASE_IDS)
    sampler = se.ParentSampler(seed=7)
    draws = [sampler.draw(frontier) for _ in range(8)]

    # 池记录只描述一次优化任务：仅 hash/引用/分数向量，不含候选正文
    payload = pool.to_dict()
    assert payload["candidates"] and payload["attempts"]
    for record in payload["candidates"]:
        assert "snapshot" not in record and "instructions" not in record
        assert set(record["score_vector"]) == set(CASE_IDS)

    # 落盘只进 DE-29 搜索命名空间；生产扫描根不含池路径
    evolution_dir = sandbox / ".evoharness" / "skill-evolution"
    namespace = evolution_dir / "search" / "tenant-x" / "space-y" / "lineage-z" / "s-de33-isolation"
    saved = pool.save(namespace)
    assert saved["ok"] is True
    pool_path = Path(saved["path"])
    assert pool_path == namespace / "pool.json"
    production_roots = (
        (Path.home() / ".evoharness" / "skills").resolve(),
        (sandbox / ".evoharness" / "skills").resolve(),
    )
    assert not pool_path.is_relative_to(production_roots[0])
    assert not pool_path.is_relative_to(production_roots[1])
    assert namespace.is_relative_to(evolution_dir / "search")

    # 生产 discover_skills 不受池影响：无新技能、无池条目、experimental 原样
    reset_skill_cache()
    after_skills = [s.name for s in discover_skills()]
    assert after_skills == ["legacy-experimental"]
    assert skill_file.read_text(encoding="utf-8") == before_text
    assert pl.CandidatePool.load(namespace).to_dict()["candidates"] == payload["candidates"]

    # 不写 usage/tier 表：池与选择全程无生产台账副作用
    assert not (evolution_dir / "usage.jsonl").exists()
    assert not (evolution_dir / "deployment_tiers.json").exists()
    assert not (evolution_dir / "tier_audit.jsonl").exists()
    assert st.get_tier_map() == {}
    for record in payload["candidates"]:
        assert st.get_tier(record["candidate_id"]) == st.DEFAULT_TIER

    # champion 展示复用原 variant ID 语义，不含任何生产 tier/晋级字段
    assert champion["search_champion"] == "C"
    assert champion["search_champion"] in {r["candidate_id"] for r in pool.candidates}
    champion_payload = json.dumps(champion, ensure_ascii=False)
    assert "tier" not in champion_payload
    assert "promotion" not in champion_payload
    for draw in draws:
        assert draw["parent_id"] in frontier["frontier"]
