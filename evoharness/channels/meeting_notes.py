"""获授权会议纪要导入：发言人/时间锚点保留、候选化抽取与待办映射（DE-22）。

- 纪要导入必须持有效授权决定（逐版本+逐载荷）并携带授权参会人名单；
  未授权参会人的段落按授权范围在入库前裁剪，不进入共同知识。
- 目标/决定/待办只形成候选（proposals）：不直接写知识或工作项终态；
  关键决定冲突/缺主体 → 呈报对应负责人核对；ASR 不同版本决定保留冲突。
- 待办映射到工作项只写提案（不改变工作项状态），确认后才经补充队列生效，
  按 source event（proposal_id）幂等；可回溯原段落 anchor。
- mode 固定 minutes_import：纪要导入不等于实时会议参与（DE-26 另列）。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Protocol

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from evoharness.clock import utcnow
from evoharness.governance import authorization
from evoharness.ids import normalize_text, sha256_json, sha256_text, stable_id
from evoharness.knowledge import service as knowledge_service
from evoharness.models import Base, Task, TaskEvent, TaskStatus
from evoharness.work import service as work_service
from evoharness.work import supplements
from evoharness.channels.documents import scan_injection


class MinutesError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ImportedMinutes(Base):
    """导入的授权纪要：入库前已按授权参会人裁剪；mode 固定 minutes_import。"""
    __tablename__ = "channel_minutes"

    minutes_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    space_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    # 裁剪后的段落：[{index,speaker,time,text,unattributed,restricted,untrusted}]
    paragraphs_json: Mapped[str] = mapped_column(Text, default="[]")
    attendees_json: Mapped[str] = mapped_column(Text, default="[]")
    authorized_scopes_json: Mapped[str] = mapped_column(Text, default="[]")
    knowledge_scope: Mapped[str] = mapped_column(String(128), default="private")
    injection_flags_json: Mapped[str] = mapped_column(Text, default="[]")
    versions_json: Mapped[str] = mapped_column(Text, default="[]")
    meeting_ref: Mapped[str] = mapped_column(String(255), default="")
    source_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    mode: Mapped[str] = mapped_column(String(24), default="minutes_import")
    state: Mapped[str] = mapped_column(String(16), default="active", index=True)
    degraded: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class MinuteProposal(Base):
    """纪要候选（目标/决定/待办）：确认前不触碰知识/工作项终态。"""
    __tablename__ = "channel_minute_proposals"
    __table_args__ = (UniqueConstraint("minutes_id", "proposal_key", name="uq_minute_proposal_key"),)

    proposal_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    proposal_key: Mapped[str] = mapped_column(String(64), default="")
    minutes_id: Mapped[str] = mapped_column(String(64), index=True)
    minutes_ref: Mapped[str] = mapped_column(String(80), default="")
    org_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    space_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    kind: Mapped[str] = mapped_column(String(16))            # goal / decision / todo
    subject: Mapped[str] = mapped_column(String(255), default="")
    value: Mapped[str] = mapped_column(Text, default="")
    due_text: Mapped[str] = mapped_column(String(64), default="")
    anchor_json: Mapped[str] = mapped_column(Text, default="{}")
    work_item_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    reviewer: Mapped[str] = mapped_column(String(128), default="")
    owner_scope: Mapped[str] = mapped_column(String(128), default="")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    confirmed_by: Mapped[str] = mapped_column(String(64), default="")
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolution_note: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


# ── 注入式解析端口：ASR 纯文本 → 段落（发言人/时间锚点） ─────────────────────

class MinutesParser(Protocol):
    """解析端口：只接收文本、只返回段落结构化数据，不得有副作用。"""

    def parse(self, text: str) -> list[dict]:  # pragma: no cover - 端口定义
        ...


_SPEAKER_BRACKET_RE = re.compile(
    r"^\s*【(?P<speaker>[^】]{1,30})】\s*(?:[(（](?P<time>[^)）]{1,20})[)）])?\s*(?P<rest>.*)$")
_SPEAKER_COLON_RE = re.compile(r"^\s*(?P<speaker>[^:：]{1,20})[:：]\s*(?P<rest>.*)$")
_TIME_PREFIX_RE = re.compile(r"^\s*[(（]?(?P<time>\d{1,2}:\d{2})[)）]?\s*")
# 说话人前缀不能是结构关键词，避免把"决定：X"误判成说话人
_KEYWORD_PREFIXES = ("决定", "决议", "目标", "待办", "行动项", "任务", "结论", "议程",
                     "会议", "纪要", "topic", "decision", "goal", "todo", "action")


class SpeakerPrefixParser:
    """默认受限解析：【张三】(10:32) 文本 / 张三：文本 / (10:32) 文本。"""

    def parse(self, text: str) -> list[dict]:
        paragraphs: list[dict] = []
        index = 0
        for raw_line in (text or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            speaker, time, body = "", "", line
            bracket = _SPEAKER_BRACKET_RE.match(line)
            if bracket:
                speaker = bracket.group("speaker").strip()
                time = (bracket.group("time") or "").strip()
                body = bracket.group("rest").strip()
            else:
                time_prefix = _TIME_PREFIX_RE.match(body)
                if time_prefix:
                    time = time_prefix.group("time")
                    body = body[time_prefix.end():].strip()
                colon = _SPEAKER_COLON_RE.match(body)
                if colon and not colon.group("speaker").strip().lower().startswith(_KEYWORD_PREFIXES):
                    speaker = colon.group("speaker").strip()
                    body = colon.group("rest").strip()
            paragraphs.append({"index": index, "speaker": speaker,
                               "time": time, "text": body})
            index += 1
        return paragraphs


# ── 候选抽取：确定性关键词规则，离线、无模型、无执行 ─────────────────────────

_GOAL_RE = re.compile(r"^(?:目标|goal|aim)\s*[:：]\s*(?P<value>.+)$", re.I)
_DECISION_RE = re.compile(r"^(?:决定|决议|拍板|敲定|agreed|decided)\s*[:：]?\s*(?P<value>.+)$", re.I)
_DECIDER_RE = re.compile(r"由\s*(?P<who>[\w\u4e00-\u9fff·@]{1,20})\s*(?:决定|拍板|敲定)")
_TODO_RE = re.compile(r"^(?:待办|行动项|action\s*item|todo|跟进事项)\s*[:：]\s*(?P<value>.+)$", re.I)
_ASSIGNEE_RE = re.compile(r"(?:(?:由|让|请)\s*)?(?P<who>[\w\u4e00-\u9fff·@]{1,20})\s*(?:负责|跟进|执行|落实)")
_MENTION_RE = re.compile(r"@(?P<who>[\w\u4e00-\u9fff·]{1,20})")
_DUE_RE = re.compile(r"(?:截止|期限|deadline|due)\s*[:：]?\s*(?P<due>[^\s。；;，,]{1,20})", re.I)
_WORK_ITEM_RE = re.compile(r"(?:工作项|task)\s*[#＃]?\s*(?P<id>\d+)", re.I)
_SEGMENT_SPLIT_RE = re.compile(r"[。；;\n]+")


def minutes_source_ref(minutes_id: str, revision: int) -> str:
    return f"channel-minutes:{minutes_id}@{int(revision)}"


def minutes_payload_hash(*, minutes_id: str, title: str, version: int, paragraphs: list[dict],
                         authorized_attendees: list[str], meeting_ref: str = "") -> str:
    """授权载荷指纹：裁剪前后无关，绑到本次提交的段落+名单+版本。"""
    return sha256_json({"minutes_id": minutes_id, "title": title, "version": int(version),
                        "paragraphs": paragraphs, "authorized_attendees": sorted(authorized_attendees),
                        "meeting_ref": meeting_ref})


def approve_minutes_import(session: Session, actor_id: str, *, org_id: str, minutes_id: str,
                           title: str, version: int, paragraphs: list[dict] | None = None,
                           text: str | None = None, parser: MinutesParser | None = None,
                           authorized_attendees: list[str], meeting_ref: str = "",
                           grantee_id: str = "", expires_at: datetime | None = None):
    """为一次纪要导入签发授权决定（动作 import，绑定版本、段落与参会名单）。"""
    normalized = _normalized_source(paragraphs, text, parser)
    return authorization.approve(
        session, actor_id, org_id=org_id, target_type="channel_minutes",
        target_id=minutes_id, target_revision=int(version),
        payload_hash=minutes_payload_hash(minutes_id=minutes_id, title=title, version=version,
                                          paragraphs=normalized,
                                          authorized_attendees=authorized_attendees,
                                          meeting_ref=meeting_ref),
        actions={"import"}, grantee_id=grantee_id, expires_at=expires_at)


def _normalize_paragraph(item: dict, index: int) -> dict:
    return {"index": index,
            "speaker": str(item.get("speaker") or "").strip(),
            "time": str(item.get("time") or "").strip(),
            "text": str(item.get("text") or "").strip(),
            "visibility": str(item.get("visibility") or "shared").strip().lower()}


def _normalized_source(paragraphs: list[dict] | None, text: str | None,
                       parser: MinutesParser | None) -> list[dict]:
    """approve 与 import 共用的归一入口：授权指纹与实际导入逐字节同源。"""
    if (paragraphs is None) == (text is None):
        raise MinutesError("minutes_source_ambiguous")
    raw = list(paragraphs) if paragraphs is not None else \
        (parser or SpeakerPrefixParser()).parse(str(text or ""))
    return [_normalize_paragraph(item, index)
            for index, item in enumerate(raw) if str(item.get("text") or "").strip()]


def _match_work_item(session: Session, *, space_id: str, text: str) -> int | None:
    """待办 → 工作项映射：显式 工作项#id 优先，其次归一化子串匹配（确定性、离线）。"""
    explicit = _WORK_ITEM_RE.search(text or "")
    if explicit:
        task = session.get(Task, int(explicit.group("id")))
        return task.id if task is not None else None
    if not space_id:
        return None
    normalized = normalize_text(text or "")
    if len(normalized) < 4:
        return None
    for task in session.scalars(select(Task).where(Task.space_id == space_id)):
        for field in (task.goal, task.action, task.title):
            field_normalized = normalize_text(field or "")
            if field_normalized and (field_normalized in normalized or normalized in field_normalized):
                return task.id
    return None


