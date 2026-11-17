"""DE-25 最终验收回归：状态判定表 / S2 done 守卫 / 灰度阶梯 / 撤权演练 / 声明分离。

覆盖 FINAL-ACCEPTANCE.md（DE-25 交付物）的五组可执行判定，全部离线：

1. 45 项 DE 工单状态判定表一致性：每项 done/blocked/pending 都有存在的证据引用或
   明确阻塞原因；DE-26/27 标 optional；无"已完成但无证据"项。核对以文档结构 +
   tasks.json + acceptance 目录为准（读结构断言，不硬编码结论），并以伪造行负例
   证明判定器真实生效。
2. S2 守卫：样本不足/权限未给/等待自然周期/外部依赖未满足/无证据 → 禁止记 done。
3. 灰度阶梯：影子零写副作用、批准逐条绑定、限定自动只放行低风险集合 ——
   纯函数判定 + DE-19 真实工作流组合（不旁路业务判定）。
4. 停止/撤权/更正演练：撤权传播后无新副作用 —— 真实组合 DE-12 authorization、
   DE-41 decisions.check_send_allowed / pause_scope、DE-19 apply_correction。
5. 基础试点≠搜索增强：声明分离规则与交叉核对（搜索未过不得报收益/证书）。

不删除、不弱化任何既有测试；不触碰 eval/loop/BACKLOG.json 与生产数据。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from evoharness.governance import authorization, decisions
from evoharness.governance.decisions import GovernanceError
from evoharness.knowledge import service as knowledge
from evoharness.models import Base, OutboxStatus
from evoharness.organization import service as org
from evoharness.organization.service import AuthenticatedPrincipal
from evoharness.organization.models import BusinessSpace, Organization
from evoharness.outbox import enqueue
from evoharness.workflows import knowledge_answer as ka

PROJECT = Path(__file__).resolve().parents[1]
from delivery_fixtures import delivery_fixture
REPO = delivery_fixture()
CHECKER_PATH = PROJECT / "eval/acceptance_check.py"
FINAL_DOC = "docs/digital-employee/acceptance/FINAL-ACCEPTANCE.md"


def _load_checker():
    spec = importlib.util.spec_from_file_location("acceptance_check_de25", CHECKER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ac = _load_checker()

GOV_OWNER = AuthenticatedPrincipal(
    principal_id="founder", tenant_id="tn", app_id="app", authenticated=True,
    authorities=frozenset({"organization.bootstrap", "organization.manage"}))


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as s:
        yield s
    engine.dispose()


@pytest.fixture()
def qa_space(session):
    """组织 + 业务空间责任人（升级目标来自配置，不猜 owner）。"""
    session.add(Organization(org_id="org", title="EvoHarness", owner_principal_id="boss"))
    session.add(BusinessSpace(space_id="support", org_id="org", title="支持",
                              owner_principal_id="space-owner"))
    session.flush()


def _claim(s, subject="退款规则", value="7 天无理由", scope="public",
           refs=("doc:policy@v3",), **kw):
    return knowledge.record_claim(s, org_id="org", space_id="support",
                                  owner_scope=scope, subject=subject, value=value,
                                  source_refs=list(refs), **kw)


def _ask(s, event="om_1", subject="退款规则", audience="", auto=False, sink=None, **kw):
    return ka.answer_question(s, org_id="org", space_id="support", subject=subject,
                              source_event_id=event, audience_scope=audience,
                              audience_id=audience or "anonymous",
                              sink=sink, auto_enabled=auto, **kw)


def _send_intents(sink) -> list[dict]:
    return [e for e in sink.events if e["kind"] == "send_intent"]


# ════════════════════════════════════════════════════════════════════════════
# 组 1：45 项状态判定表（读文档结构断言，不硬编码结论）
# ════════════════════════════════════════════════════════════════════════════


def test_status_table_covers_all_tasks_with_evidence_or_block_reason():
    """45 项每项都有状态 + 存在的证据或明确阻塞原因；表与 tasks.json 一一对应。"""
    report = ac.verify_status_table(REPO, doc_path=FINAL_DOC)
    task_index = ac.load_tasks_index(REPO)
    assert report["task_total"] == len(task_index)
    assert report["row_count"] == report["task_total"]
    assert report["violations"] == []
    for tid, status in report["statuses"].items():
        assert status in ac.ALLOWED_STATUS
        if status == ac.STATUS_DONE:
            for path in ac._split_evidence(
                    _evidence_cell(REPO, tid)):
                assert (REPO / path).is_file(), f"{tid} 证据不存在：{path}"
        if status == ac.STATUS_BLOCKED:
            assert len(_note_cell(REPO, tid).strip()) >= 8, f"{tid} 缺阻塞原因"


def _row(REPO_, tid):
    text = (REPO_ / FINAL_DOC).read_text(encoding="utf-8")
    rows = ac._table_rows(ac._section(text, "DE 工单状态判定表"))
    for row in rows:
        if row and row[0] == tid:
            return row
    raise AssertionError(f"状态表缺 {tid}")


def _evidence_cell(REPO_, tid):
    return _row(REPO_, tid)[2]


def _note_cell(REPO_, tid):
    return _row(REPO_, tid)[3]


def test_optional_flag_matches_tasks_json_and_no_done_without_evidence():
    """optional 标记与 tasks.json 双向一致；无"已完成但无证据"项。"""
    task_index = ac.load_tasks_index(REPO)
    optional_in_tasks = {tid for tid, t in task_index.items() if t.get("optional")}
    report = ac.verify_status_table(REPO, doc_path=FINAL_DOC)
    optional_in_table = {tid for tid, status in report["statuses"].items()
                         if status == ac.STATUS_OPTIONAL}
    assert optional_in_table == optional_in_tasks
    done_without_evidence = [
        v for v in report["violations"] if "无证据" in v or "不存在" in v]
    assert done_without_evidence == []


def test_status_table_checker_catches_fabricated_rows():
    """负例：判定器对伪造行真实生效（不是空跑）。"""
    repo, tasks = REPO, ac.load_tasks_index(REPO)
    # done 无证据
    assert ac.validate_status_row(
        ["DE-00", "done", "", ""], task_index=tasks, repo_root=repo)
    # done 证据文件不存在
    assert any("不存在" in v for v in ac.validate_status_row(
        ["DE-00", "done", "docs/digital-employee/acceptance/DE-00/nope.md", ""],
        task_index=tasks, repo_root=repo))
    # blocked 缺阻塞原因
    assert ac.validate_status_row(
        ["DE-02", "blocked", "docs/digital-employee/acceptance/DE-02/IMPLEMENTATION-20261113.md", "x"],
        task_index=tasks, repo_root=repo)
    # 非可选项标 optional / 可选项未标 optional
    assert any("未标可选" in v for v in ac.validate_status_row(
        ["DE-00", "optional", "", ""], task_index=tasks, repo_root=repo))
    assert ac.validate_status_row(
        ["DE-26", "done", ""], task_index=tasks, repo_root=repo)
    # 未知工单 / pending 无说明 / 非法状态
    assert ac.validate_status_row(
        ["DE-99", "done", ""], task_index=tasks, repo_root=repo)
    assert any("缺未实施" in v for v in ac.validate_status_row(
        ["DE-36", "pending", "", ""], task_index=tasks, repo_root=repo))
    assert ac.validate_status_row(
        ["DE-00", "shipped", "", ""], task_index=tasks, repo_root=repo)


def test_status_table_missing_id_is_caught(tmp_path):
    """负例：状态表缺行时整体核对必须报缺（判定器不漏项）。"""
    doc = tmp_path / "final-acceptance-subset.md"
    doc.write_text(
        "# 最终验收\n\n## 2. DE 工单状态判定表（45 项）\n\n"
        "| 工单 | 状态 | 证据 | 阻塞原因与备注 |\n|---|---|---|---|\n"
        "| DE-00 | done | docs/digital-employee/acceptance/DE-00/acceptance-report.md | ok |\n",
        encoding="utf-8")
    report = ac.verify_status_table(REPO, doc_path=str(doc))
    assert any("状态表缺工单" in v for v in report["violations"])
    assert report["row_count"] == 1


# ════════════════════════════════════════════════════════════════════════════
# 组 2：S2 done 守卫 + 灰度阶梯（影子→批准→限定自动）
# ════════════════════════════════════════════════════════════════════════════


def _done_claim(**overrides) -> dict:
    claim = {
        "claimed_status": "done",
        "evidence_paths": ["docs/digital-employee/acceptance/DE-25/IMPLEMENTATION-20261114.md"],
        "evidence_exists": True,
        "external_deps_unmet": [],
        "required_permissions": [],
        "permissions_granted": True,
        "owner_release_required": False,
        "owner_release_recorded": True,
        "sample_min": 0,
        "sample_count": 0,
        "natural_cycle_required": False,
        "natural_cycle_met": False,
    }
    claim.update(overrides)
    return claim


def test_s2_guard_forbids_done_on_insufficient_samples():
    allowed, reasons = ac.validate_status_claim(_done_claim(sample_min=60, sample_count=2))
    assert not allowed and reasons == ["sample_insufficient"]


def test_s2_guard_forbids_done_on_missing_permissions():
    allowed, reasons = ac.validate_status_claim(
        _done_claim(required_permissions=["im:message.send_as_user"],
                    permissions_granted=False))
    assert not allowed and reasons == ["permission_not_granted"]


def test_s2_guard_forbids_done_on_natural_cycle_pending():
    allowed, reasons = ac.validate_status_claim(
        _done_claim(natural_cycle_required=True, natural_cycle_met=False))
    assert not allowed and reasons == ["natural_cycle_pending"]


def test_s2_guard_forbids_done_on_external_dependency_unmet():
    allowed, reasons = ac.validate_status_claim(
        _done_claim(external_deps_unmet=["真实飞书应用权限"]))
    assert not allowed and reasons == ["external_dependency_unmet"]


def test_s2_guard_forbids_done_without_evidence_and_without_owner_release():
    allowed, reasons = ac.validate_status_claim(
        _done_claim(evidence_paths=[], evidence_exists=False,
                    owner_release_required=True, owner_release_recorded=False))
    assert not allowed
    assert sorted(reasons) == ["done_without_evidence", "owner_release_missing"]


def test_s2_guard_allows_done_with_complete_evidence():
    allowed, reasons = ac.validate_status_claim(_done_claim(sample_min=60, sample_count=60))
    assert allowed and reasons == []


def test_s2_guard_allows_blocked_and_pending_recorded_honestly():
    """非 done 状态恒允许（诚实记录），不触发任何禁止规则。"""
    for status in ("blocked", "pending"):
        allowed, reasons = ac.validate_status_claim({"claimed_status": status})
        assert allowed and reasons == []


def test_ladder_shadow_forbids_write_side_effects():
    for action in ("send", "write", "publish"):
        allowed, reason = ac.decide_stage_admission("shadow", action=action)
        assert not allowed and reason == "shadow_no_write_side_effect"
    for action in ("observe", "draft", "escalate_record"):
        allowed, reason = ac.decide_stage_admission("shadow", action=action)
        assert allowed and reason == "shadow_read_only"


def test_ladder_approval_requires_per_item_binding():
    # 非发送动作在批准阶段直接拒绝
    allowed, reason = ac.decide_stage_admission("approval", action="draft")
    assert not allowed and reason == "approval_stage_rejects_non_send"
    # 无决定 → 逐条批准缺失
    allowed, reason = ac.decide_stage_admission("approval", action="send")
    assert not allowed and reason == "approval_required_per_item"
    # 绑定五元组缺字段
    allowed, reason = ac.decide_stage_admission(
        "approval", action="send", decision={"decision_id": "d1"})
    assert not allowed and reason.startswith("approval_binding_incomplete")
    # 决定授权动作不是 send_as_bot → 动作不匹配
    allowed, reason = ac.decide_stage_admission(
        "approval", action="send",
        decision={"decision_id": "d1", "payload_hash": "h", "target_revision": 1,
                  "grantee_id": "public", "action": "send"})
    assert not allowed and reason == "approval_action_mismatch"
    # 完整绑定 + send_as_bot 授权 → 放行
    allowed, reason = ac.decide_stage_admission(
        "approval", action="send",
        decision={"decision_id": "d1", "payload_hash": "h", "target_revision": 1,
                  "grantee_id": "public", "action": "send_as_bot"})
    assert allowed and reason == "approved_per_item"


def test_ladder_auto_restricted_to_low_risk_set():
    binding = {"decision_id": "d1", "payload_hash": "h", "target_revision": 1,
               "grantee_id": "public", "action": "send_as_bot"}
    release = {"released": True, "ref": "runs/F09/owner-release.json"}
    base = dict(action="send", category="faq", confidence=0.97,
                risk_flags=(), decision=binding, owner_release=release,
                daily_sent=0, daily_cap=100)
    allowed, reason = ac.decide_stage_admission("auto", **base)
    assert allowed and reason == "auto_allowed_within_low_risk_set"
    # 自动阶段同样只发送：非发送动作拒绝
    allowed, reason = ac.decide_stage_admission(
        "auto", **{**base, "action": "write"})
    assert not allowed and reason == "auto_stage_rejects_non_send"
    # 低风险集合外类别
    allowed, reason = ac.decide_stage_admission("auto", **{**base, "category": "billing"})
    assert not allowed and reason == "category_not_in_low_risk_set"
    # 风险标记（承诺/敏感）
    allowed, reason = ac.decide_stage_admission(
        "auto", **{**base, "risk_flags": ("promise",)})
    assert not allowed and reason == "risk_flags_present"
    # 置信低于冻结阈值
    allowed, reason = ac.decide_stage_admission("auto", **{**base, "confidence": 0.85})
    assert not allowed and reason == "confidence_below_frozen_threshold"
    # 无负责人放行 / 无放行记录
    allowed, reason = ac.decide_stage_admission("auto", **{**base, "owner_release": None})
    assert not allowed and reason == "owner_release_missing"
    allowed, reason = ac.decide_stage_admission(
        "auto", **{**base, "owner_release": {"released": True}})
    assert not allowed and reason == "owner_release_missing"
    # 配额未配置 / 已超
    allowed, reason = ac.decide_stage_admission("auto", **{**base, "daily_cap": None})
    assert not allowed and reason == "auto_cap_unconfigured"
    allowed, reason = ac.decide_stage_admission("auto", **{**base, "daily_sent": 100})
    assert not allowed and reason == "daily_cap_exceeded"
    # 未知阶段 fail-closed
    allowed, reason = ac.decide_stage_admission("freestyle", **base)
    assert not allowed and reason == "unknown_stage"


def test_shadow_stage_real_workflow_has_zero_send_side_effect(session, qa_space):
    """影子阶段真实组合（DE-19 工作流）：任何输入都不产生发送副作用。"""
    _claim(session)
    sink = ka.RecordedSink()
    first = _ask(session, event="ev1", sink=sink, auto=False)
    assert first["delivery"]["mode"] == "draft_pending_approval"
    assert _send_intents(sink) == []
    # 即使批准在案、开关误开为 True 之外的路径不存在：影子口径下开关关闭
    approved = ka.approve_answer(session, actor_id="boss", org_id="org",
                                 space_id="support", subject="退款规则",
                                 answer_payload_dict=first["answer"]["payload"])
    second = _ask(session, event="ev2", sink=sink, auto=False)
    assert second["delivery"]["mode"] == "draft_reusable"
    assert second["delivery"]["decision_id"] == approved["decision_id"]
    assert _send_intents(sink) == []          # 影子/单批准：零发送意图
    # 承诺/敏感内容同样只落草稿
    knowledge.record_claim(session, org_id="org", space_id="support",
                           owner_scope="public", subject="赔付承诺",
                           value="全额退款保障", source_refs=["doc:v2"])
    risky = _ask(session, event="ev3", subject="赔付承诺", sink=sink, auto=False)
    assert risky["delivery"]["mode"] == "draft_pending_approval"
    assert _send_intents(sink) == []


def test_approval_stage_binds_per_item_and_audience(session, qa_space):
    """批准阶段真实组合：逐条绑定 payload hash + 受众；换受众不自动发送。"""
    _claim(session)
    sink = ka.RecordedSink()
    first = _ask(session, event="ev1", audience="public", sink=sink, auto=True)
    assert first["delivery"]["mode"] == "draft_pending_approval"
    ka.approve_answer(session, actor_id="boss", org_id="org", space_id="support",
                      subject="退款规则",
                      answer_payload_dict=first["answer"]["payload"],
                      audience_scope="public")
    second = _ask(session, event="ev2", audience="public", sink=sink, auto=True)
    assert second["delivery"]["mode"] == "auto_send"           # 同载荷同受众 → 限定自动
    assert second["delivery"]["decision_id"]
    # 受众不同 → grantee 不匹配 → 回落草稿（逐条绑定不放大）
    other = _ask(session, event="ev3", audience="team-b", sink=sink, auto=True)
    assert other["delivery"]["mode"] == "draft_pending_approval"
    assert other["delivery"]["decision_id"] == ""
    assert len(_send_intents(sink)) == 1                       # 仅第二次（批准范围内）一条


def test_auto_stage_low_confidence_still_escalates(session, qa_space):
    """低置信真实组合：低于冻结阈值不自动发送，升级责任人（DE-19 语义）。"""
    _claim(session)
    sink = ka.RecordedSink()
    outcome = _ask(session, event="ev1", sink=sink, auto=True, min_confidence=0.95)
    assert outcome["delivery"]["mode"] == "escalation_review"
    assert outcome["delivery"]["reasons"] == ["below_auto_threshold"]
    assert outcome["status"] == "escalated"
    assert _send_intents(sink) == []


def test_emergency_stop_switches_to_single_approval_and_preserves_evidence():
    """急停判定：停止类事件 → 停发+撤权+保留证据+复盘，恢复口径 single_approval。"""
    for kind in ("unauthorized_send", "wrong_content_sent", "severe_error",
                 "permission_scope_change"):
        verdict = ac.emergency_stop_decision({"kind": kind})
        assert verdict["stop_sending"] and verdict["revoke_decisions"]
        assert verdict["preserve_evidence"] and verdict["review_required"]
        assert verdict["resume_mode"] == "single_approval"
    # 对账异常只标记复盘不停发；普通事件不触发
    verdict = ac.emergency_stop_decision({"kind": "delivery_unknown"})
    assert not verdict["stop_sending"] and verdict["review_required"]
    assert ac.emergency_stop_decision({"kind": "daily_report"})["review_required"] is False


# ════════════════════════════════════════════════════════════════════════════
# 组 3：停止/撤权/更正演练判定（DE-41 语义真实组合）
# ════════════════════════════════════════════════════════════════════════════


def test_revocation_propagation_leaves_no_new_side_effects(session):
    """撤权传播演练：排队意图就地取消；在途行发送前被拦截；真实回执不被改写。"""
    authorization.approve(session, "founder", org_id="org", target_type="draft",
                          target_id="draft-1", target_revision=1, payload_hash="h1",
                          actions={"send_as_bot"})
    queued = enqueue(session, intent="rev-drill-1", tenant="default", kind="reply",
                     target_type="message", target_id="draft-1", payload={"body": "答案"})
    assert queued.status == OutboxStatus.QUEUED
    cancelled = authorization.revoke(session, "founder", target_type="draft",
                                     target_id="draft-1", org_id="org")
    assert cancelled == ["rev-drill-1"]
    session.flush()
    assert queued.status == OutboxStatus.CANCELLED
    # 在途 SENDING（撤权取消不到）→ 发送前重验拦截（DE-41 check_send_allowed）
    inflight = enqueue(session, intent="rev-drill-2", tenant="default", kind="reply",
                       target_type="message", target_id="draft-1", payload={"body": "答案"})
    inflight.status = OutboxStatus.SENDING
    session.flush()
    assert decisions.check_send_allowed(session, inflight) == (False, "decision_revoked")
    # 真实回执（sent / delivery_unknown）不被检查改写（只对账，不冒充）
    sent = enqueue(session, intent="rev-drill-3", tenant="default", kind="reply",
                   target_type="message", target_id="draft-1", payload={"body": "答案"})
    sent.status = OutboxStatus.DELIVERY_UNKNOWN
    session.flush()
    assert decisions.check_send_allowed(session, sent) == (True, "")


def test_organization_pause_reaches_send_layer(session):
    """组织暂停（DE-41 pause_scope）传播到发送层：误发急停演练的组织级路径。"""
    org.bootstrap_organization(session, GOV_OWNER, org_id="org-pause",
                               title="急停演练", membership_id="m0")
    authorization.approve(session, "founder", org_id="org-pause", target_type="draft",
                          target_id="draft-p", target_revision=1, payload_hash="h",
                          actions={"send_as_bot"})
    row = enqueue(session, intent="pause-drill-1", tenant="default", kind="reply",
                  target_type="message", target_id="draft-p", payload={"body": "答案"})
    assert decisions.check_send_allowed(session, row) == (True, "")
    decisions.pause_scope(session, GOV_OWNER, org_id="org-pause", kind="organization",
                          target_id="org-pause", reason="误发复盘演练")
    session.flush()
    assert decisions.check_send_allowed(session, row) == (False, "organization_paused")


def test_correction_drill_kills_old_approval_and_reuse(session, qa_space):
    """更正演练（DE-19）：纠正后旧批准失效、旧答案不再自动复用，复问走草稿。"""
    claim = _claim(session)
    sink = ka.RecordedSink()
    first = _ask(session, event="ev1", audience="public", sink=sink, auto=True)
    assert first["delivery"]["mode"] == "draft_pending_approval"
    ka.approve_answer(session, actor_id="boss", org_id="org", space_id="support",
                      subject="退款规则",
                      answer_payload_dict=first["answer"]["payload"],
                      audience_scope="public")
    second = _ask(session, event="ev2", audience="public", sink=sink, auto=True)
    assert second["delivery"]["mode"] == "auto_send"
    # 授权人纠正：supersede 旧主张 + 撤回旧批准
    correction = ka.apply_correction(session, actor_id="boss", org_id="org",
                                     space_id="support", subject="退款规则",
                                     value="15 天无理由",
                                     source_refs=["doc:policy@v4"], supersedes=claim.claim_id)
    assert correction["claim_id"]
    assert correction["invalidated_claims"] == []
    third = _ask(session, event="ev3", audience="public", sink=sink, auto=True)
    # 旧批准被撤回 + 载荷已变 → 不再自动发送，重新走草稿批准
    assert third["delivery"]["mode"] == "draft_pending_approval"
    assert third["delivery"]["decision_id"] == ""
    sends = _send_intents(sink)
    assert len(sends) == 1 and sends[0]["decision_id"] == second["delivery"]["decision_id"]


# ════════════════════════════════════════════════════════════════════════════
# 组 4：基础试点≠搜索增强（声明分离）
# ════════════════════════════════════════════════════════════════════════════


def test_claims_separate_basic_and_search_lines():
    """basic / search 两条线声明键各自单列；当前均为 false；规则核验零违规。"""
    result = ac.verify_conclusion_claims(REPO, doc_path=FINAL_DOC)
    assert result["violations"] == []
    assert result["claims"]["basic_pilot_passed"] is False
    assert result["claims"]["search_enhancement_passed"] is False
    for key in ac.REQUIRED_CLAIM_KEYS:
        assert key in result["claims"]


def _doctored_doc(tmp_path: Path, replacements: dict[str, str]) -> Path:
    text = (REPO / FINAL_DOC).read_text(encoding="utf-8")
    for old, new in replacements.items():
        assert old in text, f"被替换片段不存在：{old}"
        text = text.replace(old, new)
    doc = tmp_path / "doctored.md"
    doc.write_text(text, encoding="utf-8")
    return doc


def test_search_benefit_claimed_while_search_not_passed_is_caught(tmp_path):
    doc = _doctored_doc(tmp_path, {
        "| search_benefit_claimed | false |": "| search_benefit_claimed | true |"})
    result = ac.verify_conclusion_claims(REPO, doc_path=str(doc))
    assert any("搜索未通过却声明搜索收益" in v for v in result["violations"])


def test_release_certificate_claimed_while_search_not_passed_is_caught(tmp_path):
    doc = _doctored_doc(tmp_path, {
        "| release_certificate_obtained | false |": "| release_certificate_obtained | true |"})
    result = ac.verify_conclusion_claims(REPO, doc_path=str(doc))
    assert any("发布证书" in v for v in result["violations"])


def test_search_pass_claim_requires_de36_done(tmp_path):
    doc = _doctored_doc(tmp_path, {
        "| search_enhancement_passed | false |": "| search_enhancement_passed | true |"})
    result = ac.verify_conclusion_claims(REPO, doc_path=str(doc))
    assert any("DE-36 非 done" in v for v in result["violations"])


def test_basic_pilot_claim_requires_de25_and_f_gates_done(tmp_path):
    doc = _doctored_doc(tmp_path, {
        "| basic_pilot_passed | false |": "| basic_pilot_passed | true |"})
    result = ac.verify_conclusion_claims(REPO, doc_path=str(doc))
    assert any("F08 现状非 done" in v for v in result["violations"])
    assert any("F09 现状非 done" in v for v in result["violations"])
    assert any("DE-25 非 done" in v for v in result["violations"])


def test_merged_lines_claim_is_always_caught(tmp_path):
    doc = _doctored_doc(tmp_path, {
        "| basic_pilot_equals_search | false |": "| basic_pilot_equals_search | true |"})
    result = ac.verify_conclusion_claims(REPO, doc_path=str(doc))
    assert any("必须为 false" in v for v in result["violations"])


def test_search_line_kept_in_own_section():
    """搜索线 DE-29～36 单独成节且不混入非搜索工单；行级校验零违规。"""
    result = ac.verify_search_line(REPO, doc_path=FINAL_DOC)
    assert result["violations"] == []
    assert set(result["statuses"]) == set(ac.SEARCH_LINE_IDS)
    assert result["statuses"]["DE-36"] != ac.STATUS_DONE


# ════════════════════════════════════════════════════════════════════════════
# 组 5：F01/F08/F09 结构化现状输出
# ════════════════════════════════════════════════════════════════════════════


def test_f_gate_structured_output_complete():
    """F01/F08/F09 每项：状态/阻塞条件/证据位置齐备；证据存在且含诚实口径标记。"""
    result = ac.f_gate_status(REPO, doc_path=FINAL_DOC)
    assert result["violations"] == []
    gates = {gate["id"]: gate for gate in result["gates"]}
    assert set(gates) == set(ac.F_GATE_IDS)
    for gid, gate in gates.items():
        assert gate["status"] in {"done", "partial", "blocked", "pending"}
        assert gate["status_detail"]
        assert gate["evidence"], f"{gid} 无证据位置"
        for path in gate["evidence"]:
            assert (REPO / path).is_file()
        if gate["status"] != "done":
            assert len(gate["blocked_on"].strip()) >= 8
    # F08/F09 当前诚实口径：未完成（状态表/证据文档明确"不得宣称"）
    assert gates["F08"]["status"] != "done"
    assert gates["F09"]["status"] != "done"
    assert "自然日" in gates["F08"]["blocked_on"]
    assert ("60" in gates["F09"]["blocked_on"]) or ("样本" in gates["F09"]["blocked_on"])


def test_f_gate_missing_row_is_caught(tmp_path):
    """负例：缺 F09 行时结构化核对必须报缺。"""
    text = (REPO / FINAL_DOC).read_text(encoding="utf-8")
    lines = text.splitlines()
    drop = [line for line in lines if line.startswith("| F09 |")]
    assert len(drop) == 1
    doc = tmp_path / "no-f09.md"
    doc.write_text("\n".join(line for line in lines if line != drop[0]), encoding="utf-8")
    result = ac.f_gate_status(REPO, doc_path=str(doc))
    assert any("缺 F09" in v for v in result["violations"])


def test_acceptance_check_cli_end_to_end():
    """端到端：判定脚本 CLI 对当前文档零违规、退出码 0、JSON 结构完整。"""
    import subprocess
    proc = subprocess.run(
        [sys.executable, "-I", str(CHECKER_PATH), "--repo", str(REPO), "--json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120, env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads(proc.stdout)
    assert report["ok"] is True
    assert report["checks"]["status_table"]["row_count"] == 45
    assert len(report["checks"]["f_gate_status"]["gates"]) == 3
    assert report["fatal"] == []
