"""获授权文档导入：版本、时间锚点、附件引用与受限解析（DE-22）。

- 导入必须持有效授权决定（governance.authorization，动作 import，逐版本+逐载荷批准）。
- 导入内容一律是资料不是指令：注入扫描只标记不执行；本模块没有任何工具执行入口。
- 解析走注入式端口（DocumentParser），默认只做纯文本/markdown 结构化，
  不执行文档内容；解析失败安全降级为原文保存，不崩溃。
- 重复导入幂等：同内容不重复入库，新内容必须推进版本号。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Protocol

from sqlalchemy import Boolean, DateTime, Integer, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from evoharness.clock import utcnow
from evoharness.governance import authorization
from evoharness.ids import sha256_json, sha256_text
from evoharness.knowledge import service as knowledge_service
from evoharness.models import Base


class DocumentError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ChannelDocument(Base):
    """导入的授权文档：版本化索引，正文只作资料保存，不承载指令语义。"""
    __tablename__ = "channel_documents"

    doc_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    space_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    content_type: Mapped[str] = mapped_column(String(64), default="text/markdown")
    content_text: Mapped[str] = mapped_column(Text, default="")
    # 结构化块：[{kind,index,text,anchor:{block,kind,section_title,quote,anchor_ref},untrusted}]
    sections_json: Mapped[str] = mapped_column(Text, default="[]")
    # 附件只留引用与指纹（name/size/content_type/sha256/ref/quarantined），不解析不执行
    attachments_json: Mapped[str] = mapped_column(Text, default="[]")
    authorized_scopes_json: Mapped[str] = mapped_column(Text, default="[]")
    injection_flags_json: Mapped[str] = mapped_column(Text, default="[]")
    versions_json: Mapped[str] = mapped_column(Text, default="[]")
    source_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    mode: Mapped[str] = mapped_column(String(24), default="documents_import")
    state: Mapped[str] = mapped_column(String(16), default="active", index=True)
    degraded: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


# ── 注入扫描：只标记，永不执行 ────────────────────────────────────────────────

INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("ignore_constraints", re.compile(
        r"忽略(?:(?:之前|以上|上述|前面|以前|所有|全部|任何|的)\s*){0,5}"
        r"(指令|约束|规则|限制|指示|设定)", re.I)),
    ("ignore_constraints_en", re.compile(
        r"ignore\s+(all\s+|any\s+)?(previous|prior|above|earlier|system)\s+"
        r"(instructions?|rules?|prompts?|constraints?)", re.I)),
    ("disregard", re.compile(r"disregard\s+(all\s+)?(previous|above|prior)", re.I)),
    ("exfiltrate", re.compile(
        r"(导出|下载|外发|泄露|窃取)(全部|所有)?(资料|文件|数据|密钥|凭据|源码|代码)", re.I)),
    ("execute_command", re.compile(r"(执行|运行|调用)(命令|脚本|工具|shell|终端)", re.I)),
    ("use_tools", re.compile(r"(使用|调用)工具", re.I)),
    ("reveal_prompt", re.compile(r"reveal\s+(your\s+)?(system\s+)?(prompt|instructions)", re.I)),
    ("conceal", re.compile(r"(不要|别|勿)(告诉|告知|上报|记录|显示)", re.I)),
]


def scan_injection(text: str) -> list[dict]:
    """扫描指令式文本并返回标记列表；调用方据此裁剪候选，绝不据此执行。"""
    flags: list[dict] = []
    source = text or ""
    for rule, pattern in INJECTION_PATTERNS:
        for match in pattern.finditer(source):
            start = max(0, match.start() - 10)
            flags.append({"rule": rule, "match": source[start:match.end() + 10].strip()})
    return flags


# ── 注入式解析端口：纯文本/markdown 优先，不执行文档内容 ─────────────────────

class DocumentParser(Protocol):
    """解析端口：只接收文本、只返回结构化数据；实现方不得有副作用。"""

    def parse(self, text: str) -> list[dict]:  # pragma: no cover - 端口定义
        ...


class MarkdownTextParser:
    """默认受限解析：markdown ATX 标题 + 空行分段，确定性、零副作用。"""

    _HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(?P<title>.+?)\s*$")

    def parse(self, text: str) -> list[dict]:
        blocks: list[dict] = []
        current_title = ""
        buffer: list[str] = []

        def flush(index: int) -> int:
            paragraph = " ".join(part.strip() for part in buffer if part.strip())
            buffer.clear()
            if paragraph:
                blocks.append({"kind": "paragraph", "index": index,
                               "text": paragraph, "section_title": current_title})
                return index + 1
            return index

        index = 0
        for line in (text or "").splitlines():
            heading = self._HEADING_RE.match(line)
            if heading:
                index = flush(index)
                current_title = heading.group("title").strip()
                blocks.append({"kind": "heading", "index": index,
                               "text": current_title, "section_title": ""})
                index += 1
            elif line.strip():
                buffer.append(line)
            else:
                index = flush(index)
        index = flush(index)
        return blocks


_TEXT_CONTENT_PREFIX = "text/"
_EXECUTABLE_SUFFIXES = (".exe", ".bat", ".cmd", ".ps1", ".sh", ".dll", ".msi",
                        ".scr", ".com", ".js", ".vbs", ".jar", ".py")


def source_ref(doc_id: str, revision: int) -> str:
    return f"channel-doc:{doc_id}@{int(revision)}"


def _attachment_view(item: dict) -> dict:
    name = str(item.get("name") or "")
    content = item.get("content")
    sha = str(item.get("sha256") or "")
    if content is not None and not sha:
        sha = sha256_text(str(content))
    content_type = str(item.get("content_type") or "")
    lower_name = name.lower()
    quarantined = (bool(content_type) and not content_type.lower().startswith(_TEXT_CONTENT_PREFIX)) \
        or lower_name.endswith(_EXECUTABLE_SUFFIXES)
    return {"name": name, "size": int(item.get("size") or 0),
            "content_type": content_type, "sha256": sha,
            "ref": str(item.get("ref") or ""), "quarantined": quarantined}


def _attachment_views(attachments: list[dict] | None) -> list[dict]:
    return [_attachment_view(item) for item in (attachments or [])]


def import_payload_hash(*, doc_id: str, title: str, version: int, content: str,
                        content_type: str, attachments: list[dict] | None) -> str:
    """授权载荷指纹：版本+正文+类型+附件引用；任一变化都需要新批准。"""
    return sha256_json({"doc_id": doc_id, "title": title, "version": int(version),
                        "content": content, "content_type": content_type,
                        "attachments": _attachment_views(attachments)})


def approve_document_import(session: Session, actor_id: str, *, org_id: str, doc_id: str,
                            title: str, version: int, content: str,
                            content_type: str = "text/markdown",
                            attachments: list[dict] | None = None,
                            grantee_id: str = "", expires_at: datetime | None = None):
    """为一次文档导入签发授权决定（动作 import，绑定版本与载荷）。"""
    return authorization.approve(
        session, actor_id, org_id=org_id, target_type="channel_document",
        target_id=doc_id, target_revision=int(version),
        payload_hash=import_payload_hash(doc_id=doc_id, title=title, version=version,
                                         content=content, content_type=content_type,
                                         attachments=attachments),
        actions={"import"}, grantee_id=grantee_id, expires_at=expires_at)


def _version_entry(version: int, content_hash: str, now: datetime, action: str) -> dict:
    return {"revision": int(version), "content_hash": content_hash,
            "imported_at": now.isoformat(), "action": action}


def import_document(session: Session, *, org_id: str, space_id: str, doc_id: str,
                    title: str, version: int, content: str,
                    content_type: str = "text/markdown",
                    attachments: list[dict] | None = None,
                    authorized_scopes: list[str] | None = None,
                    source_time: datetime | None = None, parser: DocumentParser | None = None,
                    ) -> dict:
    """获授权导入一份文档：幂等落库、保留版本/锚点/附件引用、受限解析。

    授权缺失或载荷不符 → DocumentError("import_not_authorized")；
    解析失败安全降级（degraded=True + reason_code），不抛出、不崩溃。
    """
    doc_id = str(doc_id or "").strip()
    if not doc_id:
        raise DocumentError("document_id_required")
    if not str(content or "").strip():
        raise DocumentError("document_content_required")
    version = int(version)
    if version < 1:
        raise DocumentError("document_version_invalid")
    payload_hash = import_payload_hash(doc_id=doc_id, title=title, version=version,
                                       content=content, content_type=content_type,
                                       attachments=attachments)
    try:
        authorization.validate(session, target_type="channel_document", target_id=doc_id,
                               target_revision=version, payload_hash=payload_hash,
                               action="import")
    except authorization.AuthorizationError:
        raise DocumentError("import_not_authorized") from None

    now = utcnow()
    content_text = str(content)
    content_hash = sha256_text(content_text)
    existing = session.get(ChannelDocument, doc_id)
    if existing is not None and existing.state == "revoked":
        raise DocumentError("document_revoked")
    if existing is not None and existing.content_hash == content_hash:
        # 重复导入幂等：同内容不重复入库、不重复派生
        return {"ok": True, "doc_id": doc_id, "revision": existing.revision,
                "action": "unchanged", "degraded": bool(existing.degraded),
                "reason_code": "already_imported",
                "injection_flags": json.loads(existing.injection_flags_json),
                "attachments": json.loads(existing.attachments_json),
                "source_ref": source_ref(doc_id, existing.revision),
                "mode": existing.mode}
    if existing is not None and version <= existing.revision:
        raise DocumentError("document_version_not_advancing")

    attachment_views = _attachment_views(attachments)
    flags: list[dict] = []
    degraded = False
    reason_code = ""
    sections: list[dict] = []
    ref = source_ref(doc_id, version)
    if not (content_type or "").lower().startswith(_TEXT_CONTENT_PREFIX):
        degraded, reason_code = True, "content_type_not_supported"
        flags = scan_injection(content_text)
    else:
        try:
            blocks = (parser or MarkdownTextParser()).parse(content_text)
        except Exception:
            degraded, reason_code = True, "parse_failed"
            blocks = []
            flags = scan_injection(content_text)
        else:
            for block in blocks:
                anchor = {"block": block["index"], "kind": block["kind"],
                          "section_title": block.get("section_title", ""),
                          "quote": block["text"][:80],
                          "anchor_ref": f"{ref}#b{block['index']}"}
                section = {"kind": block["kind"], "index": block["index"],
                           "text": block["text"], "anchor": anchor}
                block_flags = scan_injection(block["text"])
                if block_flags:
                    section["untrusted"] = True
                    for flag in block_flags:
                        flag["anchor_ref"] = anchor["anchor_ref"]
                    flags.extend(block_flags)
                sections.append(section)

    scopes = sorted({str(scope) for scope in (authorized_scopes or [])})
    if existing is None:
        record = ChannelDocument(
            doc_id=doc_id, org_id=org_id, space_id=space_id, title=title,
            revision=version, content_hash=content_hash, content_type=content_type,
            content_text=content_text, sections_json=json.dumps(sections, ensure_ascii=False),
            attachments_json=json.dumps(attachment_views, ensure_ascii=False),
            authorized_scopes_json=json.dumps(scopes, ensure_ascii=False),
            injection_flags_json=json.dumps(flags, ensure_ascii=False),
            versions_json=json.dumps([_version_entry(version, content_hash, now, "created")],
                                     ensure_ascii=False),
            source_time=source_time, degraded=degraded)
        session.add(record)
        action = "created"
    else:
        record = existing
        record.revision = version
        record.title = title or record.title
        record.content_hash = content_hash
        record.content_type = content_type
        record.content_text = content_text
        record.sections_json = json.dumps(sections, ensure_ascii=False)
        record.attachments_json = json.dumps(attachment_views, ensure_ascii=False)
        record.authorized_scopes_json = json.dumps(scopes, ensure_ascii=False)
        record.injection_flags_json = json.dumps(flags, ensure_ascii=False)
        record.degraded = degraded
        record.source_time = source_time or record.source_time
        versions = json.loads(record.versions_json or "[]")
        versions.append(_version_entry(version, content_hash, now, "updated"))
        record.versions_json = json.dumps(versions, ensure_ascii=False)
        action = "updated"
    record.updated_at = now
    session.flush()
    return {"ok": True, "doc_id": doc_id, "revision": version, "action": action,
            "degraded": degraded, "reason_code": reason_code,
            "injection_flags": flags, "attachments": attachment_views,
            "source_ref": ref, "mode": record.mode}


def get_document(session: Session, *, doc_id: str, viewer_scope: str = "") -> dict:
    """按授权范围裁剪的读取：viewer 不在授权名单内一律拒绝，不默认公开。"""
    record = session.get(ChannelDocument, doc_id)
    if record is None:
        raise DocumentError("document_not_found")
    if record.state == "revoked":
        raise DocumentError("document_revoked")
    scopes = json.loads(record.authorized_scopes_json or "[]")
    if scopes and viewer_scope not in scopes:
        raise DocumentError("viewer_scope_not_authorized")
    return {"doc_id": record.doc_id, "org_id": record.org_id, "space_id": record.space_id,
            "title": record.title, "revision": record.revision,
            "content_text": record.content_text,
            "sections": json.loads(record.sections_json or "[]"),
            "attachments": json.loads(record.attachments_json or "[]"),
            "versions": json.loads(record.versions_json or "[]"),
            "source_time": record.source_time, "degraded": bool(record.degraded),
            "source_ref": source_ref(record.doc_id, record.revision),
            "mode": record.mode}


def revoke_import(session: Session, *, doc_id: str, reason: str = "") -> dict:
    """回退：撤销该文档全部版本的索引，派生主张标 stale（正文与历史保留可审计）。"""
    record = session.get(ChannelDocument, doc_id)
    if record is None:
        raise DocumentError("document_not_found")
    record.state = "revoked"
    invalidated: list[str] = []
    for entry in json.loads(record.versions_json or "[]"):
        invalidated.extend(knowledge_service.invalidate_for_source(
            session, source_ref=source_ref(doc_id, entry.get("revision", 1)),
            reason=reason or "document_revoked"))
    session.flush()
    return {"ok": True, "doc_id": doc_id, "state": "revoked",
            "invalidated_claims": sorted(set(invalidated))}
