"""Experience 的受限重建投影与独立 Skill 的候选导回。

Experience 是业务事实的唯一写入者。投影先按 tenant/space/owner/audience
分区；显式个人根绑定其主体，其余分区位于生产发现根之外。投影写入复用原生
holdout、原子切换、版本快照与审计，所有目录显式传入，不切换进程全局环境。
候选保持候选身份，批准经验不自动把整个投影晋升 core；托管投影不可反向导入
自我放大，也不可经普通 evolve 覆盖。完整 scoped ref 的原生消费者迁移另见
DE-09/18；此模块不宣称已完成所有检索/评分注册表的组织隔离。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# 领域关键字 → 领域名。顺序即优先级，命中即归类。
# 刻意保持"少而稳"：领域太多会让每个 skill 只装一条，检索反而变差。
DOMAIN_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("hr", ("年假", "请假", "假期", "考勤", "加班", "调休", "入职", "离职", "转正",
            "社保", "公积金", "薪资", "工资", "绩效", "招聘", "简历", "休假")),
    ("it", ("vpn", "网络", "密码", "账号", "登录", "邮箱", "电脑", "系统", "软件",
            "权限", "it", "打印机", "门禁", "堡垒机", "重装", "蓝屏", "报错")),
    ("finance", ("报销", "发票", "付款", "费用", "预算", "借款", "差旅", "对公",
                 "财务", "税务", "开票", "采购")),
    ("admin", ("会议室", "工位", "办公", "快递", "食堂", "停车", "行政", "名片",
               "资产", "领用", "访客", "门卡")),
]

DEFAULT_DOMAIN = "general"

# 经验状态 → skill 层次（**仅作期望标记，不自动改 tier**）
#
# 为什么不当场改 tier：skill_tiers 的设计是"自进化产物一律进 experimental，
# 晋升靠人工 manual_promote"。自动把 approved_auto 的经验提成 core，
# 等于让系统自己扩张权限，与上游安全模型冲突。
# 这里只把"期望层次"写进 skill 的 tags/when-to-use，由运维决定何时晋升。
STATUS_TIER = {
    "approved_auto": "core",
    "assist": "staging",
    "candidate": "staging",
}

PUBLISHED_COUNT_THRESHOLD = 3  # 达到这个已发布数量，才建议晋升

SKILL_VERSION = "1.0.0"


@dataclass
class DomainSyncResult:
    domain: str
    slug: str
    action: str          # created | evolved | unchanged | skipped
    experience_ids: list[int]
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "slug": self.slug,
            "action": self.action,
            "experienceIds": self.experience_ids,
            "detail": self.detail,
        }


# ---------------- 领域归类 ----------------

def classify_domain(text: str, category: str = "") -> str:
    """把一句话归到某个领域。命中越多关键字越可信，取最高分。"""
    raw = str(text or "").lower()
    if not raw:
        return DEFAULT_DOMAIN
    best, best_hits = DEFAULT_DOMAIN, 0
    for domain, words in DOMAIN_RULES:
        hits = sum(1 for word in words if word in raw)
        if hits > best_hits:
            best, best_hits = domain, hits
    if best_hits:
        return best
    # 没命中关键字时退回分类名，避免所有经验挤进 general
    cat = str(category or "").strip().lower()
    return cat if cat and cat != "unknown" else DEFAULT_DOMAIN


def domain_slug(domain: str) -> str:
    """skill 目录名：固定前缀 + 领域，便于人工识别与清理。"""
    safe = re.sub(r"[^a-z0-9_-]+", "-", str(domain or DEFAULT_DOMAIN).lower()).strip("-")
    return f"feishu-{safe or DEFAULT_DOMAIN}"


def skill_name_for(domain: str) -> str:
    return domain_slug(domain)


# ---------------- skill 内容构造 ----------------

def _clip(text: object, limit: int = 400) -> str:
    value = re.sub(r"\s+", " ", str(text or "").strip())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _section_for(exp: dict[str, Any]) -> str:
    """单条经验在 skill 正文里的分节。"""
    question = _clip(exp.get("question"), 200)
    answer = _clip(exp.get("answer"), 2000)
    steps = _clip(exp.get("steps"), 800)
    pre = _clip(exp.get("preconditions"), 400)
    neg = exp.get("negative_examples") or []
    lines = [f"### {question}", "", f"**答**：{answer}"]
    if pre:
        lines.append(f"**前提**：{pre}")
    if steps:
        lines.append(f"**步骤**：{steps}")
    if neg:
        lines.append("**易混淆（不要用这条回答）**：" + "；".join(_clip(n, 80) for n in neg[:3]))
    lines.append("")
    lines.append(
        f"_来源：飞书经验 #{exp.get('id')}（{exp.get('status')}）_"
    )
    lines.append("")
    return "\n".join(lines)


def build_skill_body(domain: str, experiences: Iterable[dict[str, Any]]) -> str:
    items = sorted(
        [e for e in experiences if _clip(e.get("question")) and _clip(e.get("answer"))],
        key=lambda e: (-int(e.get("hit_count") or 0), int(e.get("id") or 0)),
    )
    header = [
        f"# {domain} 常见问题",
        "",
        "本技能由飞书问答经验按原权限自动投影。候选与已批准条目分别标记，",
        "回答时请优先引用对应条目；条目里标记「易混淆」的问法不要套用。",
        "",
        f"共 {len(items)} 条经验。",
        "",
    ]
    if not items:
        header.append("（暂无经验。收到相关问题时按保守口径回复并请人工确认。）")
        header.append("")
    return "\n".join(header) + "\n" + "\n".join(_section_for(e) for e in items)


def build_skill_description(domain: str, count: int) -> str:
    return f"{domain} 领域的经验投影（{count} 条；批准状态见各条来源）"


def build_when_to_use(domain: str) -> str:
    words = next((w for d, w in DOMAIN_RULES if d == domain), ())
    hint = "、".join(words[:6]) if words else "通用问题"
    return f"当用户询问 {domain} 领域的事务时使用（例如：{hint}）"


def compute_content_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


EVOLUTION_MARKERS = ("## Evolution Notes", "## Creation Evidence")


def strip_evolution_notes(body: str) -> str:
    """剥掉 skill 体系自己追加的账本段落（演进记录 / 创建证据），并归一首尾空白。

    这些段落属于 skill 历史，不属于我们同步的知识内容。拿它们一起做哈希，
    会导致"内容没变"永远判成"需要演进"，于是每次同步都白写一遍、
    版本号无限增长、usage 日志被刷爆（2026-11-11 实测）。

    归一空白是必需的：frontmatter 之后正文以空行开头，而生成的 body 不带，
    只差两个换行也会让哈希对不上。
    """
    cut = len(body)
    for marker in EVOLUTION_MARKERS:
        index = body.find(marker)
        if index != -1:
            cut = min(cut, index)
    return body[:cut].strip()


# ---------------- 与 skill_evolution 的桥 ----------------

def _import_evolution():
    """导入 agents.skill_evolution。

    飞书 agent 通常以 feishu-daily-agent 为工作目录运行，而 ``agents`` 包在
    仓库根目录。与 retrieval.py 同口径：按 EVOHARNESS_ROOT 或相对路径推导后
    插进 sys.path，找不到就明确报错（不要静默降级成"同步成功"）。
    """

    root = (os.environ.get("EVOHARNESS_ROOT") or "").strip()
    if not root:
        candidate = Path(__file__).resolve().parents[3]
        if (candidate / "agents" / "skill_evolution.py").is_file():
            root = str(candidate)
    if root and root not in sys.path:
        sys.path.insert(0, root)
    from agents import skill_evolution as se  # noqa: PLC0415

    return se


def _import_search_contracts():
    """导入 agents.skill_search.contracts（DE-29 搜索身份门，单一判定口径）。"""
    root = (os.environ.get("EVOHARNESS_ROOT") or "").strip()
    if not root:
        candidate = Path(__file__).resolve().parents[3]
        if (candidate / "agents" / "skill_search" / "contracts.py").is_file():
            root = str(candidate)
    if root and root not in sys.path:
        sys.path.insert(0, root)
    from agents.skill_search import contracts as search_contracts  # noqa: PLC0415

    return search_contracts


def search_target_admission(slug: str, *, scope=None) -> dict[str, Any]:
    """DE-29 投影接缝：托管投影确认不可作为搜索目标（拒绝并说明原因）。

    producer_kind 语义沿用 DE-06/DE-18：``projection`` 由源服务重建
    （sync_domain → write_managed_projection），搜索不得认领投影正文——
    源修订只能走原投影链重建；``executable_sop`` / ``direct``（带责任人确认）
    才可进入搜索。判定复用 agents.skill_search.contracts 的单一口径，
    不在此处复制第二套规则；根目录取本 scope 的投影根。
    """
    from app.memory.projection_scope import default_scope, root_for

    scope_obj = scope or default_scope()
    root = root_for(scope_obj)
    normalized = str(slug or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", normalized):
        return {"ok": False, "searchable": False, "slug": slug, "reason": "skill_slug_invalid"}
    skill_dir = root / normalized
    if not skill_dir.is_dir() or not (skill_dir / "SKILL.md").is_file():
        return {"ok": False, "searchable": False, "slug": normalized, "reason": "skill_not_found"}
    contracts = _import_search_contracts()
    classification = contracts.classify_producer_kind(skill_dir)
    kind = str(classification.get("producer_kind") or "unknown")
    if kind == "projection":
        return {
            "ok": False,
            "searchable": False,
            "slug": normalized,
            "producer_kind": "projection",
            "evidence": classification.get("evidence", []),
            "reason": "managed_projection_not_searchable",
            "detail": "投影由源服务重建（producer-kind=projection / managed-by=experience.skill_sync）："
                      "搜索不得认领投影正文，源修订走原投影链（skill_sync）重建。",
        }
    if kind == "unknown":
        return {
            "ok": False,
            "searchable": False,
            "slug": normalized,
            "producer_kind": kind,
            "reason": "unknown_identity_not_searchable",
            "detail": "身份不明（无 producer-kind）：组织自动搜索拒绝认领存量不明文件，先诊断并明确技能身份。",
        }
    result = {"ok": True, "searchable": True, "slug": normalized, "producer_kind": kind}
    if kind == "direct":
        # direct 可搜索，但建立 SearchManifest 时仍须携带责任人确认标记（contracts 门）
        result["requires_owner_confirmation"] = True
    return result


def sync_domain(domain: str, experiences: list[dict[str, Any]], *, actor: str = "feishu", scope=None) -> DomainSyncResult:
    """Partition by permission intersection before invoking the source-owned writer."""
    from app.memory.projection_scope import ProjectionScope, default_scope, root_for, bind_root
    if scope is None:
        groups = {}
        for exp in experiences:
            item_scope = ProjectionScope.from_payload(exp) if any(k in exp for k in ("tenant", "owner", "visibility", "space_id")) else default_scope()
            groups.setdefault(item_scope, []).append(exp)
        if not groups:
            groups[default_scope()] = []
        if len(groups) > 1:
            results = [sync_domain(domain, items, actor=actor, scope=key).as_dict() for key, items in groups.items()]
            return DomainSyncResult(domain, domain_slug(domain), "partitioned", [], json.dumps(results, ensure_ascii=False))
        scope, experiences = next(iter(groups.items()))
    for exp in experiences:
        if any(k in exp for k in ("tenant", "owner", "visibility", "space_id")) and ProjectionScope.from_payload(exp) != scope:
            raise ValueError("projection_scope_mismatch")
    se = _import_evolution()
    if se.skill_state_read_only():
        return DomainSyncResult(domain, domain_slug(domain), "skipped", [], "holdout read-only mode")
    root = root_for(scope)
    bind_root(root, scope)
    slug = domain_slug(domain)
    body = build_skill_body(domain, experiences)
    sources = [dict(id=e.get("id"), version=e.get("version", 1),
                    source_refs=e.get("source_refs", []), status=e.get("status", "candidate")) for e in experiences]
    result = se.write_managed_projection(skills_root=root,
        evolution_root=root.parent / (".projection-state-" + scope.key), name=slug,
        description=build_skill_description(domain, len(experiences)), instructions=body,
        when_to_use=build_when_to_use(domain), scope=scope.as_dict(), source_revisions=sources, actor=actor)
    if not result.get("ok"):
        return DomainSyncResult(domain, slug, "skipped", [], result.get("error", "projection_failed"))
    try:
        from agents.skills import reset_skill_cache
        reset_skill_cache()
    except ImportError:
        pass
    return DomainSyncResult(domain, slug, result["action"], sorted(int(e.get("id") or 0) for e in experiences))


def list_skill_domains(*, scope=None) -> list[dict[str, Any]]:
    from app.memory.projection_scope import default_scope, root_for
    scope = scope or default_scope()
    root = root_for(scope)
    binding = root / ".projection-scope.json"
    if not binding.is_file() or json.loads(binding.read_text(encoding="utf-8")) != scope.as_dict():
        return []
    result = []
    for path in sorted(root.glob("feishu-*/SKILL.md")):
        meta_path = path.parent / ".projection.json"
        if not meta_path.is_file():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("scope") != scope.as_dict():
            continue
        result.append(dict(slug=path.parent.name, domain=meta["domain"], path=str(path),
                           bytes=path.stat().st_size, scope=scope.as_dict(), projection_hash=meta["projection_hash"]))
    return result


def tier_of(domain: str) -> str:
    # Publishing one Experience does not certify an entire projection as a core SOP.
    return "experimental" if any(x["domain"] == domain for x in list_skill_domains()) else "unknown"


def promotion_hint(domain: str, experiences: Iterable[dict[str, Any]]) -> str:
    """给出晋升建议（不执行晋升）。

    上游的 tier 体系要求"人工放行"，所以这里只回答"够不够格提"：
    已发布经验达到阈值、且没有仍在候选状态的条目时才建议晋升。
    """
    items = list(experiences)
    published = sum(1 for e in items if e.get("status") == "approved_auto")
    pending = sum(1 for e in items if e.get("status") in {"candidate", "assist"})
    if published >= PUBLISHED_COUNT_THRESHOLD and pending == 0:
        return f"可考虑晋升：{published} 条已发布经验，无待验证条目"
    return (
        f"维持实验区：已发布 {published} 条（阈值 {PUBLISHED_COUNT_THRESHOLD}），"
        f"待验证 {pending} 条"
    )


# ---------------- skill → 飞书（反向） ----------------

def parse_skill_sections(text: str) -> list[dict[str, str]]:
    """把 skill 正文按 ``### 问题`` 切成问答对。"""
    pairs: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in text.splitlines():
        if line.startswith("### "):
            if current and current.get("question"):
                pairs.append(current)
            current = {"question": line[4:].strip(), "answer": "", "raw": ""}
            continue
        if current is None:
            continue
        current["raw"] += line + "\n"
        if line.startswith("**答**："):
            current["answer"] = line[len("**答**："):].strip()
    if current and current.get("question"):
        pairs.append(current)
    return [p for p in pairs if p.get("question") and p.get("answer")]