def _extract_candidates(paragraph: dict) -> list[dict]:
    """从单段落抽取 目标/决定/待办 候选；一个片段最多归类一种。"""
    found: list[dict] = []
    for segment in _SEGMENT_SPLIT_RE.split(paragraph.get("text") or ""):
        segment = segment.strip()
        if not segment:
            continue
        decision = _DECISION_RE.match(segment)
        todo = None if decision else _TODO_RE.match(segment)
        goal = None if (decision or todo) else _GOAL_RE.match(segment)
        if decision:
            value = decision.group("value").strip()
            decider = _DECIDER_RE.search(paragraph.get("text") or "")
            found.append({"kind": "decision", "value": value,
                          "subject": (decider.group("who") if decider else "")})
        elif todo:
            value = todo.group("value").strip()
            assignee_match = _ASSIGNEE_RE.search(value)
            assignee = assignee_match.group("who") if assignee_match else ""
            if not assignee:
                mention = _MENTION_RE.search(value)
                assignee = mention.group("who") if mention else ""
            due = _DUE_RE.search(value)
            found.append({"kind": "todo", "value": value, "subject": assignee,
                          "due": due.group("due") if due else ""})
        elif goal:
            found.append({"kind": "goal", "value": goal.group("value").strip(), "subject": ""})
    return found


