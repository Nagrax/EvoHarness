"""飞书经验 ↔ skill 双向融合回归（B 方法，多层设计）。

背景（2026-11-11 用户指出的架构割裂）：飞书沉淀问答对、EvoHarness 沉淀 skill，
两套各自演化互不相通。B 方法要求同一份知识双向可读写。

多层设计的关键约束（不是"一个 skill 装全部"）：
- 按领域分文件：hr / it / finance / admin / general
- 按状态分层次：approved_auto→core，candidate/assist→staging
- 同领域的新经验走 evolve 追加，不新建 skill（避免碎片化）
"""
from __future__ import annotations

from app.core.database import session_scope
from app.memory import experience as experience_module
from app.memory import skill_bridge, skill_sync
from tests.helpers import incoming

OWNER = "ou_skill_owner"
CHAT = "oc_skill_chat"


def _setup(settings, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "feishu_owner_open_id", OWNER)
    monkeypatch.setattr(settings, "feishu_owner_chat_id", CHAT)
    monkeypatch.setenv("EVOHARNESS_SKILLS_ROOT", str(tmp_path / "skills"))


# ---------------- 领域归类 ----------------


def test_classify_domain_by_keywords():
    assert skill_bridge.classify_domain("请问年假怎么申请") == "hr"
    assert skill_bridge.classify_domain("VPN 连不上") == "it"
    assert skill_bridge.classify_domain("报销发票要几张") == "finance"
    assert skill_bridge.classify_domain("会议室怎么预订") == "admin"
    assert skill_bridge.classify_domain("今天天气如何") == "general"


def test_domain_slug_is_prefixed_and_stable():
    assert skill_bridge.domain_slug("hr") == "feishu-hr"
    assert skill_bridge.domain_slug("HR") == "feishu-hr"
    assert skill_bridge.skill_name_for("it") == "feishu-it"


# ---------------- 单领域同步 ----------------


def test_first_sync_creates_skill_file(settings, monkeypatch, tmp_path):
    _setup(settings, monkeypatch, tmp_path)
    domain = "hr"
    items = [{"id": 1, "question": "年假怎么申请", "answer": "在 OA 提交", "status": "approved_auto"}]
    result = skill_bridge.sync_domain(domain, items)
    assert result.action == "created"
    path = tmp_path / "skills" / "feishu-hr" / "SKILL.md"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "年假怎么申请" in text
    assert "在 OA 提交" in text
    assert "hr 领域" in text or "hr" in text


def test_second_sync_with_same_content_is_unchanged(settings, monkeypatch, tmp_path):
    _setup(settings, monkeypatch, tmp_path)
    items = [{"id": 1, "question": "年假怎么申请", "answer": "在 OA 提交", "status": "approved_auto"}]
    skill_bridge.sync_domain("hr", items)
    again = skill_bridge.sync_domain("hr", items)
    assert again.action == "unchanged", "内容没变不该反复写盘"


def test_changed_content_evolves_existing_skill(settings, monkeypatch, tmp_path):
    _setup(settings, monkeypatch, tmp_path)
    skill_bridge.sync_domain(
        "it", [{"id": 2, "question": "VPN 连不上", "answer": "先重启客户端", "status": "approved_auto"}]
    )
    evolved = skill_bridge.sync_domain(
        "it",
        [
            {"id": 2, "question": "VPN 连不上", "answer": "先重启客户端", "status": "approved_auto"},
            {"id": 3, "question": "密码忘了", "answer": "走自助重置", "status": "candidate"},
        ],
    )
    assert evolved.action == "evolved"
    text = (tmp_path / "skills" / "feishu-it" / "SKILL.md").read_text(encoding="utf-8")
    assert "密码忘了" in text
    assert "自助重置" in text


def test_multiple_domains_produce_separate_skills(settings, monkeypatch, tmp_path):
    """核心诉求：多方面多层次，不是一个大杂烩。"""
    _setup(settings, monkeypatch, tmp_path)
    skill_bridge.sync_domain("hr", [{"id": 1, "question": "年假几天", "answer": "5 天"}])
    skill_bridge.sync_domain("it", [{"id": 2, "question": "VPN 怎么连", "answer": "用客户端"}])
    skill_bridge.sync_domain("finance", [{"id": 3, "question": "报销要发票吗", "answer": "需要"}])
    domains = {item["domain"] for item in skill_bridge.list_skill_domains()}
    assert {"hr", "it", "finance"} <= domains
    # 每个文件只装自己领域的内容
    hr_text = (tmp_path / "skills" / "feishu-hr" / "SKILL.md").read_text(encoding="utf-8")
    assert "VPN" not in hr_text


# ---------------- 经验生命周期联动 ----------------


def test_publish_projects_experience_into_skill(settings, monkeypatch, tmp_path):
    _setup(settings, monkeypatch, tmp_path)
    with session_scope() as session:
        exp = experience_module.create_candidate(
            session, question="请假流程是怎样的", answer="先在 OA 提交申请"
        )
        experience_module.publish(session, exp.id, actor=OWNER)
    path = tmp_path / "skills" / "feishu-hr" / "SKILL.md"
    assert path.is_file(), "发布经验后必须自动投影出 skill"
    assert "请假流程是怎样的" in path.read_text(encoding="utf-8")


