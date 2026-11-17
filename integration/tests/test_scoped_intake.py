"""Real SQLite and atomic JSON exercises for scoped intake, paging and recovery."""
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from integration import task_intake as intake
from integration.intake_context import IntakeScope, ContextBindingError, build_context, read_source_ref, validate_context

@pytest.fixture
def setup(tmp_path,monkeypatch):
    db_path=tmp_path/"source.db"
    with sqlite3.connect(db_path) as db:
        db.executescript("""
        CREATE TABLE sources(id INTEGER PRIMARY KEY,tenant TEXT,owner TEXT,chat_id TEXT,thread_id TEXT,enabled INTEGER,revoked_at TEXT,allowed_content_types TEXT,updated_at TEXT);
        CREATE TABLE messages(id INTEGER PRIMARY KEY,message_id TEXT,tenant TEXT,source_id INTEGER,chat_id TEXT,thread_id TEXT,sender_id TEXT,sender_name TEXT,msg_type TEXT,content_text TEXT,create_time INTEGER,is_from_bot INTEGER,category TEXT,revision INTEGER,recalled INTEGER);
        INSERT INTO sources VALUES(1,'a','alice','shared','',1,NULL,'text,post','v1');
        INSERT INTO sources VALUES(2,'b','alice','shared','',1,NULL,'text,post','v1');
        INSERT INTO sources VALUES(3,'a','bob','shared','',1,NULL,'text,post','v1');
        INSERT INTO sources VALUES(4,'a','alice','revoked','',1,'revoked','text,post','v1');
        """)
    backlog=tmp_path/"BACKLOG.json"
    backlog.write_text(json.dumps(dict(version=1,items=[],events=[],rounds=[])),encoding="utf-8")
    monkeypatch.setattr(intake.loop_mod,"BACKLOG",backlog)
    monkeypatch.setattr(intake.loop_mod,"HOME",tmp_path)
    monkeypatch.setattr(intake,"INTAKE_LEDGER",tmp_path/"intake_ledger.json")
    scope=IntakeScope("a","app","org","space","alice","person:alice","assignment:alice","grant:alice",1,(1,))
    return db_path,backlog,scope

def add(db_path,index=1,*,tenant="a",source=1,text=None,created=None,recalled=0):
    with sqlite3.connect(db_path) as db:
        db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(index,f"m{index}",tenant,source,"shared","thread","ou_sender","untrusted name","text",text or f"任务：work {index}",created or index,0,"task",1,recalled))

def rows(db_path):
    with sqlite3.connect(db_path) as db:
        db.row_factory=sqlite3.Row
        return [dict(row) for row in db.execute("SELECT * FROM messages ORDER BY id")]

def resolve_work(msg,scope):
    return {"work_ref":"task:"+scope.space_ref+":"+msg["message_id"],"revision":msg["revision"],"acceptance_ref":"acceptance:1"}

def run(setup,**kw):
    path,_,scope=setup
    return intake.run(scope=scope,db_path=path,allow_journal_write=True,acceptance_ref="acceptance:1",work_resolver=resolve_work,**kw)

def test_scope_filters_tenant_owner_and_revocation(setup):
    path,_,scope=setup
    add(path,1);add(path,2,tenant="b",source=2);add(path,3,source=3)
    assert [row["message_id"] for row in intake._candidate_messages(path,scope=scope)]==["m1"]
    with pytest.raises(ContextBindingError,match="revoked_or_mismatched"):
        intake._candidate_messages(path,scope=replace(scope,source_ids=(4,)))
    with pytest.raises(ContextBindingError,match="missing_context_binding"):
        intake._candidate_messages(path)