def _recompute_conflicts(session: Session, *, minutes_id: str) -> list[dict]:
    """同主体不同版本的未决决定 → 全组标 conflict_review 保留，不自动裁决。"""
    pending = list(session.scalars(select(MinuteProposal).where(
        MinuteProposal.minutes_id == minutes_id,
        MinuteProposal.status.in_(["pending", "needs_owner", "conflict_review"]))))
    groups: dict[str, list[MinuteProposal]] = {}
    for proposal in pending:
        if proposal.kind != "decision":
            continue
        groups.setdefault(normalize_text(proposal.subject) or "unknown", []).append(proposal)
    conflicts: list[dict] = []
    for members in groups.values():
        values = {normalize_text(member.value) for member in members}
        if len(values) <= 1:
            continue
        for member in members:
            member.status = "conflict_review"
        conflicts.append({"subject": members[0].subject or "（未识别主体）",
                          "values": sorted({member.value for member in members}),
                          "proposal_ids": [member.proposal_id for member in members],
                          "reviewer": next((m.reviewer for m in members if m.reviewer), "")})
    session.flush()
    return conflicts


def import_minutes(session: Session, *, org_id: str, space_id: str, minutes_id: str,
                   title: str, version: int, paragraphs: list[dict] | None = None,
                   text: str | None = None, parser: MinutesParser | None = None,
                   authorized_attendees: list[str], authorized_scopes: list[str] | None = None,
                   knowledge_scope: str = "private", meeting_ref: str = "",
                   source_time: datetime | None = None, escalate_to: str = "",
                   tenant: str = "default") -> dict:
    """获授权导入纪要：授权校验 → 隐私裁剪 → 注入扫描 → 幂等落库 → 候选抽取。

    候选不直接生效；待办映射只写提案；纪要导入不标成实时会议参与。
    """
    minutes_id = str(minutes_id or "").strip()
    if not minutes_id:
        raise MinutesError("minutes_id_required")
    attendees = [str(name).strip() for name in (authorized_attendees or []) if str(name).strip()]
    if not attendees:
        raise MinutesError("authorized_attendees_required")
    version = int(version)
    if version < 1:
        raise MinutesError("minutes_version_invalid")
    normalized = _normalized_source(paragraphs, text, parser)
    payload_hash = minutes_payload_hash(minutes_id=minutes_id, title=title, version=version,
                                        paragraphs=normalized,
                                        authorized_attendees=attendees, meeting_ref=meeting_ref)
    try:
        authorization.validate(session, target_type="channel_minutes", target_id=minutes_id,
                               target_revision=version, payload_hash=payload_hash,
                               action="import")
    except authorization.AuthorizationError:
        raise MinutesError("import_not_authorized") from None

    now = utcnow()
    attendee_set = set(attendees)
    # 隐私裁剪：未授权参会人的内容在入库前丢弃，不进入共同知识
    cropped = 0
    kept: list[dict] = []
    flags: list[dict] = []
    ref = minutes_source_ref(minutes_id, version)
    for paragraph in normalized:
        speaker = paragraph["speaker"]
        if speaker and speaker not in attendee_set:
            cropped += 1
            continue
        entry = {"index": paragraph["index"], "speaker": speaker, "time": paragraph["time"],
                 "text": paragraph["text"],
                 "unattributed": not bool(speaker),
                 "restricted": paragraph["visibility"] == "private"}
        paragraph_flags = scan_injection(paragraph["text"])
        if paragraph_flags:
            entry["untrusted"] = True
            for flag in paragraph_flags:
                flag["anchor_ref"] = f"{ref}#p{entry['index']}"
            flags.extend(paragraph_flags)
        kept.append(entry)
    content_hash = sha256_json({"paragraphs": kept, "attendees": sorted(attendees),
                                "title": title, "meeting_ref": meeting_ref})

    existing = session.get(ImportedMinutes, minutes_id)
    if existing is not None and existing.state == "revoked":
        raise MinutesError("minutes_revoked")
    if existing is not None and existing.content_hash == content_hash:
        return {"ok": True, "minutes_id": minutes_id, "revision": existing.revision,
                "action": "unchanged", "cropped_paragraphs": cropped,
                "proposals": [], "conflicts": [], "review_requests": [],
                "injection_flags": json.loads(existing.injection_flags_json),
                "participation_mode": existing.mode, "realtime_participation": False,
                "source_ref": minutes_source_ref(minutes_id, existing.revision)}
    if existing is not None and version <= existing.revision:
        raise MinutesError("minutes_version_not_advancing")

    if existing is None:
        record = ImportedMinutes(
            minutes_id=minutes_id, org_id=org_id, space_id=space_id, title=title,
            revision=version, content_hash=content_hash,
            paragraphs_json=json.dumps(kept, ensure_ascii=False),
            attendees_json=json.dumps(sorted(attendee_set), ensure_ascii=False),
            authorized_scopes_json=json.dumps(
                sorted({str(scope) for scope in (authorized_scopes or [])}), ensure_ascii=False),
            knowledge_scope=knowledge_scope,
            injection_flags_json=json.dumps(flags, ensure_ascii=False),
            versions_json=json.dumps([{"revision": version, "content_hash": content_hash,
                                       "imported_at": now.isoformat(), "action": "created"}],
                                     ensure_ascii=False),
            meeting_ref=meeting_ref, source_time=source_time)
        session.add(record)
        action = "created"
    else:
        record = existing
        record.revision = version
        record.title = title or record.title
        record.content_hash = content_hash
        record.paragraphs_json = json.dumps(kept, ensure_ascii=False)
        record.authorized_scopes_json = json.dumps(
            sorted({str(scope) for scope in (authorized_scopes or [])}), ensure_ascii=False)
        record.injection_flags_json = json.dumps(flags, ensure_ascii=False)
        record.meeting_ref = meeting_ref or record.meeting_ref
        record.source_time = source_time or record.source_time
        versions = json.loads(record.versions_json or "[]")
        versions.append({"revision": version, "content_hash": content_hash,
                         "imported_at": now.isoformat(), "action": "updated"})
        record.versions_json = json.dumps(versions, ensure_ascii=False)
        action = "updated"
    record.updated_at = now

    # 候选抽取：只处理授权、非受限、非注入可疑段落
    proposals: list[MinuteProposal] = []
    review_requests: list[dict] = []
    for paragraph in kept:
        if paragraph.get("untrusted") or paragraph.get("restricted"):
            continue
        anchor = {"paragraph": paragraph["index"], "speaker": paragraph["speaker"],
                  "time": paragraph["time"], "quote": paragraph["text"][:120],
                  "anchor_ref": f"{ref}#p{paragraph['index']}"}
        for candidate in _extract_candidates(paragraph):
            proposal_key = stable_id(minutes_id, candidate["kind"], paragraph["index"],
                                     normalize_text(candidate["value"])[:120])
            duplicated = session.scalar(select(MinuteProposal).where(
                MinuteProposal.minutes_id == minutes_id,
                MinuteProposal.proposal_key == proposal_key))
            if duplicated is not None:
                continue
            work_item_id = None
            reviewer = ""
            reason = ""
            subject_value = candidate.get("subject") or ""
            if candidate["kind"] == "todo":
                work_item_id = _match_work_item(session, space_id=space_id,
                                                text=paragraph["text"])
                if subject_value:
                    reviewer = subject_value
                else:
                    reason = "todo_missing_assignee"
                    if work_item_id is not None:
                        mapped = session.get(Task, work_item_id)
                        reviewer = mapped.owner if mapped is not None else ""
                    reviewer = reviewer or escalate_to
            elif candidate["kind"] == "decision":
                # 决定主体：显式"由X决定"优先，其次段落发言人；都没有 → 待核对
                subject_value = subject_value or paragraph["speaker"]
                if subject_value:
                    reviewer = subject_value
                else:
                    reason = "decision_missing_subject"
                    reviewer = escalate_to
            else:  # goal
                reviewer = paragraph["speaker"] or escalate_to
            proposal = MinuteProposal(
                proposal_id=f"mp{proposal_key}"[:64], proposal_key=proposal_key,
                minutes_id=minutes_id, minutes_ref=ref, org_id=org_id, space_id=space_id,
                kind=candidate["kind"], subject=subject_value,
                value=candidate["value"], due_text=candidate.get("due") or "",
                anchor_json=json.dumps(anchor, ensure_ascii=False),
                work_item_id=work_item_id,
                status="pending" if not reason else "needs_owner",
                reviewer=reviewer, owner_scope=knowledge_scope)
            session.add(proposal)
            proposals.append(proposal)
            if reason:
                review_requests.append({"proposal_id": proposal.proposal_id, "reason": reason,
                                        "reviewer": reviewer, "value": proposal.value[:120],
                                        "anchor_ref": anchor["anchor_ref"]})
    session.flush()
    conflicts = _recompute_conflicts(session, minutes_id=minutes_id)
    for conflict in conflicts:
        review_requests.append({"proposal_ids": conflict["proposal_ids"],
                                "reason": "conflicting_decision_versions",
                                "reviewer": conflict["reviewer"],
                                "value": " / ".join(conflict["values"])[:120],
                                "anchor_ref": ref})
    session.flush()
    return {"ok": True, "minutes_id": minutes_id, "revision": version, "action": action,
            "cropped_paragraphs": cropped,
            "proposals": [_proposal_view(item) for item in proposals],
            "conflicts": conflicts, "review_requests": review_requests,
            "injection_flags": flags, "participation_mode": record.mode,
            "realtime_participation": False, "source_ref": ref}


