"""DE-34 持久搜索状态存储回归：generation CAS、租约、绑定核验与成本账本。

对应 tasks.json DE-34 implementation_steps 第 1 条（父代/候选/协议/RNG/待完成
Case/Attempt 全部落盘，幂等可恢复）与 acceptance_scenarios 第 2 条前半
（旧 generation 不能覆盖新状态）。全部离线（tmp 目录 + 固定时钟）。

R08 修复后新增：跨实例/跨进程原子性——claim_lease 与 generation CAS 的
「读 generation→比较→写」全程持命名空间 OS 文件锁，并发认领/并发保存恰好
一个成功；``_disk_generation`` 的读取时序任意交错都不影响结果；持有者崩溃
后锁由内核释放可接管。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.skill_search import pool as pl
from agents.skill_search import store as st
from agents.skill_search.contracts import build_search_manifest, search_namespace


SKILL_TEXT = """---
name: deploy-rollout
description: 部署发布独立 SOP
when_to_use: deploy release canary rollout
producer-kind: executable_sop
version: 1.0.0
---

## 工作流

1. 灰度 5% 观察 10 分钟。
2. 全量并回填结果。

## 安全边界

只读保护内容。
"""


@pytest.fixture()
def skill_dir(tmp_path):
    directory = tmp_path / "deploy-rollout"
    directory.mkdir()
    (directory / "SKILL.md").write_text(SKILL_TEXT, encoding="utf-8")
    return directory


@pytest.fixture()
def manifest(skill_dir):
    built = build_search_manifest(skill_dir=skill_dir, tenant="acme", space="ops")
    assert built["ok"] is True, built
    return built["manifest"]


def _namespace(tmp_path, manifest):
    return search_namespace(manifest, tmp_path / "evo")


# ── 初始化 / 恢复 / 原子落盘 ─────────────────────────────────────────────────


def test_initialize_then_load_roundtrip(tmp_path, manifest):
    ns = _namespace(tmp_path, manifest)
    store = st.SearchStore(ns)
    assert store.exists() is False
    state = store.initialize(manifest=manifest, case_ids=["case-1", "case-2"], config={"a": 1})
    assert state["phase"] == st.PHASE_DRAFT
    assert state["generation"] == 1
    assert state["case_ids"] == ["case-1", "case-2"]
    assert state["manifest_digest_value"] == st.manifest_digest(manifest)
    # 重复初始化拒绝（不覆盖既有搜索）
    with pytest.raises(st.StoreCorruption, match="state_already_exists"):
        st.SearchStore(ns).initialize(manifest=manifest, case_ids=["case-1"])
    # 重启恢复：内存副本清零后从磁盘读回，字段完整
    revived = st.SearchStore(ns)
    loaded = revived.load()
    assert loaded["search_id"] == manifest.search_id
    assert loaded["config"] == {"a": 1}
    assert loaded["baseline"]["cases"] == {}
    assert loaded["round"]["phase"] == st.ROUND_PHASE_NONE
    # 无残留临时文件（原子写不留半截文件）
    assert [p.name for p in ns.glob(".*tmp*")] == []


def test_load_missing_returns_none_and_corruption_detected(tmp_path, manifest):
    ns = _namespace(tmp_path, manifest)
    store = st.SearchStore(ns)
    assert store.load() is None
    ns.mkdir(parents=True, exist_ok=True)
    (ns / "state.json").write_text("{not-json", encoding="utf-8")
    with pytest.raises(st.StoreCorruption, match="state_unreadable"):
        st.SearchStore(ns).load()
    (ns / "state.json").write_text(
        json.dumps({"schema_version": 99, "search_id": "x"}), encoding="utf-8"
    )
    with pytest.raises(st.StoreCorruption, match="state_schema_newer"):
        st.SearchStore(ns).load()
    (ns / "state.json").write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    with pytest.raises(st.StoreCorruption, match="state_missing_fields"):
        st.SearchStore(ns).load()
    with pytest.raises(st.StoreCorruption, match="state_not_loaded"):
        st.SearchStore(ns).save()


# ── generation CAS：旧 worker 不能覆盖新状态（S2 前半） ───────────────────────


def test_stale_generation_cannot_overwrite_newer_state(tmp_path, manifest):
    ns = _namespace(tmp_path, manifest)
    worker_a = st.SearchStore(ns)
    worker_a.initialize(manifest=manifest, case_ids=["case-1"], config={})
    worker_b = st.SearchStore(ns)
    worker_b.load()

    # A 推进状态（phase 前进 + generation +1）
    worker_a.state["phase"] = st.PHASE_BASELINE
    worker_a.state["baseline"]["cases"]["case-1"] = {"status": "done", "outcome": {"score": 1.0}}
    worker_a.save()
    assert worker_a.state["generation"] == 2

    # B 仍持有旧 generation=1：盲写必须被 CAS 拒绝，磁盘状态不被覆盖
    worker_b.state["phase"] = st.PHASE_DRAFT  # 旧 worker 的过期决定
    with pytest.raises(st.StoreConflict) as excinfo:
        worker_b.save()
    assert excinfo.value.code == "stale_generation"
    assert excinfo.value.disk_generation == 2
    on_disk = json.loads((ns / "state.json").read_text(encoding="utf-8"))
    assert on_disk["phase"] == st.PHASE_BASELINE
    assert on_disk["baseline"]["cases"]["case-1"]["status"] == "done"
    # B 只能 reload 后在新状态上重新决策
    reloaded = worker_b.reload()
    assert reloaded["phase"] == st.PHASE_BASELINE
    assert reloaded["generation"] == 2


# ── worker 租约（两 worker 不并发推进；过期可接管） ───────────────────────────


def test_worker_lease_claim_renew_and_expiry_takeover(tmp_path, manifest):
    ns = _namespace(tmp_path, manifest)
    store = st.SearchStore(ns)
    store.initialize(manifest=manifest, case_ids=["case-1"], config={})
    now = 1000.0
    assert store.claim_lease("worker-a", now=now, lease_seconds=300) is True
    other = st.SearchStore(ns)
    other.load()
    # 有效租约期内他人不可抢占
    assert other.claim_lease("worker-b", now=now + 1, lease_seconds=300) is False
    assert other.state["owner"] == "worker-a"
    # 持有人可续租
    assert store.renew_lease("worker-a", now=now + 2, lease_seconds=300) is True
    # 过期后可被接管（续租后租约至 now+2+300，now+400 已过期）
    late = st.SearchStore(ns)
    late.load()
    assert late.claim_lease("worker-b", now=now + 400, lease_seconds=300) is True
    assert late.state["owner"] == "worker-b"
    # 释放
    assert late.release_lease("worker-b") is True
    assert late.state["owner"] == ""
    # 过期内存副本（旧 generation）不能释放他人租约：CAS 拒绝，磁盘租约不变
    with pytest.raises(st.StoreConflict):
        store.release_lease("worker-a")
    assert json.loads((ns / "state.json").read_text(encoding="utf-8"))["owner"] == ""


# ── 绑定核验：manifest/协议漂移拒绝续用旧状态 ─────────────────────────────────


def test_verify_binding_rejects_manifest_or_protocol_drift(tmp_path, manifest, skill_dir):
    ns = _namespace(tmp_path, manifest)
    store = st.SearchStore(ns)
    store.initialize(manifest=manifest, case_ids=["case-1"], config={})
    ok = store.verify_binding(manifest)
    assert ok["ok"] is True

    # manifest 文档漂移（协议变化 → 摘要变化）：拒绝续用旧状态
    from agents.skill_search.contracts import freeze_protocol

    drifted = build_search_manifest(
        skill_dir=skill_dir,
        tenant="acme",
        space="ops",
        search_id=manifest.search_id,
        protocol=freeze_protocol(rubric_sha256="changed-rubric"),
    )
    assert drifted["ok"] is True
    verdict = store.verify_binding(drifted["manifest"])
    assert verdict["ok"] is False
    assert verdict["error"] == "protocol_changed"  # bundle 同、协议指纹变 → 协议漂移
    # bundle 漂移（源文件变化 → 新 bundle）
    (skill_dir / "SKILL.md").write_text(SKILL_TEXT.replace("灰度 5%", "灰度 6%"), encoding="utf-8")
    bundle_drift = build_search_manifest(
        skill_dir=skill_dir, tenant="acme", space="ops", search_id=manifest.search_id
    )
    verdict = store.verify_binding(bundle_drift["manifest"])
    assert verdict["ok"] is False
    # 协议指纹不一致（防御纵深分支）：落盘指纹与 manifest 不符即拒绝
    tampered = st.SearchStore(ns)
    tampered.load()
    tampered.state["protocol_fingerprint"] = "fp-forged"
    tampered.save()
    verdict = tampered.verify_binding(manifest)
    assert verdict["ok"] is False
    assert verdict["error"] == "protocol_changed"


# ── 成本归集账本（七类别预留/结算/释放） ───────────────────────────────────────


def test_cost_ledger_records_reservation_settlement_and_release(tmp_path, manifest):
    ns = _namespace(tmp_path, manifest)
    store = st.SearchStore(ns)
    store.initialize(manifest=manifest, case_ids=["case-1"], config={})
    store.record_reservation(key="baseline:s1:case-1:a1", category="baseline", handle="h1", tokens=10)
    assert store.state["open_reservations"]["baseline:s1:case-1:a1"]["handle"] == "h1"
    store.record_settlement(category="baseline", reserved_tokens=10, used_tokens=6, ok=True)
    store.pop_reservation("baseline:s1:case-1:a1")
    store.record_reservation(key="eval:s1:r1:c1:case-1:a1", category="execution", handle="h2", tokens=10)
    store.record_settlement(category="execution", reserved_tokens=10, used_tokens=0, ok=False)
    store.pop_reservation("eval:s1:r1:c1:case-1:a1")
    store.record_release(category="retry", tokens=10)
    store.record_settlement(category="retry", reserved_tokens=0, used_tokens=4, ok=True, calls=0)
    summary = store.cost_summary()
    assert summary["baseline"] == {"reserved_tokens": 10, "used_tokens": 6, "calls": 1, "failed_calls": 0, "released": 0}
    assert summary["execution"]["failed_calls"] == 1
    assert summary["retry"]["released"] == 10
    assert store.state["open_reservations"] == {}  # 结算/释放后无遗留开放预留


# ── 候选池 / 产物 / 回执落盘 ──────────────────────────────────────────────────


def test_pool_artifacts_and_receipts_live_in_namespace(tmp_path, manifest):
    ns = _namespace(tmp_path, manifest)
    store = st.SearchStore(ns)
    store.initialize(manifest=manifest, case_ids=["case-1", "case-2"], config={})
    pool = pl.CandidatePool(search_id=manifest.search_id, case_ids=["case-1", "case-2"])
    result = pool.register_seed(
        candidate_id="seed",
        score_vector={"case-1": 0.2, "case-2": 1.0},
        content_hash="hash-seed",
        protocol_hash="proto-1",
    )
    assert result["ok"] is True
    saved = store.save_pool(pool)
    assert saved["ok"] is True
    assert Path(saved["path"]).parent == ns
    loaded = store.load_pool(search_id=manifest.search_id)
    assert loaded is not None and len(loaded.candidates) == 1
    # search_id 不匹配的池拒绝加载（不串账）
    with pytest.raises(st.StoreCorruption, match="pool_search_id_mismatch"):
        store.load_pool(search_id="other-search")

    path = store.save_artifact("feedback-r1.json", {"ok": True, "items": []}, subdir="feedback")
    assert Path(path).parent == ns / "feedback"
    assert store.load_artifact("feedback-r1.json", subdir="feedback") == {"ok": True, "items": []}
    assert store.load_artifact("missing.json") is None

    receipt_path = store.save_receipt("restore-seed-s1", {"ok": True, "mounted": False})
    assert Path(receipt_path).parent == ns / "receipts"
    assert store.load_receipt("restore-seed-s1")["ok"] is True

    candidate_dir = store.candidate_dir("cand-r1")
    assert candidate_dir == ns / "candidates" / "cand-r1"
    # 路径分量被剥离：候选目录不逃逸命名空间
    assert store.candidate_dir("../escape").name == "escape"
    assert store.candidate_dir("../escape").parent == ns / "candidates"
    with pytest.raises(ValueError, match="candidate_id_invalid"):
        store.candidate_dir("  ")


def test_history_is_bounded_and_save_artifacts_atomic(tmp_path, manifest):
    ns = _namespace(tmp_path, manifest)
    store = st.SearchStore(ns)
    store.initialize(manifest=manifest, case_ids=["case-1"], config={})
    for index in range(500):
        store.append_history(f"event-{index}")
    assert len(store.state["history"]) <= 200
    store.save()
    revived = st.SearchStore(ns)
    revived.load()
    assert len(revived.state["history"]) <= 200


def test_open_search_store_uses_manifest_namespace(tmp_path, manifest):
    store = st.open_search_store(manifest, base_dir=tmp_path / "evo")
    assert store.namespace == search_namespace(manifest, tmp_path / "evo")
    assert store.namespace.parts[-4:] == ("acme", "ops", manifest.lineage, manifest.search_id)


# ── 跨实例/跨进程原子性（R08）：读→判→写全程持命名空间锁，不断言交错路径 ──────


def _run_claim_threads(handles, now=1000.0):
    outcomes = {}
    start = threading.Barrier(len(handles))

    def claim(pair):
        name, handle = pair
        start.wait(timeout=30)
        try:
            outcomes[name] = handle.claim_lease(name, now=now)
        except Exception as exc:  # 探针式异常也算失败结果
            outcomes[name] = exc

    threads = [threading.Thread(target=claim, args=(pair,)) for pair in handles]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return outcomes


def test_concurrent_claim_lease_single_winner_across_instances(tmp_path, manifest):
    """两实例真并发 claim_lease：恰好一个成功，最终 owner/_generation 与胜者一致。"""
    ns = _namespace(tmp_path, manifest)
    first = st.SearchStore(ns)
    first.initialize(manifest=manifest, case_ids=["case-1"], config={})
    second = st.SearchStore(ns)
    second.load()

    outcomes = _run_claim_threads([("worker-a", first), ("worker-b", second)])
    winners = [name for name, value in outcomes.items() if value is True]
    assert len(winners) == 1, outcomes
    winner = winners[0]
    final = st.SearchStore(ns)
    final.load()
    assert final.state["owner"] == winner
    assert final.state["generation"] == 2  # 只推进一代：没有双写
    loser = second if winner == "worker-a" else first
    assert loser.state["owner"] == winner  # 失败方内存与磁盘同步（如实上报）


def test_deterministic_interleave_same_generation_read_then_claims(tmp_path, manifest):
    """确定性交错（两实例同一 generation 读取后先后认领）：先到者胜，后到者失败。"""
    ns = _namespace(tmp_path, manifest)
    a = st.SearchStore(ns)
    a.initialize(manifest=manifest, case_ids=["case-1"], config={})
    b = st.SearchStore(ns)
    b.load()
    assert a.state["generation"] == b.state["generation"] == 1
    # A 先写完（generation 前进），B 仍持旧内存副本才认领
    assert a.claim_lease("worker-a", now=1000.0) is True
    assert b.claim_lease("worker-b", now=1000.0) is False
    on_disk = json.loads((ns / "state.json").read_text(encoding="utf-8"))
    assert on_disk["owner"] == "worker-a"
    assert on_disk["generation"] == 2


def test_disk_generation_read_timing_does_not_break_claim_atomicity(tmp_path, manifest):
    """``_disk_generation`` 读取被拖慢（探针式补丁：真实读 + 任意延迟）也不产生双认领。"""
    ns = _namespace(tmp_path, manifest)
    first = st.SearchStore(ns)
    first.initialize(manifest=manifest, case_ids=["case-1"], config={})
    second = st.SearchStore(ns)
    second.load()

    def jitter(handle, delay):
        original = handle._disk_generation

        def patched():
            value = original()
            time.sleep(delay)
            return value

        handle._disk_generation = patched

    jitter(first, 0.05)
    jitter(second, 0.02)
    outcomes = _run_claim_threads([("worker-a", first), ("worker-b", second)])
    winners = [name for name, value in outcomes.items() if value is True]
    assert len(winners) == 1, outcomes
    final = st.SearchStore(ns)
    final.load()
    assert final.state["owner"] == winners[0]
    assert final.state["generation"] == 2


def test_concurrent_save_same_generation_single_winner(tmp_path, manifest):
    """两实例同一 generation 并发 save：恰好一个成功，另一个 StoreConflict。"""
    ns = _namespace(tmp_path, manifest)
    a = st.SearchStore(ns)
    a.initialize(manifest=manifest, case_ids=["case-1"], config={})
    b = st.SearchStore(ns)
    b.load()
    outcomes = {}
    start = threading.Barrier(2)

    def saver(name, handle):
        start.wait(timeout=30)
        try:
            handle.save()
            outcomes[name] = "ok"
        except st.StoreConflict:
            outcomes[name] = "conflict"

    threads = [threading.Thread(target=saver, args=(name, handle))
               for name, handle in (("a", a), ("b", b))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert sorted(outcomes.values()) == ["conflict", "ok"], outcomes
    on_disk = json.loads((ns / "state.json").read_text(encoding="utf-8"))
    assert on_disk["generation"] == 2


_CROSS_PROCESS_CLAIM_CHILD = r"""
import json, os, sys, time
sys.path.insert(0, os.environ["EVO_REPO"])
from agents.skill_search.store import SearchStore