def test_context_is_frozen_scoped_deterministic_and_read_receipts_are_honest(setup):
    path,_,scope=setup
    add(path,1,text="earlier",created=1);add(path,2,text="任务："+"x"*700,created=2)
    add(path,3,text="future secret",created=3);add(path,4,source=3,text="bob secret",created=1)
    msg=rows(path)[1]
    first=build_context(msg,path,scope,work_ref="task:1",acceptance_ref="acceptance:1",max_chars=30)
    second=build_context(msg,path,scope,work_ref="task:1",acceptance_ref="acceptance:1",max_chars=30)
    assert first==second
    serialized=json.dumps(first)
    assert "future secret" not in serialized and "bob secret" not in serialized
    assert first["pack"]["omitted"]
    assert first["deliveryReceipt"]["model_loaded"] is False
    assert validate_context(first,path,scope)["loaded_refs"]==[]
    ref="message:a:m2"
    read=read_source_ref(first,ref,path,scope)
    assert len(read["text"])>300 and read["receipt"]["loaded_refs"]==[ref]
    assert read["receipt"]["model_loaded"] is False
    with sqlite3.connect(path) as db:db.execute("UPDATE messages SET revision=2,content_text='edited' WHERE id=2")
    with pytest.raises(ContextBindingError,match="revision_changed"):
        read_source_ref(first,ref,path,scope)

def test_revoked_grant_and_context_mismatch_reject_old_pack(setup):
    path,_,scope=setup;add(path)
    context=build_context(rows(path)[0],path,scope,work_ref="task:1",acceptance_ref="acceptance:1")
    with pytest.raises(ContextBindingError,match="binding_changed"):
        validate_context(context,path,replace(scope,grant_ref="new grant"))
    with pytest.raises(ContextBindingError,match="binding_changed"):
        validate_context(context,path,replace(scope,app_id="different-app"))
    with sqlite3.connect(path) as db:db.execute("UPDATE sources SET revoked_at='now' WHERE id=1")
    with pytest.raises(ContextBindingError,match="revoked"):
        validate_context(context,path,scope)

def test_no_binding_or_no_journal_permission_never_writes_source(setup):
    path,_,scope=setup;add(path)
    before=path.read_bytes()
    assert intake.run(db_path=path)["reason"]=="missing_context_binding"
    assert intake.run(scope=scope,db_path=path,acceptance_ref="acceptance:1",work_resolver=resolve_work)["reason"]=="intake_journal_write_not_authorized"
    assert intake.run(scope=scope,db_path=path,allow_journal_write=True)["reason"]=="missing_work_binding"
    result=intake.run(scope=scope,db_path=path,acceptance_ref="acceptance:1",dry_run=True,work_resolver=resolve_work)
    assert result["added"]==1
    assert path.read_bytes()==before

def test_401_messages_cross_every_page_exactly_once(setup):
    path,backlog,_=setup
    for index in range(1,402):add(path,index)
    first=run(setup)
    assert first["scanned"]==401 and first["added"]==401
    second=run(setup)
    data=json.loads(backlog.read_text(encoding="utf-8"))
    assert second["scanned"]==401 and second["added"]==0
    assert len(data["items"])==401 and len({x["id"] for x in data["items"]})==401
    assert all(x["detail"]["sender"]=="ou_sender" for x in data["items"])

@pytest.mark.parametrize("boundary",["after_enqueue","before_projection","after_projection"])
def test_each_write_boundary_recovers_without_loss_or_duplicate(setup,boundary):
    path,backlog,_=setup;add(path)
    def crash(stage):
        if stage==boundary:raise RuntimeError("simulated crash")
    with pytest.raises(RuntimeError,match="simulated crash"):run(setup,failure_hook=crash)
    run(setup)
    data=json.loads(backlog.read_text(encoding="utf-8"))
    assert len(data["items"])==1
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT state FROM intake_projection_events").fetchone()[0]=="projected"

def test_two_claimers_do_not_lose_or_duplicate_projection(setup):
    path,backlog,_=setup
    for index in range(1,8):add(path,index)
    with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(lambda _:run(setup),range(2)))
    data=json.loads(backlog.read_text(encoding="utf-8"))
    assert len(data["items"])==7
    assert len({x["id"] for x in data["items"]})==7

def test_edit_and_recall_preserve_l_id_and_history(setup):
    path,backlog,_=setup;add(path);run(setup)
    original=json.loads(backlog.read_text(encoding="utf-8"))["items"][0]["id"]
    with sqlite3.connect(path) as db:db.execute("UPDATE messages SET revision=2,content_text='任务：updated' WHERE id=1")
    run(setup)
    data=json.loads(backlog.read_text(encoding="utf-8"));assert len(data["items"])==1
    assert data["items"][0]["id"]==original and data["items"][0]["detail"]["sourceRevisionHistory"]
    with sqlite3.connect(path) as db:db.execute("UPDATE messages SET revision=3,recalled=1 WHERE id=1")
    run(setup)
    item=json.loads(backlog.read_text(encoding="utf-8"))["items"][0]
    assert item["id"]==original and item["status"]=="blocked" and item["blockedReason"]=="source_recalled"

