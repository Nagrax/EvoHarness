"""EVO-05 夜间调度部署路径回归：manifest/revision 确定性、recheck 裁决、报告字节级确定、部署配置。

部署口径：Dreaming 单轮编排（reconcile.run_dreaming_cycle）经 ops/dreaming_nightly
被 systemd timer（ops/evoharness-dreaming.timer）定时 oneshot 触发；候选不自动采用。
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hashlib
import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from evoharness.knowledge import reconcile
from evoharness.knowledge import service as knowledge_service
from evoharness.knowledge.models import KnowledgeClaim
from evoharness.models import Base
from ops import dreaming_nightly

REPO = Path(__file__).resolve().parents[1]

# 确定性 fake source：ref → {revision, value}；快照 sha256 只由内容决定
READER_STATE = {
    "config:gateway@1": {"revision": 1, "value": "30s"},
    "config:retry@1": {"revision": 1, "value": "3"},
}


def _reader(state):
    def read(session, ref, **scope):
        entry = state[ref]
        full = {key: scope.get(key, "") for key in ("org_id", "space_id", "owner_scope")}
        digest = hashlib.sha256(json.dumps(
            {"ref": ref, "value": entry["value"], **full},
            sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        return {"ref": ref, "revision": int(entry["revision"]), "sha256": digest, **full}
    return read


def _drift(state, ref="config:retry@1", value="5"):
    state[ref] = {"revision": state[ref]["revision"] + 1, "value": value}
    return state


@pytest.fixture()
def env():
    state = {ref: dict(entry) for ref, entry in READER_STATE.items()}
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    ids = {}
    with factory.begin() as session:
        reader = _reader(state)
        verified = [
            {"subject": "gateway timeout", "predicate": "value", "value": "30s",
             "source_refs": ["config:gateway@1"]},
            {"subject": "retry budget", "predicate": "value", "value": "3",
             "source_refs": ["config:retry@1"]},
        ]
        for spec in verified:
            row = knowledge_service.record_verified_claim(
                session, source_reader=reader, space_id="support", owner_scope="public",
                confidence_basis="baseline", **spec)
            ids[spec["subject"]] = row.claim_id
        declared = knowledge_service.record_claim(
            session, space_id="support", owner_scope="public", subject="legacy note",
            predicate="pointer", value="wiki:runbook-7", source_refs=["note:runbook@1"])
        ids["legacy note"] = declared.claim_id
    yield factory, ids, state
    engine.dispose()


def _subject_of(ids, claim_id):
    return next(k for k, v in ids.items() if v == claim_id)


# ── manifest 枚举：确定性 + 空间隔离 ─────────────────────────────────────────

def test_manifest_is_deterministic_and_scoped(env):
    factory, ids, _state = env
    with factory.begin() as session:
        manifest = dreaming_nightly.build_manifest(session, space_id="support")
        again = dreaming_nightly.build_manifest(session, space_id="support")
        assert manifest == again == sorted(ids.values())
        # 其他空间的主张不进 manifest
        knowledge_service.record_claim(
            session, space_id="other", owner_scope="public", subject="foreign",
            predicate="value", value="x", source_refs=["config:foreign@1"])
        assert dreaming_nightly.build_manifest(session, space_id="support") == manifest


def test_manifest_excludes_superseded_and_expired(env):
    factory, ids, state = env
    with factory.begin() as session:
        knowledge_service.record_claim(
            session, space_id="support", owner_scope="public", subject="stale hint",
            predicate="value", value="old", source_refs=["config:stale@1"],
            valid_until=datetime(2020, 1, 1))
        manifest = dreaming_nightly.build_manifest(
            session, space_id="support", now=datetime(2026, 11, 21))
        assert len(manifest) == 3
        knowledge_service.record_claim(
            session, space_id="support", owner_scope="public", subject="retry budget",
            predicate="value", value="5", source_refs=["config:retry@2"],
            confidence_basis="adopted-by:maintainer", supersedes=ids["retry budget"])
        narrowed = dreaming_nightly.build_manifest(session, space_id="support")
        assert ids["retry budget"] not in narrowed
        assert len(narrowed) == 3


# ── nightly_revision：CAS 版本随采用链抬升，过期调度轮被拒 ───────────────────

def test_nightly_revision_tracks_adoption_chain(env):
    factory, ids, state = env
    with factory.begin() as session:
        manifest = dreaming_nightly.build_manifest(session, space_id="support")
        assert dreaming_nightly.nightly_revision(session, manifest) == 1
        # 夜间轮开在 rev1：gateway 源漂移后产候选
        judge = dreaming_nightly.SnapshotRecheckJudge(
            session, _reader(_drift(state, ref="config:gateway@1", value="5s")))
        round1 = reconcile.run_dreaming_cycle(
            session, space_id="support", prepared_by="dreaming-nightly",
            input_manifest=manifest, expected_revision=1, judge=judge)
        assert round1["candidates"] == [ids["gateway timeout"]]
        # 期间另一责任人对 retry 主张走了自己的采用链 → max(source_revision) 抬到 2
        # （adopt_candidate 写 successor 时 source_revision = target+1，这里如实模拟）
        knowledge_service.record_claim(
            session, space_id="support", owner_scope="public", subject="retry budget",
            predicate="value", value="5", source_refs=["config:retry@2"],
            source_revision=2, confidence_basis="adopted-by:other-owner",
            supersedes=ids["retry budget"])
        fresh_manifest = dreaming_nightly.build_manifest(session, space_id="support")
        assert dreaming_nightly.nightly_revision(session, fresh_manifest) == 2
        # 拿"最新"revision 采用旧夜间报告的候选 → CAS 拒绝；报告自带 revision 才有效
        with pytest.raises(reconcile.ReconcileError, match="stale_reconcile_revision"):
            reconcile.adopt_candidate(session, round1["job_id"], item=ids["gateway timeout"],
                                      new_value="5s", actor="maintainer", expected_revision=2)
        outcome = reconcile.adopt_candidate(
            session, round1["job_id"], item=ids["gateway timeout"],
            new_value="5s", actor="maintainer", expected_revision=1)
        assert outcome["reason"] == "adopted"


# ── recheck 裁决：漂移/一致/声明级三分，不自动采用 ───────────────────────────

def test_recheck_judge_verdicts_and_no_auto_adoption(env):
    factory, ids, state = env
    with factory.begin() as session:
        judge = dreaming_nightly.SnapshotRecheckJudge(session, _reader(_drift(state)))
        assert judge(ids["gateway timeout"]).verdict == "supports"
        absent = judge(ids["legacy note"])
        assert absent.verdict == "absent" and absent.source_refs == []
        drifted = judge(ids["retry budget"])
        assert drifted.verdict == "contradicts"
        assert drifted.source_refs == ["config:retry@1"]
        assert "config:retry@1" in drifted.note
        # 三种裁决都不改主张：候选只记录，采用必须显式
        report = reconcile.run_dreaming_cycle(
            session, space_id="support", prepared_by="dreaming-nightly",
            input_manifest=sorted(ids.values()), expected_revision=1, judge=judge)
        assert report["candidates"] == [ids["retry budget"]]
        for subject, claim_id in ids.items():
            claim = session.get(KnowledgeClaim, claim_id)
            assert claim.state == "active"
            assert claim.value == {"gateway timeout": "30s", "retry budget": "3",
                                   "legacy note": "wiki:runbook-7"}[subject]


def test_source_read_failure_is_error_not_fabricated_contradiction(env):
    """读取失败 ≠ 矛盾：error 计入 failed、partial 不冒充完成、不产候选。"""
    factory, ids, state = env

    def broken_reader(session, ref, **scope):
        raise RuntimeError(f"source down: {ref}")

    with factory.begin() as session:
        judge = dreaming_nightly.SnapshotRecheckJudge(session, broken_reader)
        verdict = judge(ids["gateway timeout"])
        assert verdict.verdict == "error"
        assert verdict.source_refs == [] and "source_unreadable" in verdict.note
        report = reconcile.run_dreaming_cycle(
            session, space_id="support", prepared_by="dreaming-nightly",
            input_manifest=sorted(ids.values()), expected_revision=1, judge=judge)
        assert report["candidates"] == []
        # 两条 source_verified 主张读取失败 → error 进 failed；declared 主张 absent（不在 failed）
        assert report["failed"] == sorted([ids["gateway timeout"], ids["retry budget"]])
        assert report["completed"] is False
        job = session.get(reconcile.ReconciliationJob, report["job_id"])
        assert job.status == "partial"  # 作业 partial（可经原作业续处理），不冒充完成


# ── 夜间报告：同输入两轮字节级一致；漂移可见 ─────────────────────────────────

def test_nightly_report_is_byte_deterministic(env, tmp_path):
    factory, ids, state = env
    report_dir = tmp_path / "dreaming"
    now = datetime(2026, 11, 21, 2, 30, 0)
    first = dreaming_nightly.run_nightly(factory, spaces=["support"],
                                         source_reader=_reader(state), now=now,
                                         report_dir=report_dir)
    path = report_dir / "2026-11-21-support.json"
    assert path.is_file()
    body1 = path.read_bytes()
    report = json.loads(body1)
    assert report["date"] == "2026-11-21" and "job_id" not in report
    assert report["revision"] == 1 and report["counts"]["candidates"] == 0
    second = dreaming_nightly.run_nightly(factory, spaces=["support"],
                                          source_reader=_reader(state), now=now,
                                          report_dir=report_dir)
    assert path.read_bytes() == body1
    assert first["spaces"][0]["status"] == second["spaces"][0]["status"] == "done"
    # 源漂移后同入口重跑：报告字节变化，候选出现（仍是候选，不自动采用）
    state["config:retry@1"] = {"revision": 2, "value": "5"}
    third = dreaming_nightly.run_nightly(factory, spaces=["support"],
                                         source_reader=_reader(state), now=now,
                                         report_dir=report_dir)
    assert path.read_bytes() != body1
    assert third["spaces"][0]["report"]["counts"]["candidates"] == 1
    with factory.begin() as session:
        assert session.get(KnowledgeClaim, ids["retry budget"]).value == "3"


def test_single_writer_conflict_skips_space_and_keeps_others(env, tmp_path):
    factory, ids, state = env
    with factory.begin() as session:
        reconcile.start_job(session, space_id="support", prepared_by="human-reconciler",
                            input_manifest=[ids["gateway timeout"]], expected_revision=1)
    outcome = dreaming_nightly.run_nightly(
        factory, spaces=["support", "other"], source_reader=_reader(state),
        now=datetime(2026, 11, 21, 2, 30, 0), report_dir=tmp_path / "dreaming")
    by_space = {e["space_id"]: e for e in outcome["spaces"]}
    assert by_space["support"]["status"] == "skipped"
    assert by_space["support"]["reason"] == "reconciler_already_active"
    assert by_space["other"]["status"] == "empty"
    assert outcome["ok"] is True


def test_cli_entry_runs_and_writes_reports(tmp_path, monkeypatch):
    """CLI oneshot（部署默认 reader = provenance）：合成引用不可解引 → error 留痕
    （不伪造矛盾、不产候选），作业 partial、退出 0；报告落盘。
    （漂移检测路径由注入 reader 的 run_nightly 覆盖，见 test_nightly_report_is_byte_deterministic。）
    """
    db_path = tmp_path / "agent.db"
    db_url = f"sqlite:///{db_path.as_posix()}"
    engine = create_engine(db_url, future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    state = {ref: dict(entry) for ref, entry in READER_STATE.items()}
    with factory.begin() as session:
        knowledge_service.record_verified_claim(
            session, source_reader=_reader(state), space_id="support",
            owner_scope="public", subject="retry budget", predicate="value",
            value="3", source_refs=["config:retry@1"])
    engine.dispose()
    monkeypatch.chdir(tmp_path)
    code = dreaming_nightly.main([
        "--db", db_url, "--space", "support",
        "--report-dir", str(tmp_path / "dreaming"), "--date", "2026-11-21"])
    assert code == 0
    report_path = tmp_path / "dreaming" / "2026-11-21-support.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["counts"] == {"total": 1, "processed": 1, "failed": 1,
                                "candidates": 0, "completed": False}
    verdict = next(iter(report["verdicts"].values()))
    assert verdict["verdict"] == "error" and "source_unreadable" in verdict["note"]
    assert verdict["candidate"] is False


# ── 部署配置：systemd timer/service 结构断言（确定性） ───────────────────────

def test_systemd_units_are_deployable_and_consistent():
    service = (REPO / "ops" / "evoharness-dreaming.service").read_text(encoding="utf-8")
    timer = (REPO / "ops" / "evoharness-dreaming.timer").read_text(encoding="utf-8")
    doc = (REPO / "ops" / "NIGHTLY-DREAMING.md").read_text(encoding="utf-8")
    assert "Type=oneshot" in service
    exec_line = next(ln for ln in service.splitlines() if ln.startswith("ExecStart="))
    assert "-m ops.dreaming_nightly" in exec_line
    assert "--space" in exec_line and "--report-dir" in exec_line
    assert "User=evoharness" in service and "EnvironmentFile=" in service
    assert "Unit=evoharness-dreaming.service" in timer
    assert "OnCalendar=" in timer and "Persistent=true" in timer
    assert "RandomizedDelaySec=" in timer
    assert "WantedBy=timers.target" in timer
    # 部署文档：一键步骤、显式采用路径、与既有机制（supervisor/wakeups）的组合口径
    assert "systemctl enable --now evoharness-dreaming.timer" in doc
    assert "adopt_candidate" in doc and "ops.dreaming_nightly" in doc
    assert "supervisor" in doc.lower() and "wakeups" in doc.lower()