def test_pause_removes_experience_from_skill(settings, monkeypatch, tmp_path):
    _setup(settings, monkeypatch, tmp_path)
    with session_scope() as session:
        exp = experience_module.create_candidate(
            session, question="年假怎么申请", answer="OA 提交"
        )
        experience_module.publish(session, exp.id, actor=OWNER)
    path = tmp_path / "skills" / "feishu-hr" / "SKILL.md"
    assert "年假怎么申请" in path.read_text(encoding="utf-8")

    with session_scope() as session:
        experience_module.pause(session, exp.id, actor=OWNER, reason="内容过时")
    text = path.read_text(encoding="utf-8")
    assert "年假怎么申请" not in text, "停用后不能继续把答案喂给 Agent"


def test_domain_sync_picks_latest_version(settings, monkeypatch, tmp_path):
    _setup(settings, monkeypatch, tmp_path)
    with session_scope() as session:
        v1 = experience_module.create_candidate(
            session, question="报销多久到账", answer="一周"
        )
        experience_module.publish(session, v1.id, actor=OWNER)
        v2 = experience_module.create_candidate(
            session, question="报销多久到账", answer="三个工作日",
            intent_id=v1.intent_id, previous_id=v1.id,
        )
        experience_module.publish(session, v2.id, actor=OWNER)
        skill_sync.sync_domain(session, "finance")
    text = (tmp_path / "skills" / "feishu-finance" / "SKILL.md").read_text(encoding="utf-8")
    assert "三个工作日" in text
    assert "一周" not in text, "旧版本不该覆盖新版本"


# ---------------- 反向导入 ----------------


def test_import_skill_creates_candidates_not_published(settings, monkeypatch, tmp_path):
    _setup(settings, monkeypatch, tmp_path)
    skill_dir = tmp_path / "skills" / "feishu-it"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: feishu-it\ndescription: IT\n---\n\n"
        "# IT\n\n### 打印机卡纸怎么办\n\n**答**：打开后盖取出纸张\n\n",
        encoding="utf-8",
    )
    result = skill_bridge.import_skill_as_candidates(
        "feishu-it", tenant=settings.tenant_id
    )
    assert result["ok"] is True
    assert result["created"], "应导入成候选"

    from app.models import Experience

    with session_scope() as session:
        exp = session.get(Experience, result["created"][0])
        assert exp.status == "candidate", "导入只能是候选，必须仍要人工批准"
        assert exp.question_pattern == "打印机卡纸怎么办"


def test_import_is_idempotent(settings, monkeypatch, tmp_path):
    _setup(settings, monkeypatch, tmp_path)
    skill_dir = tmp_path / "skills" / "feishu-it"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: feishu-it\n---\n\n### 邮箱怎么配\n\n**答**：用 Exchange\n",
        encoding="utf-8",
    )
    first = skill_bridge.import_skill_as_candidates("feishu-it", tenant=settings.tenant_id)
    second = skill_bridge.import_skill_as_candidates("feishu-it", tenant=settings.tenant_id)
    assert first["created"]
    assert not second["created"], "同一问题不该重复导入"
    assert second["skipped"]


# ---------------- DE-29 搜索接缝：托管投影不可作为搜索目标 ----------------


def test_managed_projection_rejected_as_search_target(settings, monkeypatch, tmp_path):
    """投影接缝：producer-kind=projection 的托管投影拒绝进入搜索并说明原因。"""
    _setup(settings, monkeypatch, tmp_path)
    skill_bridge.sync_domain(
        "hr", [{"id": 1, "question": "年假怎么申请", "answer": "在 OA 提交", "status": "approved_auto"}]
    )
    result = skill_bridge.search_target_admission("feishu-hr")
    assert result["ok"] is False
    assert result["searchable"] is False
    assert result["reason"] == "managed_projection_not_searchable"
    assert result["producer_kind"] == "projection"
    assert (tmp_path / "skills" / "feishu-hr" / ".projection.json").is_file()
    assert any("sidecar" in item or "producer-kind" in item for item in result["evidence"])
    assert "投影" in result["detail"], "拒绝必须说明原因（投影由源服务重建）"


def test_independent_sop_admitted_and_unknown_rejected(settings, monkeypatch, tmp_path):
    """executable_sop 可进入搜索；身份不明存量文件拒绝认领（先诊断）。"""
    _setup(settings, monkeypatch, tmp_path)
    sop_dir = tmp_path / "skills" / "deploy-sop"
    sop_dir.mkdir(parents=True)
    (sop_dir / "SKILL.md").write_text(
        "---\nname: deploy-sop\ndescription: 独立部署 SOP\nversion: 0.1.0\nproducer-kind: executable_sop\n---\n\n# 步骤\n\n1. 检查发布单。\n",
        encoding="utf-8",
    )
    admitted = skill_bridge.search_target_admission("deploy-sop")
    assert admitted["ok"] is True and admitted["producer_kind"] == "executable_sop"

    unknown_dir = tmp_path / "skills" / "legacy-note"
    unknown_dir.mkdir(parents=True)
    (unknown_dir / "SKILL.md").write_text(
        "---\nname: legacy-note\ndescription: 存量不明\nversion: 0.1.0\n---\n\n旧内容。\n",
        encoding="utf-8",
    )
    rejected = skill_bridge.search_target_admission("legacy-note")
    assert rejected["ok"] is False and rejected["reason"] == "unknown_identity_not_searchable"

    assert skill_bridge.search_target_admission("no-such-skill")["reason"] == "skill_not_found"
    assert skill_bridge.search_target_admission("../escape")["reason"] == "skill_slug_invalid"