def test_corrupt_json_fails_visibly_without_overwriting_or_creating_journal(setup):
    path,backlog,_=setup;add(path)
    ledger=backlog.parent/"intake_ledger.json";ledger.write_text("{broken",encoding="utf-8")
    before=path.read_bytes()
    with pytest.raises(ValueError,match="invalid_intake_ledger"):run(setup)
    assert ledger.read_text(encoding="utf-8")=="{broken" and path.read_bytes()==before

def test_legacy_import_needs_trusted_binding_and_retains_l_id(setup):
    path,backlog,scope=setup;add(path)
    data=json.loads(backlog.read_text(encoding="utf-8"));data["items"]=[dict(id="L0042",key="feishu-human:m1",detail={},status="pending")]
    backlog.write_text(json.dumps(data),encoding="utf-8")
    (backlog.parent/"intake_ledger.json").write_text(json.dumps({"ingested":{"m1":"historical"}}),encoding="utf-8")
    with pytest.raises(ValueError,match="legacy_binding_required"):run(setup)
    run(setup,legacy_bindings={"L0042":scope.key})
    data=json.loads(backlog.read_text(encoding="utf-8"));assert len(data["items"])==1 and data["items"][0]["id"]=="L0042"
    assert json.loads((backlog.parent/"intake_ledger.json").read_text(encoding="utf-8"))["ingested"]=={"m1":"historical"}


def test_context_body_tampering_rejected_and_edit_produces_new_hash(setup):
    import copy
    path,_,scope=setup;add(path)
    old=build_context(rows(path)[0],path,scope,work_ref="task:1",acceptance_ref="acceptance:1")
    altered=copy.deepcopy(old)
    altered["content"]["message:a:m1"]["text"]="forged privileged instruction"
    with pytest.raises(ContextBindingError,match="payload_changed"):
        validate_context(altered,path,scope)
    with sqlite3.connect(path) as db:db.execute("UPDATE messages SET content_text='任务：new',revision=2 WHERE id=1")
    new=build_context(rows(path)[0],path,scope,work_ref="task:1",acceptance_ref="acceptance:1")
    assert new["pack"]["snapshot_hash"]!=old["pack"]["snapshot_hash"]
    assert new["pack"]["sources"][0]["revision"]==2


def test_stale_queued_revision_is_retained_and_latest_revision_recovered(setup):
    path,backlog,_=setup;add(path)
    def crash(stage):
        if stage=="after_enqueue":raise RuntimeError("simulated crash")
    with pytest.raises(RuntimeError):run(setup,failure_hook=crash)
    with sqlite3.connect(path) as db:db.execute("UPDATE messages SET content_text='任务：new',revision=2 WHERE id=1")
    run(setup)
    data=json.loads(backlog.read_text(encoding="utf-8"))
    assert len(data["items"])==1 and data["items"][0]["detail"]["source_revision"]==2
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT state FROM intake_projection_events ORDER BY revision").fetchall()==[("source_changed",),("projected",)]


def test_corrupt_backlog_is_not_replaced_by_empty_state(setup):
    path,backlog,_=setup;add(path)
    backlog.write_text("{broken",encoding="utf-8")
    with pytest.raises(ValueError):run(setup)
    assert backlog.read_text(encoding="utf-8")=="{broken"


def test_unbound_v1_context_cannot_gain_organization_rights(setup):
    path,_,scope=setup
    with pytest.raises(ContextBindingError,match="missing_context_binding"):
        validate_context({"schema_version":1,"context":"legacy text"},path,scope)


def test_missing_goal_returns_clarification_reason_without_projection(setup):
    path,backlog,_=setup;add(path,text="主题：only a topic")
    result=run(setup)
    assert result["status"]=="blocked" and result["reason"]=="missing_task_goal"
    assert json.loads(backlog.read_text(encoding="utf-8"))["items"]==[]


