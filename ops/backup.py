"""业务 DB 与文件/密钥协调备份、恢复核验、升级回退演练（DE-23）。

协调口径
--------
- SQLite 用在线 backup API（`sqlite3.Connection.backup`）取一致快照；
  恢复前先停写实例（supervisor stop），文件级复制即可。
- token 密文与密钥**分离备份**：库里的 token 全部是 Fernet 密文，
  `data/.token_key`（本地密钥文件形态）随备份单独走；
  `FEISHU_TOKEN_SECRET`（环境变量形态）属部署凭据，**绝不入库也不入备份**，
  由部署负责人的密钥渠道保管——恢复演练必须同时验证"可解密"与"撤权仍有效"。
- 核验口径：恢复到临时目录 → 打开副本逐项核对
  组织图 / 任务 / 知识版本 / 回执 与备份时刻的计数快照一致。

演练（run_drill）是真实执行：种子数据 → 备份 → 破坏原件 → 恢复到临时目录 →
逐项核验 → 升级回退计时（perf_counter 实测，写入 OPERATIONS.md 的依据）。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from evoharness.clock import utcnow

REPO = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _feishu_app_dir() -> Path:
    return REPO / "feishu-daily-agent"


# ---------------------------------------------------------------------------
# 计数快照与核验
# ---------------------------------------------------------------------------

def count_snapshot(db_path: Path) -> dict:
    """备份时刻的业务计数快照：恢复核验逐项对照，不接受"打开就算恢复成功"。"""
    from sqlalchemy import create_engine, func, select
    from evoharness import migrations  # noqa: F401 — 注册组织/Run/治理/知识表
    from evoharness.knowledge.models import KnowledgeClaim
    from evoharness.models import Base, DailyReport, Outbox, Task, TwinIdentity
    from evoharness.organization.models import (
        Membership, Organization, OrganizationUnit, Position,
    )
    from evoharness.work.models import ExecutionRun

    engine = create_engine(f"sqlite:///{Path(db_path).as_posix()}", future=True)
    Base.metadata.create_all(bind=engine)
    try:
        with engine.connect() as conn:
            def count(model):
                return int(conn.execute(select(func.count("*")).select_from(model)).scalar_one())

            def status_counts(model, column):
                rows = conn.execute(select(column, func.count("*")).select_from(model)
                                    .group_by(column)).all()
                return {str(status): int(n) for status, n in rows}

            knowledge = {
                "total": count(KnowledgeClaim),
                "by_state": status_counts(KnowledgeClaim, KnowledgeClaim.state),
                "revision_sum": int(conn.execute(
                    select(func.coalesce(func.sum(KnowledgeClaim.source_revision), 0))
                ).scalar_one()),
            }
            return {
                "organizations": count(Organization),
                "organization_units": count(OrganizationUnit),
                "positions": count(Position),
                "memberships": count(Membership),
                "tasks": count(Task),
                "runs": count(ExecutionRun),
                "daily_reports": count(DailyReport),
                "twin_identities": count(TwinIdentity),
                "outbox": status_counts(Outbox, Outbox.status),
                "knowledge": knowledge,
            }
    finally:
        engine.dispose()


def _decrypt_with_secret(ciphertext: str, secret: str) -> str:
    """用指定密钥解密；复用 app.services.oauth 的口径（不另造加密）。"""
    old = os.environ.get("FEISHU_TOKEN_SECRET")
    os.environ["FEISHU_TOKEN_SECRET"] = secret
    try:
        feishu_root = _feishu_app_dir()
        if str(feishu_root) not in sys.path:
            sys.path.insert(0, str(feishu_root))
        from app.services.oauth import decrypt_token

        return decrypt_token(ciphertext)
    finally:
        if old is None:
            os.environ.pop("FEISHU_TOKEN_SECRET", None)
        else:
            os.environ["FEISHU_TOKEN_SECRET"] = old


def verify_restored(db_path: Path, *, token_secret: str, expected: dict | None = None) -> dict:
    """恢复核验：token 可解密、撤权仍有效、组织图/任务/知识版本/回执可核对。"""
    from sqlalchemy import create_engine, func, select
    from evoharness import migrations  # noqa: F401
    from evoharness.knowledge.models import KnowledgeClaim
    from evoharness.models import Base, DailyReport, Outbox, Task, TwinIdentity
    from evoharness.organization.models import (
        Membership, Organization, OrganizationUnit, Position,
    )
    from evoharness.work.models import ExecutionRun

    db_path = Path(db_path)
    if not db_path.is_file():
        return {"ok": False, "error": f"restored db missing: {db_path}"}

    problems: list[str] = []
    token_report = {"authorized": 0, "decrypted": 0, "revoked": 0, "revoked_still_revoked": 0}
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    try:
        with engine.connect() as conn:
            rows = conn.execute(select(TwinIdentity)).mappings().all()
            for row in rows:
                if row["authorized"]:
                    token_report["authorized"] += 1
                    cipher = row["access_token_enc"] or ""
                    if not cipher:
                        problems.append(f"twin {row['open_id']}: authorized 但密文为空")
                        continue
                    if _decrypt_with_secret(cipher, token_secret):
                        token_report["decrypted"] += 1
                    else:
                        problems.append(f"twin {row['open_id']}: token 解密失败（密钥不匹配？）")
                else:
                    token_report["revoked"] += 1
                    # 撤权仍有效：authorized=False 必须保持，且密文已被清空
                    if not (row["access_token_enc"] or "") and not (row["refresh_token_enc"] or ""):
                        token_report["revoked_still_revoked"] += 1
                    else:
                        problems.append(f"twin {row['open_id']}: 撤权后密文未清空")

            snapshot = {
                "organizations": int(conn.execute(
                    select(func.count("*")).select_from(Organization)).scalar_one()),
                "organization_units": int(conn.execute(
                    select(func.count("*")).select_from(OrganizationUnit)).scalar_one()),
                "positions": int(conn.execute(
                    select(func.count("*")).select_from(Position)).scalar_one()),
                "memberships": int(conn.execute(
                    select(func.count("*")).select_from(Membership)).scalar_one()),
                "tasks": int(conn.execute(
                    select(func.count("*")).select_from(Task)).scalar_one()),
                "runs": int(conn.execute(
                    select(func.count("*")).select_from(ExecutionRun)).scalar_one()),
                "twin_identities": int(conn.execute(
                    select(func.count("*")).select_from(TwinIdentity)).scalar_one()),
                "daily_reports": int(conn.execute(
                    select(func.count("*")).select_from(DailyReport)).scalar_one()),
            }
            receipts: dict[str, int] = {}
            for status, n in conn.execute(
                    select(Outbox.status, func.count("*"))
                    .select_from(Outbox).group_by(Outbox.status)).all():
                receipts[str(status)] = int(n)
            claims = conn.execute(select(KnowledgeClaim.state,
                                         KnowledgeClaim.source_revision)).all()
            knowledge = {
                "total": len(claims),
                "by_state": {},
                "revision_sum": sum(int(rev or 0) for _, rev in claims),
            }
            for state, _rev in claims:
                knowledge["by_state"][str(state)] = knowledge["by_state"].get(str(state), 0) + 1
    finally:
        engine.dispose()

    counts = dict(snapshot)
    counts["outbox"] = receipts
    counts["knowledge"] = knowledge
    mismatch: list[str] = []
    if expected:
        for key, want in expected.items():
            got = counts.get(key)
            if got != want:
                mismatch.append(f"{key}: want={want} got={got}")
    return {
        "ok": not problems and not mismatch,
        "tokens": token_report,
        "counts": counts,
        "problems": problems,
        "mismatches": mismatch,
    }


# ---------------------------------------------------------------------------
# 备份 / 恢复
# ---------------------------------------------------------------------------

def backup(db_path: Path, out_dir: Path, *, token_key_path: Path | None = None,
           extra_files: list[Path] | None = None, now: datetime | None = None) -> dict:
    """协调备份：DB 一致快照 + token 密钥文件 + manifest（sha256/计数快照）。

    - `FEISHU_TOKEN_SECRET` 环境变量形态的密钥不落备份（部署凭据渠道保管）。
    - 返回 manifest 字典；同时写 `manifest.json` 进备份目录。
    """
    db_path = Path(db_path)
    out_dir = Path(out_dir)
    if not db_path.is_file():
        raise FileNotFoundError(f"db missing: {db_path}")
    out_dir.mkdir(parents=True, exist_ok=True)
    moment = now or utcnow()

    db_copy = out_dir / db_path.name
    _sqlite_snapshot(db_path, db_copy)

    files: list[dict] = [_file_entry(db_copy)]
    if token_key_path and Path(token_key_path).is_file():
        key_copy = out_dir / Path(token_key_path).name
        shutil.copy2(token_key_path, key_copy)
        files.append(_file_entry(key_copy))
    for extra in extra_files or []:
        extra = Path(extra)
        if extra.is_file():
            copy = out_dir / extra.name
            shutil.copy2(extra, copy)
            files.append(_file_entry(copy))

    manifest = {
        "createdAt": moment.isoformat(),
        "sourceDb": str(db_path),
        "files": files,
        "counts": count_snapshot(db_path),
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def _file_entry(path: Path) -> dict:
    return {"path": path.name, "sha256": _sha256(path), "bytes": path.stat().st_size}


def _sqlite_snapshot(source: Path, target: Path) -> None:
    src = sqlite3.connect(str(source))
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def restore(backup_dir: Path, target_dir: Path) -> dict:
    """恢复到目标目录（通常是临时目录）；逐文件校验 sha256。"""
    backup_dir = Path(backup_dir)
    target_dir = Path(target_dir)
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    target_dir.mkdir(parents=True, exist_ok=True)
    restored: list[str] = []
    for entry in manifest["files"]:
        source = backup_dir / entry["path"]
        if not source.is_file():
            raise FileNotFoundError(f"backup file missing: {source}")
        if _sha256(source) != entry["sha256"]:
            raise ValueError(f"backup integrity failed: {entry['path']}")
        shutil.copy2(source, target_dir / entry["path"])
        restored.append(entry["path"])
    return {"ok": True, "restored": restored, "target": str(target_dir),
            "counts": manifest.get("counts")}


def rollback_upgrade(backup_dir: Path, deploy_dir: Path) -> dict:
    """升级回退：用最近一份一致备份覆盖部署目录中的 DB（先停写实例）。"""
    started = time.perf_counter()
    result = restore(backup_dir, deploy_dir)
    elapsed = time.perf_counter() - started
    result["rollbackSeconds"] = round(elapsed, 3)
    result["measuredAt"] = utcnow().isoformat()
    return result


# ---------------------------------------------------------------------------
# 真实演练
# ---------------------------------------------------------------------------

def _seed_deploy_db(db_path: Path, *, token_secret: str) -> dict:
    """种子一套可核对的业务库（离线）：组织图/任务/Run/知识/回执/分身凭据。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from evoharness import migrations  # noqa: F401
    from evoharness.knowledge.models import KnowledgeClaim
    from evoharness.models import Base, Outbox, OutboxStatus, Task, TaskStatus, TwinIdentity
    from evoharness.organization.models import (
        Membership, Organization, OrganizationUnit, Position,
    )
    from evoharness.work.models import ExecutionRun

    feishu_root = _feishu_app_dir()
    if str(feishu_root) not in sys.path:
        sys.path.insert(0, str(feishu_root))
    # 加密必须走演练密钥：显式设置环境变量，避免 oauth 退回生成真实 data/.token_key
    old_secret = os.environ.get("FEISHU_TOKEN_SECRET")
    os.environ["FEISHU_TOKEN_SECRET"] = token_secret
    try:
        from app.services.oauth import encrypt_token

        db_path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
        Base.metadata.create_all(bind=engine)
        session = sessionmaker(bind=engine, expire_on_commit=False)()
        try:
            org = Organization(org_id="org-seed-1", title="客服中心", owner_principal_id="ou_alice")
            session.add(org)
            session.flush()
            unit = OrganizationUnit(unit_id="unit-seed-1", org_id=org.org_id, title="一线支持")
            session.add(unit)
            session.flush()
            pos = Position(position_id="pos-seed-1", org_id=org.org_id, unit_id=unit.unit_id,
                           title="值班专员")
            session.add(pos)
            session.flush()
            session.add(Membership(membership_id="mem-seed-1", org_id=org.org_id,
                                   principal_id="ou_alice", status="active"))
            session.add(Task(task_key="seed-task-1", tenant="default", owner="ou_alice",
                             chat_id="oc_seed", title="演练工单", action="fix",
                             status=TaskStatus.OPEN, revision=1))
            session.flush()
            session.add(ExecutionRun(id="run-seed-1", task_id=1, tenant="default",
                                     task_revision=1, idempotency_key="seed-run",
                                     actor_id="ou_alice", context_json="{}",
                                     context_hash="0" * 64, output_root="out",
                                     state="queued"))
            session.add(KnowledgeClaim(claim_id="claim-seed-1", org_id=org.org_id, space_id="",
                                       owner_scope="public", subject="值班表", predicate="入口",
                                       value="L1 值班由 A 组承担", source_refs_json='["seed:1"]',
                                       source_revision=2))
            session.add(Outbox(intent_key="seed|sent", tenant="default", kind="daily",
                               target_type="chat", target_id="oc_seed", payload_json="{}",
                               status=OutboxStatus.SENT, receipt_message_id="om_seed_1"))
            session.add(Outbox(intent_key="seed|unknown", tenant="default", kind="notice",
                               target_type="chat", target_id="oc_seed", payload_json="{}",
                               status=OutboxStatus.DELIVERY_UNKNOWN, last_error="seed"))
            session.add(TwinIdentity(tenant="default", open_id="ou_alice", name="Alice",
                                     authorized=True, access_token_enc=encrypt_token(
                                         "live-token-for-alice-restore-drill"),
                                     expires_at=time.time() + 3600))
            session.add(TwinIdentity(tenant="default", open_id="ou_bob", name="Bob",
                                     authorized=False, access_token_enc="", refresh_token_enc="",
                                     last_error="revoked by owner"))
            session.commit()
        finally:
            session.close()
            engine.dispose()
    finally:
        if old_secret is None:
            os.environ.pop("FEISHU_TOKEN_SECRET", None)
        else:
            os.environ["FEISHU_TOKEN_SECRET"] = old_secret
    return {"db": str(db_path)}


