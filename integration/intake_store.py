"""Recoverable event journal and serialized technical BACKLOG projection.

No Task/Run status is stored here. Journal writes require explicit authorization;
read-only intake callers never create schema or files.
"""
from __future__ import annotations
from contextlib import contextmanager
import json
import sqlite3
import time
from pathlib import Path

class IntakeJournal:
    def __init__(self, source_db, *, allow_write=False):
        if not allow_write:
            raise PermissionError("intake_journal_write_not_authorized")
        self.db=sqlite3.connect(source_db,timeout=30)
        self.db.row_factory=sqlite3.Row
        self.db.execute("CREATE TABLE IF NOT EXISTS intake_projection_events (event_key TEXT PRIMARY KEY, scope_key TEXT NOT NULL, message_id TEXT NOT NULL, revision INTEGER NOT NULL, action TEXT NOT NULL, payload_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued', item_id TEXT NOT NULL DEFAULT '')")
        self.db.execute("CREATE TABLE IF NOT EXISTS intake_scan_progress (scope_key TEXT PRIMARY KEY, created_ms INTEGER NOT NULL, row_id INTEGER NOT NULL)")
        self.db.commit()

    def close(self):self.db.close()

    def pending(self,scope_key):
        return [dict(row) for row in self.db.execute("SELECT * FROM intake_projection_events WHERE scope_key=? AND state='queued' ORDER BY rowid",(scope_key,))]

    def enqueue(self,scope_key,msg,event_key,payload):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO intake_projection_events(event_key,scope_key,message_id,revision,action,payload_json) VALUES(?,?,?,?,?,?)",(event_key,scope_key,msg["message_id"],int(msg.get("revision") or 1),"recalled" if msg.get("recalled") else "upsert",json.dumps(payload,ensure_ascii=False,sort_keys=True)))
            self.db.execute("INSERT INTO intake_scan_progress VALUES(?,?,?) ON CONFLICT(scope_key) DO UPDATE SET created_ms=excluded.created_ms,row_id=excluded.row_id",(scope_key,int(msg["create_time"]),int(msg["id"])))

    def cursor(self,scope_key):
        row=self.db.execute("SELECT created_ms,row_id FROM intake_scan_progress WHERE scope_key=?",(scope_key,)).fetchone()
        return tuple(row) if row else None

    def reset_cursor(self,scope_key):
        with self.db:self.db.execute("DELETE FROM intake_scan_progress WHERE scope_key=?",(scope_key,))

    def acknowledge(self,event_key,item_id):
        with self.db:self.db.execute("UPDATE intake_projection_events SET state='projected',item_id=? WHERE event_key=?",(item_id,event_key))

@contextmanager
def projection_lock(backlog_path):
    """SQLite's OS lock is released on process death; no racy stale-file deletion."""
    path=Path(backlog_path).with_suffix(".json.intake-lock.sqlite3")
    path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=30)
    try:
        db.execute("CREATE TABLE IF NOT EXISTS intake_writer (id INTEGER PRIMARY KEY)")
        db.commit();db.execute("BEGIN IMMEDIATE")
        yield
        db.commit()
    finally:db.close()

def project(event,loop_mod,*,legacy_bindings=None,forced_item_id=""):
    payload=json.loads(event["payload_json"]);logical=payload["logical_key"]
    with projection_lock(loop_mod.BACKLOG):
        state=loop_mod.load()
        if not isinstance(state.get("items"),list) or not isinstance(state.get("events"),list):
            raise ValueError("invalid_backlog")
        item=next((row for row in state["items"] if row.get("key")==logical or (row.get("detail") or {}).get("scopedLogicalKey")==logical),None)
        if item is None and forced_item_id:
            item=next((row for row in state["items"] if row.get("id")==forced_item_id),None)
        if item is None:
            legacy=next((row for row in state["items"] if row.get("key")=="feishu-human:"+event["message_id"]),None)
            if legacy is not None:
                if (legacy_bindings or {}).get(legacy["id"])!=event["scope_key"]:
                    raise ValueError("legacy_binding_required")
                item=legacy
        if item is not None and (item.get("detail") or {}).get("intake_event_key")==event["event_key"]:
            return item["id"],False
        if item is None:
            if event["action"]=="recalled":return "",False
            item=dict(id=str(forced_item_id or f"L{loop_mod._next_item_number(state):04d}"),key=logical,source="feishu-human",severity="medium",status="pending",firstSeenRound="feishu-"+time.strftime("%Y%m%d"),lastSeenRound="feishu-"+time.strftime("%Y%m%d"),seenCount=1,attempts=0,occurrences=[],informational=False)
            state["items"].append(item);created=True
        else:created=False
        previous=item.get("detail") or {}
        history=list(previous.get("sourceRevisionHistory") or [])
        if previous.get("intake_event_key"):
            history.append({k:v for k,v in previous.items() if k!="sourceRevisionHistory"})
        detail=dict(payload["detail"],scopedLogicalKey=logical,intake_event_key=event["event_key"],sourceRevisionHistory=history)
        item.update(title=payload["title"],detail=detail)
        if event["action"]=="recalled":item.update(status="blocked",blockedReason="source_recalled")
        elif not created:item.update(status="pending",requiresRevalidation=True)
        loop_mod.event(state,"intake",item_id=item["id"],source="feishu",event_key=event["event_key"],source_action=event["action"],actor=payload["detail"]["sender"],scope_ref=event["scope_key"])
        loop_mod.save(state)
        persisted=loop_mod.load()
        if not any((row.get("detail") or {}).get("intake_event_key")==event["event_key"] for row in persisted["items"]):
            raise RuntimeError("intake_projection_not_persisted")
        return item["id"],created