def import_skill_as_candidates(slug: str, *, tenant: str, created_by: str = "skill-import") -> dict[str, Any]:
    """Import a local worker's independent skill as private candidates only."""
    from app.core.database import session_scope
    from app.memory import experience as experience_module
    from app.memory.projection_scope import default_scope, root_for
    from app.models import Experience
    scope = default_scope()
    if tenant != scope.tenant or not re.fullmatch(r"[A-Za-z0-9_-]+", slug):
        return {"ok": False, "error": "skill_import_scope_mismatch"}
    root = root_for(scope)
    binding = root / ".projection-scope.json"
    if binding.is_file() and json.loads(binding.read_text(encoding="utf-8")) != scope.as_dict():
        return {"ok": False, "error": "projection_root_scope_mismatch"}
    path = root / slug / "SKILL.md"
    if not path.is_file():
        return {"ok": False, "error": "skill_not_found"}
    if (path.parent / ".projection.json").is_file():
        return {"ok": True, "slug": slug, "created": [], "skipped": ["managed_projection_source_owned"]}
    text = path.read_text(encoding="utf-8")
    pairs = parse_skill_sections(text.split("---", 2)[-1])
    created, skipped = [], []
    origin = hashlib.sha256(text.encode("utf-8")).hexdigest()
    with session_scope() as session:
        for pair in pairs:
            question = pair["question"].strip()
            existed = session.query(Experience).filter(
                Experience.tenant == tenant, Experience.owner == scope.owner,
                Experience.question_pattern == question).first()
            if existed is not None:
                skipped.append(question[:40])
                continue
            candidate = experience_module.create_candidate(
                session, question=question, answer=pair["answer"], tenant=tenant,
                owner=scope.owner, visibility="private", sensitive_level="private",
                category="faq", created_by=created_by,
                reason=f"skill-import:{slug}:{origin}")
            created.append(candidate.id)
    return {"ok": True, "slug": slug, "created": created, "skipped": skipped}