store = SearchStore(os.environ["EVO_NS"])
store.load()
while not os.path.exists(os.environ["EVO_GO"]):
    time.sleep(0.005)
claimed = store.claim_lease(os.environ["EVO_WORKER"], now=1000.0)
print(json.dumps({"claimed": bool(claimed),
                  "owner": str((store.state or {}).get("owner"))}), flush=True)
"""


def test_cross_process_claim_lease_exactly_one_winner(tmp_path, manifest):
    """真正双进程并发 claim_lease：恰好一个成功（另一进程返回 False）。"""
    ns = _namespace(tmp_path, manifest)
    store = st.SearchStore(ns)
    store.initialize(manifest=manifest, case_ids=["case-1"], config={})

    go_file = tmp_path / "go.flag"
    env = dict(os.environ)
    env.update({
        "EVO_REPO": str(Path(__file__).resolve().parents[1]),
        "EVO_NS": str(ns),
        "EVO_GO": str(go_file),
        "PYTHONIOENCODING": "utf-8",
    })
    children = []
    for index, worker in enumerate(("proc-worker-a", "proc-worker-b")):
        child_env = dict(env)
        child_env["EVO_WORKER"] = worker
        children.append(subprocess.Popen(
            [sys.executable, "-c", _CROSS_PROCESS_CLAIM_CHILD], env=child_env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
    time.sleep(0.5)  # 两个进程都完成 load 并轮询 go 标记
    go_file.write_text("go", encoding="utf-8")
    outputs = []
    for proc in children:
        stdout, stderr = proc.communicate(timeout=60)
        assert proc.returncode == 0, stderr
        outputs.append(json.loads(stdout.strip().splitlines()[-1]))
    winners = [o for o in outputs if o["claimed"]]
    assert len(winners) == 1, outputs
    assert winners[0]["owner"] in {"proc-worker-a", "proc-worker-b"}
    final = st.SearchStore(ns)
    final.load()
    assert final.state["owner"] == winners[0]["owner"]
    assert final.state["generation"] == 2


def test_store_lock_recovered_after_holder_crash(tmp_path, manifest):
    """锁异常安全：持有者进程崩溃（不解锁即死亡）后内核释放锁，其他实例可接管。"""
    ns = _namespace(tmp_path, manifest)
    store = st.SearchStore(ns)
    store.initialize(manifest=manifest, case_ids=["case-1"], config={})

    crasher = r"""
