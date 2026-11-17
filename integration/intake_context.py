"""Read-only intake source authorization and frozen context bridge."""
from __future__ import annotations
from dataclasses import dataclass
from contextlib import contextmanager
import hashlib
import json
import sqlite3
from pathlib import Path

class ContextBindingError(ValueError):
    pass

def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

@dataclass(frozen=True)
class IntakeScope:
    """Trusted service binding, never reconstructed from a message or HTTP body."""
    tenant: str
    app_id: str
    org_ref: str
    space_ref: str
    owner: str
    principal_ref: str
    assignment_ref: str
    grant_ref: str
    policy_revision: int
    source_ids: tuple[int, ...]

    def validate(self):
        if not all(isinstance(v, str) and v.strip() for v in (self.tenant, self.app_id, self.org_ref, self.space_ref, self.owner, self.principal_ref, self.assignment_ref, self.grant_ref)) or type(self.policy_revision) is not int or self.policy_revision < 1 or not self.source_ids:
            raise ContextBindingError("missing_context_binding")
        if any(type(i) is not int or i < 1 for i in self.source_ids):
            raise ContextBindingError("missing_context_binding")

    @property
    def key(self):
        return digest([self.tenant, self.app_id, self.org_ref, self.space_ref, self.owner, self.principal_ref, self.assignment_ref])

    def context(self, event_key):
        self.validate()
        return dict(org_ref=self.org_ref, space_ref=self.space_ref, principal_ref=self.principal_ref, assignment_ref=self.assignment_ref, grant_ref=self.grant_ref, policy_revision=self.policy_revision, correlation_id=event_key, idempotency_key=event_key)