def run_drill(work_dir: Path, *, token_secret: str, now: datetime | None = None) -> dict:
    """备份→破坏→恢复到临时目录→逐项核验→升级回退计时（全部真实执行）。"""
    work_dir = Path(work_dir)
    steps: list[dict] = []

    def step(name: str, ok: bool, detail: Any = "") -> None:
        steps.append({"step": name, "ok": bool(ok), "detail": str(detail)[:400]})

    deploy = work_dir / "deploy" / "data"
    db_path = deploy / "agent.db"
    seeded = _seed_deploy_db(db_path, token_secret=token_secret)
    step("seed", db_path.is_file(), seeded)

    backup_dir = work_dir / "backups" / "b1"
    manifest = backup(db_path, backup_dir, now=now)
    step("backup", len(manifest["files"]) >= 1,
         f"files={[f['path'] for f in manifest['files']]} counts={manifest['counts']['tasks']}")

    # 破坏原件：模拟误删/损坏
    db_path.write_bytes(b"corrupted-not-a-sqlite-db" * 100)
    step("corrupt_original", not _db_opens(db_path), "原库已损坏（演练注入）")

    target = work_dir / "restored" / "data"
    restore_result = restore(backup_dir, target)
    step("restore_to_temp", restore_result["ok"], restore_result["restored"])

    verify = verify_restored(target / "agent.db", token_secret=token_secret,
                             expected=manifest["counts"])
    step("verify", verify["ok"],
         f"tokens={verify.get('tokens')} problems={verify.get('problems')} "
         f"mismatches={verify.get('mismatches')}")

    # 升级回退：把备份回铺到部署目录（真实覆盖 + 实测耗时）
    rb = rollback_upgrade(backup_dir, deploy)
    step("upgrade_rollback", rb["ok"] and _db_opens(deploy / "agent.db"),
         f"rollbackSeconds={rb['rollbackSeconds']} measuredAt={rb['measuredAt']}")
    verify_back = verify_restored(deploy / "agent.db", token_secret=token_secret,
                                  expected=manifest["counts"])
    step("verify_after_rollback", verify_back["ok"], verify_back.get("tokens"))

    ok = all(s["ok"] for s in steps)
    return {"ok": ok, "steps": steps,
            "rollbackSeconds": rb["rollbackSeconds"],
            "verify": verify}


def _db_opens(path: Path) -> bool:
    try:
        conn = sqlite3.connect(str(path))
        try:
            conn.execute("select count(*) from sqlite_master")
        finally:
            conn.close()
        return True
    except sqlite3.Error:
        return False


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="备份/恢复/升级回退演练（离线）")
    parser.add_argument("action", choices=["backup", "restore", "drill"])
    parser.add_argument("--db", required=False)
    parser.add_argument("--out", required=False)
    parser.add_argument("--target", required=False)
    parser.add_argument("--work-dir", required=False)
    args = parser.parse_args(argv)
    if args.action == "backup":
        manifest = backup(Path(args.db), Path(args.out))
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0
    if args.action == "restore":
        result = restore(Path(args.out), Path(args.target))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    secret = os.environ.get("FEISHU_TOKEN_SECRET", "")
    if not secret:
        print("演练需要 FEISHU_TOKEN_SECRET（离线测试密钥）", file=sys.stderr)
        return 2
    result = run_drill(Path(args.work_dir or "backup-drill-work"), token_secret=secret)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