def _proposal_view(proposal: MinuteProposal) -> dict:
    anchor = json.loads(proposal.anchor_json or "{}")
    return {"proposal_id": proposal.proposal_id, "kind": proposal.kind,
            "subject": proposal.subject, "value": proposal.value,
            "due_text": proposal.due_text, "status": proposal.status,
            "reviewer": proposal.reviewer, "work_item_id": proposal.work_item_id,
            "speaker": anchor.get("speaker", ""), "time": anchor.get("time", ""),
            "quote": anchor.get("quote", ""), "anchor_ref": anchor.get("anchor_ref", ""),
            "minutes_ref": proposal.minutes_ref}


def list_proposals(session: Session, *, space_id: str, minutes_id: str = "",
                   status: str = "", viewer_scope: str = "", include_private: bool = False) -> list[dict]:
    """候选列表：owner_scope 裁剪（私有不默认公开），可按纪要/状态过滤。"""
    query = select(MinuteProposal).where(MinuteProposal.space_id == space_id)
    if minutes_id:
        query = query.where(MinuteProposal.minutes_id == minutes_id)
    if status:
        query = query.where(MinuteProposal.status == status)
    rows = []
    for proposal in session.scalars(query):
        scope = proposal.owner_scope
        if scope not in ("", "public") and not (include_private and scope == viewer_scope):
            continue
        rows.append(_proposal_view(proposal))
    return rows