@contextmanager
def read_db(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        yield db
    finally:
        db.close()

def authorized_sources(db, scope):
    scope.validate()
    marks = ",".join("?" for _ in scope.source_ids)
    rows = [dict(row) for row in db.execute(f"SELECT * FROM sources WHERE id IN ({marks}) AND tenant=? AND owner=? AND enabled=1 AND revoked_at IS NULL ORDER BY id", (*scope.source_ids, scope.tenant, scope.owner))]
    if len(rows) != len(set(scope.source_ids)):
        raise ContextBindingError("source_grant_revoked_or_mismatched")
    return rows

def source_binding_hash(rows):
    return digest([{k: row.get(k) for k in ("id", "tenant", "owner", "chat_id", "thread_id", "enabled", "revoked_at", "allowed_content_types", "updated_at")} for row in rows])

def message_ref(msg):
    return "message:" + msg["tenant"] + ":" + msg["message_id"]

def message_hash(msg):
    return digest({key: msg.get(key) for key in ("tenant", "source_id", "message_id", "revision", "chat_id", "thread_id", "sender_id", "content_text", "create_time", "recalled")})

def event_key(scope, msg):
    return "feishu-human:" + digest([scope.key, msg["message_id"], int(msg.get("revision") or 1), bool(msg.get("recalled"))])

def message_query(db, scope, *, cutoff, chat=None, thread=None, exclude=None, limit=20, after=None, task_only=False):
    marks = ",".join("?" for _ in scope.source_ids)
    sql = (f"SELECT m.* FROM messages m JOIN sources s ON s.id=m.source_id WHERE m.source_id IN ({marks}) AND m.tenant=? AND s.tenant=m.tenant AND s.owner=? AND s.enabled=1 AND s.revoked_at IS NULL AND m.chat_id=s.chat_id AND (s.thread_id='' OR m.thread_id=s.thread_id) AND m.create_time<=? AND instr(','||replace(s.allowed_content_types,' ','')||',', ','||m.msg_type||',')>0")
    args = [*scope.source_ids, scope.tenant, scope.owner, cutoff]
    if not task_only:
        sql += " AND m.recalled=0"
    if chat is not None:
        sql += " AND m.chat_id=?"; args.append(chat)
    if thread is not None:
        sql += " AND m.thread_id=?"; args.append(thread)
    if exclude is not None:
        sql += " AND m.message_id<>?"; args.append(exclude)
    if task_only:
        sql += " AND m.is_from_bot=0 AND (m.category='task' OR m.content_text LIKE '任务：%' OR m.content_text LIKE '任务:%' OR m.content_text LIKE 'task:%')"
    if after:
        sql += " AND (m.create_time>? OR (m.create_time=? AND m.id>?))"; args.extend([after[0],after[0],after[1]])
    sql += (" ORDER BY m.create_time ASC,m.id ASC" if task_only else " ORDER BY m.create_time DESC,m.id DESC") + " LIMIT ?"
    args.append(limit)
    return [dict(row) for row in db.execute(sql,args)]

def build_context(msg, db_path, scope, *, work_ref, acceptance_ref, work_revision=1, cutoff=None, max_chars=6000):
    scope.validate()
    if not work_ref or not acceptance_ref or type(work_revision) is not int or work_revision < 1:
        raise ContextBindingError("missing_context_binding")
    cutoff = int(msg["create_time"] if cutoff is None else cutoff)
    with read_db(db_path) as db:
        db.execute("BEGIN")
        grants = authorized_sources(db,scope)
        primary = db.execute("SELECT * FROM messages WHERE tenant=? AND message_id=?", (scope.tenant,msg["message_id"])).fetchone()
        if primary is None or primary["source_id"] not in scope.source_ids or primary["recalled"] or primary["create_time"]>cutoff:
            raise ContextBindingError("required_source_missing")
        primary = dict(primary)
        if message_hash(primary)!=message_hash(msg):
            raise ContextBindingError("source_revision_changed")
        source = next(row for row in grants if row["id"]==primary["source_id"])
        if source["chat_id"]!=primary["chat_id"] or (source["thread_id"] and source["thread_id"]!=primary["thread_id"]) or primary["msg_type"] not in {kind.strip() for kind in source["allowed_content_types"].split(",")}:
            raise ContextBindingError("source_scope_mismatch")
        thread=message_query(db,scope,cutoff=cutoff,chat=msg["chat_id"],thread=msg.get("thread_id") or "",limit=20)
        recent=message_query(db,scope,cutoff=cutoff,chat=msg["chat_id"],exclude=msg["message_id"],limit=10)
        rows={row["message_id"]:row for row in [primary,*thread,*recent]}
        frozen=sorted(rows.values(),key=lambda row:(row["create_time"],row["id"]))
    metadata=[]; content={}; omitted=[]; remaining=max(0,max_chars)
    # Required task content gets the budget first, then chronological context.
    for row in [primary,*[row for row in frozen if row["message_id"]!=primary["message_id"]]]:
        ref=message_ref(row)
        metadata.append(dict(ref=ref,revision=int(row.get("revision") or 1),sha256=message_hash(row),scope_ref=scope.grant_ref))
        text=row.get("content_text") or ""; included=text[:min(300,remaining)]; remaining-=len(included)
        if included:content[ref]=dict(from_id=row.get("sender_id") or "",text=included,trust="external_source_text")
        if len(included)<len(text):omitted.append(dict(ref=ref,reason="context_budget",read_ref=ref))
    pack=dict(schema_version=2,context=scope.context(event_key(scope,primary)),work_ref=work_ref,work_revision=work_revision,objective_refs=[],acceptance_ref=acceptance_ref,sources=metadata,omitted=omitted)
    pack["snapshot_hash"]=digest(pack)
    envelope = dict(scope_key=scope.key,snapshot_at_ms=cutoff,thread=[content[message_ref(row)] for row in reversed(thread) if message_ref(row) in content],chatRecent=[content[message_ref(row)] for row in recent if message_ref(row) in content],matchedExperience=None,omittedKnowledge="knowledge_scope_binding_unavailable",pack=pack,content=content,source_binding_hash=source_binding_hash(grants),deliveryReceipt=dict(pack_hash=pack["snapshot_hash"],delivered_refs=sorted(content),model_loaded=False))
    envelope["payload_hash"] = digest(envelope)
    return envelope

def validate_context(context,db_path,scope):
    if not isinstance(context,dict) or not all(key in context for key in ("scope_key","pack","payload_hash","source_binding_hash","content")) or context["pack"].get("schema_version")!=2:
        raise ContextBindingError("missing_context_binding")
    if digest({key:value for key,value in context.items() if key!="payload_hash"})!=context.get("payload_hash"):
        raise ContextBindingError("context_payload_changed")
    if context.get("scope_key")!=scope.key:
        raise ContextBindingError("context_binding_changed")
    pack=context["pack"]; unsigned={key:value for key,value in pack.items() if key!="snapshot_hash"}
    if digest(unsigned)!=pack["snapshot_hash"] or pack["context"]!=scope.context(pack["context"]["idempotency_key"]):
        raise ContextBindingError("context_binding_changed")
    with read_db(db_path) as db:
        db.execute("BEGIN"); grants=authorized_sources(db,scope)
        if source_binding_hash(grants)!=context["source_binding_hash"]:raise ContextBindingError("source_grant_changed")
        for source in pack["sources"]:
            tenant,mid=source["ref"].removeprefix("message:").split(":",1)
            row=db.execute("SELECT * FROM messages WHERE tenant=? AND message_id=?",(tenant,mid)).fetchone()
            if row is None or row["source_id"] not in scope.source_ids or row["recalled"] or message_hash(dict(row))!=source["sha256"]:raise ContextBindingError("source_revision_changed")
            snippet=context["content"].get(source["ref"])
            if snippet and (not (row["content_text"] or "").startswith(snippet["text"]) or snippet["from_id"]!=row["sender_id"] or snippet["trust"]!="external_source_text"):
                raise ContextBindingError("context_payload_changed")
    return dict(pack_hash=pack["snapshot_hash"],validated_refs=[s["ref"] for s in pack["sources"]],loaded_refs=[],model_loaded=False)

def read_source_ref(context,ref,db_path,scope):
    validate_context(context,db_path,scope)
    expected=next((s for s in context["pack"]["sources"] if s["ref"]==ref),None)
    if expected is None:raise ContextBindingError("source_not_in_pack")
    tenant,mid=ref.removeprefix("message:").split(":",1)
    with read_db(db_path) as db:
        db.execute("BEGIN")
        grants=authorized_sources(db,scope)
        if source_binding_hash(grants)!=context["source_binding_hash"]:raise ContextBindingError("source_grant_changed")
        row=db.execute("SELECT * FROM messages WHERE tenant=? AND message_id=?",(tenant,mid)).fetchone()
        if row is None or row["source_id"] not in scope.source_ids or row["recalled"] or message_hash(dict(row))!=expected["sha256"]:raise ContextBindingError("source_revision_changed")
    return dict(ref=ref,text=row["content_text"],trust="external_source_text",receipt=dict(pack_hash=context["pack"]["snapshot_hash"],loaded_refs=[ref],model_loaded=False))