import os, sys, time
sys.path.insert(0, os.environ["EVO_REPO"])
from agents.skill_search.store import namespace_exclusive_lock
with namespace_exclusive_lock(os.environ["EVO_NS"], timeout=5.0):
    print("locked", flush=True)
    time.sleep(30)
"""
    env = dict(os.environ)
    env.update({"EVO_REPO": str(Path(__file__).resolve().parents[1]),
                "EVO_NS": str(ns), "PYTHONIOENCODING": "utf-8"})
    proc = subprocess.Popen([sys.executable, "-c", crasher], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    line = proc.stdout.readline().strip()
    assert line == "locked", line
    proc.kill()  # 持有者崩溃：绝不走释放路径
    proc.wait(timeout=30)
    time.sleep(0.2)
    # 接管：持锁获取 + 正常认领都成功（不是永久死锁的锁文件）
    with st.namespace_exclusive_lock(ns, timeout=10.0):
        pass
    assert store.claim_lease("worker-parent", now=1000.0) is True
    on_disk = json.loads((ns / "state.json").read_text(encoding="utf-8"))
    assert on_disk["owner"] == "worker-parent"
    assert on_disk["generation"] == 2


def test_store_lock_timeout_is_fail_closed(tmp_path, manifest):
    """锁等待超时：抛明确 StoreLockTimeout，绝不无锁写入。"""
    ns = _namespace(tmp_path, manifest)
    store = st.SearchStore(ns)
    store.initialize(manifest=manifest, case_ids=["case-1"], config={})
    with st.namespace_exclusive_lock(ns, timeout=10.0):
        with pytest.raises(st.StoreLockTimeout):
            with st.namespace_exclusive_lock(ns, timeout=0.05):
                pass
        on_disk = json.loads((ns / "state.json").read_text(encoding="utf-8"))
        assert on_disk["generation"] == 1  # 未被无锁写入污染