def confirm_proposal(session: Session, proposal_id: str, *, actor: str,
                     subject_owner: str = "", decision: str = "approve",
                     reason: str = "", tenant: str = "default") -> dict:
    """人工核对候选：批准后按类别落到既有知识/工作项接口（不建平行事实源）。

    - decision/goal → knowledge.record_claim（来源=纪要 ref+段落 anchor，冲突由知识层保留）
    - todo（已映射）→ 补充队列 note（event_id=proposal_id 幂等）+ 工作项提案事件（可回溯 anchor）
    - todo（未映射）→ 新建待确认工作项（task_key 由 proposal 派生，天然幂等）
    关键决定缺主体必须由负责人带主体核对，不自动改正式业务规则。
    """
    if not str(actor or "").strip():
        raise MinutesError("confirm_actor_required")
    proposal = session.get(MinuteProposal, str(proposal_id))
    if proposal is None:
        raise MinutesError("proposal_not_found")
    record = session.get(ImportedMinutes, proposal.minutes_id)
    if record is None or record.state == "revoked":
        raise MinutesError("minutes_revoked")
    if proposal.status not in {"pending", "needs_owner", "conflict_review"}:
        raise MinutesError("proposal_not_confirmable")
    anchor = json.loads(proposal.anchor_json or "{}")
    anchor_ref = anchor.get("anchor_ref", "")
    now = utcnow()

    if decision == "reject":
        proposal.status = "rejected"
        proposal.resolution_note = reason[:255]
        proposal.updated_at = now
        session.flush()
        return {"ok": True, "proposal_id": proposal.proposal_id, "effect": "rejected"}

    if proposal.kind in {"decision", "goal"}:
        subject = proposal.subject or str(subject_owner or "").strip() or anchor.get("speaker", "")
        if not subject:
            raise MinutesError("proposal_owner_required")
        predicate = "决定" if proposal.kind == "decision" else "目标"
        claim = knowledge_service.record_claim(
            session, org_id=proposal.org_id, space_id=proposal.space_id,
            owner_scope=proposal.owner_scope or record.knowledge_scope,
            subject=subject, predicate=predicate, value=proposal.value,
            source_refs=[proposal.minutes_ref, anchor_ref],
            confidence_basis=f"minutes-import:{proposal.minutes_id}")
        proposal.status = "confirmed"
    elif proposal.kind == "todo":
        if proposal.work_item_id is not None:
            task = session.get(Task, proposal.work_item_id)
            if task is None:
                raise MinutesError("work_item_not_found")
            supplements.enqueue(session, task.id, event_id=proposal.proposal_id,
                                event_kind="note",
                                payload={"source": "meeting_minutes", "value": proposal.value,
                                         "due": proposal.due_text, "anchor_ref": anchor_ref,
                                         "minutes_ref": proposal.minutes_ref})
            work_service.propose(session, task.id, actor=actor, kind="relate",
                                 payload={"proposal_id": proposal.proposal_id,
                                          "anchor_ref": anchor_ref,
                                          "source_ref": proposal.minutes_ref})
            proposal.status = "confirmed"
            effect = {"effect": "work_item_note", "task_id": task.id, "anchor_ref": anchor_ref}
        else:
            task_key = stable_id("minutes-todo", proposal.proposal_id)[:120]
            task = session.scalar(select(Task).where(Task.tenant == tenant, Task.task_key == task_key))
            if task is None:
                task = Task(task_key=task_key, tenant=tenant,
                            owner=str(subject_owner or "").strip() or actor,
                            chat_id=f"minutes:{proposal.minutes_id}",
                            title=proposal.value[:255], action=proposal.value,
                            assignee_name=proposal.subject or "",
                            due_text=proposal.due_text,
                            status=TaskStatus.NEEDS_CONFIRMATION, needs_confirmation=True,
                            source_message_ids=json.dumps(
                                [item for item in [proposal.minutes_ref, anchor_ref] if item],
                                ensure_ascii=False),
                            org_id=proposal.org_id, space_id=proposal.space_id,
                            goal=proposal.value)
                session.add(task)
                session.flush()
                session.add(TaskEvent(
                    task_id=task.id, event_type="created", actor=actor,
                    detail="纪要待办候选经人工核对建立",
                    evidence_message_ids=json.dumps(
                        [item for item in [proposal.minutes_ref, anchor_ref] if item],
                        ensure_ascii=False)))
                session.flush()
            proposal.status = "confirmed"
            effect = {"effect": "work_item_created", "task_id": task.id, "anchor_ref": anchor_ref}
    else:
        raise MinutesError("invalid_proposal_kind")

    proposal.confirmed_by = actor
    proposal.confirmed_at = now
    proposal.updated_at = now
    session.flush()
    result = {"ok": True, "proposal_id": proposal.proposal_id, "kind": proposal.kind,
              "status": proposal.status, "anchor_ref": anchor_ref,
              "minutes_ref": proposal.minutes_ref}
    if proposal.kind == "todo":
        result.update(effect)
    else:
        result["effect"] = "knowledge_claim"
        result["claim_id"] = claim.claim_id
    return result


def revoke_minutes(session: Session, *, minutes_id: str, reason: str = "") -> dict:
    """回退：撤销纪要全部版本的索引；派生主张标 stale，候选保留供审计。"""
    record = session.get(ImportedMinutes, minutes_id)
    if record is None:
        raise MinutesError("minutes_not_found")
    record.state = "revoked"
    invalidated: list[str] = []
    for entry in json.loads(record.versions_json or "[]"):
        invalidated.extend(knowledge_service.invalidate_for_source(
            session, source_ref=minutes_source_ref(minutes_id, entry.get("revision", 1)),
            reason=reason or "minutes_revoked"))
    session.flush()
    return {"ok": True, "minutes_id": minutes_id, "state": "revoked",
            "invalidated_claims": sorted(set(invalidated))}
