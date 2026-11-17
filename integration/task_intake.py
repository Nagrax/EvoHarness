"""Scoped Feishu intake and recoverable technical BACKLOG projection.

Organization intake requires an already resolved service-side identity/grant.
The bare legacy CLI reports missing_context_binding instead of manufacturing
organization rights. Journal writes require a separate explicit permission.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))

from integration.config import (  # noqa: E402
    INTAKE_LEDGER, feishu_database_path,
)
import loop as loop_mod  # noqa: E402  (eval/loop.py)

TASK_PREFIXES = ("任务：", "任务:", "task:", "Task:")
THEME_PREFIXES = ("主题：", "主题:")
DB_PATH = feishu_database_path()
CONTEXT_CHAT_LOOKBACK = 10   # 同会话近 N 条消息
CONTEXT_THREAD_LIMIT = 20    # 同线程最多 N 条


def _load_ledger() -> dict:
    if not INTAKE_LEDGER.is_file():
        return {"ingested": {}}
    try:
        value = json.loads(INTAKE_LEDGER.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("invalid_intake_ledger") from exc
    if not isinstance(value, dict) or not isinstance(value.get("ingested"), dict):
        raise ValueError("invalid_intake_ledger")
    return value


def _save_ledger(data: dict) -> None:
    INTAKE_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    tmp = INTAKE_LEDGER.with_name(f".{INTAKE_LEDGER.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, INTAKE_LEDGER)
    finally:
        tmp.unlink(missing_ok=True)


def _candidate_messages(db_path: Path = DB_PATH, *, scope=None, after=None, limit=200, cutoff=None) -> list[dict]:
    from integration.intake_context import ContextBindingError, authorized_sources, message_query, read_db
    if scope is None:
        raise ContextBindingError("missing_context_binding")
    if not Path(db_path).is_file():
        return []
    with read_db(db_path) as db:
        db.execute("BEGIN")
        authorized_sources(db, scope)
        return message_query(db, scope, cutoff=int(time.time()*1000) if cutoff is None else cutoff,
                             limit=limit, after=after, task_only=True)


def _gather_context(msg: dict, db_path: Path = DB_PATH, *, scope=None, work_ref=None, acceptance_ref=None, work_revision=1) -> dict:
    from integration.intake_context import ContextBindingError, build_context
    if scope is None:
        raise ContextBindingError("missing_context_binding")
    return build_context(msg, db_path, scope, work_ref=work_ref, acceptance_ref=acceptance_ref,work_revision=work_revision)


def _parse_theme(text: str) -> tuple[str, str | None]:
    """L9040：解析「主题：xxx 任务：yyy」组合前缀，返回 (任务文本, 主题)。"""
    theme = None
    for prefix in THEME_PREFIXES:
        idx = text.find(prefix)
        if idx != -1:
            rest = text[idx + len(prefix):]
            # 主题名取到「任务：」或行尾，再从原文移除该段
            theme_end = len(rest)
            for task_prefix in TASK_PREFIXES:
                p = rest.find(task_prefix)
                if p != -1:
                    theme_end = min(theme_end, p)
            theme = rest[:theme_end].strip() or None
            # 原文去掉「主题：xxx」段
            seg_end = text.find(prefix) + len(prefix) + theme_end
            text = (text[:text.find(prefix)] + text[seg_end:]).strip()
            break
    return text, theme


def run(*, dry_run: bool = False, scope=None, db_path: Path = DB_PATH,
        allow_journal_write: bool = False, acceptance_ref: str = "",
        legacy_bindings: dict | None = None, work_resolver=None, failure_hook=None,
        journal_factory=None) -> dict:
    """Bound intake only. Unbound legacy callers cannot acquire organization rights.

    Journal authorization is separate from read grants. SQLite source DB is
    never modified during dry-run or without allow_journal_write.
    """
    from integration.intake_context import ContextBindingError, authorized_sources, digest, event_key, read_db, validate_context
    from integration.intake_store import IntakeJournal, project
    if scope is None:
        return {"scanned": 0, "added": 0, "items": [], "status": "blocked", "reason": "missing_context_binding"}
    scope.validate()
    if work_resolver is None:
        return {"scanned":0,"added":0,"items":[],"status":"blocked","reason":"missing_work_binding"}
    # Detect corrupted legacy state before creating a journal or changing files.
    legacy = _load_ledger()
    if not dry_run and not allow_journal_write:
        return {"scanned": 0, "added": 0, "items": [], "status": "blocked", "reason": "intake_journal_write_not_authorized"}
    if not Path(db_path).is_file():
        return {"scanned":0,"added":0,"items":[],"status":"blocked","reason":"source_database_missing"}
    with read_db(db_path) as source:
        authorized_sources(source,scope)
        source.execute("SELECT 1 FROM messages LIMIT 0")
    results=[];scanned=0;added=0
    journal=None if dry_run else (
        journal_factory(scope.key) if journal_factory is not None
        else IntakeJournal(db_path,allow_write=True))
    lease_holder = ""
    if journal is not None and hasattr(journal, "acquire_lease"):
        lease_holder = f"intake:{os.getpid()}:{time.time_ns()}"
        if not journal.acquire_lease(scope.key, lease_holder, lease_seconds=300):
            return {"scanned": 0, "added": 0, "items": [],
                    "status": "blocked", "reason": "intake_scope_busy"}
    def project_pending():
        nonlocal added
        for event in journal.pending(scope.key):
            with journal.db:
                # Keep source revocation/edit writers out until the projection
                # has been durably written and its journal receipt committed.
                journal.db.execute("BEGIN IMMEDIATE")
                payload=json.loads(event["payload_json"])
                with read_db(db_path) as source:authorized_sources(source,scope)
                if event["action"]!="recalled":
                    try:validate_context(payload["detail"]["context"],db_path,scope)
                    except ContextBindingError as exc:
                        if str(exc)!="source_revision_changed":raise
                        journal.db.execute("UPDATE intake_projection_events SET state='source_changed' WHERE event_key=?",(event["event_key"],))
                        journal.db.execute("DELETE FROM intake_scan_progress WHERE scope_key=?",(scope.key,))
                        continue
                if failure_hook:failure_hook("before_projection")
                item_id,created=project(event,loop_mod,legacy_bindings=legacy_bindings)
                if failure_hook:failure_hook("after_projection")
                journal.db.execute("UPDATE intake_projection_events SET state='projected',item_id=? WHERE event_key=?",(item_id,event["event_key"]))
                added+=int(created)
                results.append({"id":item_id,"event_key":event["event_key"],"created":created})
    def project_pending_product():
        """产品库 journal（DE-05）：相 B 逐事件投影并落回执；异常保留 pending。"""
        nonlocal added
        for event in journal.pending(scope.key):
            with read_db(db_path) as source: authorized_sources(source,scope)
            payload=json.loads(event["payload_json"])
            if event["action"]!="recalled":
                try: validate_context(payload["detail"]["context"],db_path,scope)
                except ContextBindingError as exc:
                    if str(exc)!="source_revision_changed": raise
                    journal.mark_source_changed(event["event_key"])
                    journal.reset_cursor(scope.key)
                    continue
            if failure_hook: failure_hook("before_projection")
            item_id,created=project(event,loop_mod,legacy_bindings=legacy_bindings)
            if failure_hook: failure_hook("after_projection")
            # 投影后再次复核源版本：撤权/编辑恰好发生在投影期间时，不落产品库回执，
            # 事件回退到 source_changed 并重置游标，下一轮按新版本重投影（DE-05/14）。
            if event["action"]!="recalled":
                try: validate_context(payload["detail"]["context"],db_path,scope)
                except ContextBindingError as exc:
                    if str(exc)!="source_revision_changed": raise
                    journal.mark_source_changed(event["event_key"])
                    journal.reset_cursor(scope.key)
                    continue
            recorder=getattr(journal,"record_and_mark",None)
            if callable(recorder):
                recorder(event,item_id)
            else:
                journal.mark_projected(event["event_key"],item_id)
            added+=int(created)
            results.append({"id":item_id,"event_key":event["event_key"],"created":created})
    flush = project_pending_product if journal_factory is not None else project_pending
    try:
        if journal:flush()
        after=journal.cursor(scope.key) if journal else None
        while True:
            candidates=_candidate_messages(db_path,scope=scope,after=after,limit=200)
            if not candidates:
                if journal:journal.reset_cursor(scope.key)
                break
            for msg in candidates:
                scanned+=1;mid=msg["message_id"]
                text=(msg["content_text"] or "").strip()
                text,theme=_parse_theme(text)
                for prefix in TASK_PREFIXES:
                    if text.startswith(prefix):text=text[len(prefix):].strip();break
                if not text and not msg.get("recalled"):
                    raise ContextBindingError("missing_task_goal")
                key=event_key(scope,msg)
                logical="feishu-human:"+digest([scope.key,mid])
                binding=work_resolver(msg,scope)
                if not isinstance(binding,dict) or not binding.get("work_ref") or not binding.get("acceptance_ref"):
                    raise ContextBindingError("missing_work_binding")
                context=None if msg.get("recalled") else _gather_context(msg,db_path,scope=scope,work_ref=binding["work_ref"],acceptance_ref=binding["acceptance_ref"],work_revision=binding.get("revision",1))
                payload={"logical_key":logical,"title":"飞书任务 "+(text if len(text)<=60 else text[:57]+"…"),"detail":{"message_id":mid,"chat_id":msg["chat_id"],"sender":msg["sender_id"],"tenant":scope.tenant,"app_id":scope.app_id,"space_ref":scope.space_ref,"owner":scope.owner,"source_id":msg["source_id"],"source_revision":int(msg.get("revision") or 1),"create_time":msg["create_time"],"text":text[:2000],"kind":"feishu-human-task","theme":theme,"context":context}}
                if dry_run:results.append({"title":payload["title"],"theme":theme,"dryRun":True,"event_key":key});added+=1
                else:
                    journal.enqueue(scope.key,msg,key,payload)
                    if failure_hook:failure_hook("after_enqueue")
                    flush()
                after=(msg["create_time"],msg["id"])
        return {"scanned":scanned,"added":added,"items":results,"status":"dry_run" if dry_run else "projected","legacy_entries_preserved":len(legacy["ingested"])}
    except ContextBindingError as exc:
        return {"scanned":scanned,"added":added,"items":results,"status":"blocked","reason":str(exc)}
    finally:
        if journal:
            if lease_holder and hasattr(journal, "release_lease"):
                journal.release_lease(scope.key, lease_holder)
            journal.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    import argparse
    parser = argparse.ArgumentParser(description="飞书任务 → 迭代台账")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(dry_run=args.dry_run), ensure_ascii=False, indent=2))