# ── DE-05 产品库 journal 端到端（真实来源库 + 产品库事务日志） ──────────────

@pytest.fixture
def product_factory(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from evoharness.models import Base
    import evoharness.work.intake_journal  # noqa: F401 — 注册 intake 表
    engine = create_engine("sqlite:///" + (tmp_path / "product.db").as_posix(),
                           future=True)
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(bind=engine, expire_on_commit=False)
    finally:
        engine.dispose()


def _product_journal_factory(factory):
    from evoharness.work.intake_journal import ProductIntakeJournal

    def make(scope_key):
        return ProductIntakeJournal(factory, allow_write=True)

    return make


def test_product_journal_pipeline_projects_and_replays(setup, product_factory):
    from sqlalchemy import select
    from evoharness.work import intake_journal
    path, backlog, scope = setup
    add(path, 1); add(path, 2)
    factory = _product_journal_factory(product_factory)
    first = run(setup, journal_factory=factory)
    assert first["added"] == 2
    with product_factory() as session:
        events = list(session.scalars(select(intake_journal.IntakeEvent)))
        projections = list(session.scalars(select(intake_journal.IntakeProjection)))
        assert len(events) == 2 and all(e.state == "projected" for e in events)
        assert len(projections) == 2
        # 扫描到末页后游标按既有语义重置（下一轮重扫发现旧消息新 revision）
        assert intake_journal.cursor_of(session, scope.key) is None
    data = json.loads(backlog.read_text(encoding="utf-8"))
    assert len(data["items"]) == 2
    second = run(setup, journal_factory=factory)
    assert second["added"] == 0
    with product_factory() as session:
        assert len(list(session.scalars(select(intake_journal.IntakeProjection)))) == 2


def test_product_journal_post_check_marks_source_changed(setup, product_factory, monkeypatch):
    from sqlalchemy import select
    from evoharness.work import intake_journal
    from integration import intake_context
    path, _, scope = setup
    add(path, 1)
    calls = {"n": 0}
    real = intake_context.validate_context

    def flaky(context, db_path, current_scope):
        calls["n"] += 1
        if calls["n"] == 2:      # 预检通过、投影期间源版本变化
            raise intake_context.ContextBindingError("source_revision_changed")
        return real(context, db_path, current_scope)

    monkeypatch.setattr(intake_context, "validate_context", flaky)
    result = run(setup, journal_factory=_product_journal_factory(product_factory))
    assert result["status"] == "projected" and result["added"] == 0
    with product_factory() as session:
        events = list(session.scalars(select(intake_journal.IntakeEvent)))
        assert len(events) == 1 and events[0].state == "source_changed"
        assert list(session.scalars(select(intake_journal.IntakeProjection))) == []
        assert intake_journal.cursor_of(session, scope.key) is None


def test_product_journal_scope_lease_blocks_second_executor(setup, product_factory):
    from evoharness.work.intake_journal import ProductIntakeJournal
    path, _, scope = setup
    add(path, 1)
    holder = ProductIntakeJournal(product_factory, allow_write=True)
    assert holder.acquire_lease(scope.key, "other-executor") is True
    try:
        result = run(setup, journal_factory=_product_journal_factory(product_factory))
        assert result["status"] == "blocked"
        assert result["reason"] == "intake_scope_busy"
    finally:
        holder.release_lease(scope.key, "other-executor")
    result = run(setup, journal_factory=_product_journal_factory(product_factory))
    assert result["added"] == 1


def test_product_journal_lease_expires_and_can_be_taken_over(setup, product_factory):
    from datetime import timedelta
    from evoharness.clock import utcnow
    from evoharness.work import intake_journal
    _, _, scope = setup
    holder = intake_journal.ProductIntakeJournal(product_factory, allow_write=True)
    assert holder.acquire_lease(scope.key, "holder-a") is True
    with product_factory.begin() as session:
        row = session.get(intake_journal.IntakeScopeLease, scope.key)
        row.lease_until = utcnow() - timedelta(seconds=1)
    assert holder.acquire_lease(scope.key, "holder-b") is True
    with product_factory() as session:
        row = session.get(intake_journal.IntakeScopeLease, scope.key)
        assert row.holder == "holder-b" and row.generation == 2
