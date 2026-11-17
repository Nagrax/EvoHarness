"""DE-23 备份/恢复/升级回退演练：真实离线执行，逐项核验。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ops.backup import (
    backup,
    restore,
    rollback_upgrade,
    run_drill,
    verify_restored,
)

TOKEN_SECRET = "offline-test-only-token-secret"


@pytest.fixture()
def work(tmp_path):
    return tmp_path


def test_drill_backup_corrupt_restore_verify_rollback(work):
    """真实演练：种子→备份→破坏→恢复到临时目录→逐项核验→升级回退计时。"""
    result = run_drill(work, token_secret=TOKEN_SECRET)
    assert result["ok"], [s for s in result["steps"] if not s["ok"]]
    steps = {s["step"]: s for s in result["steps"]}
    assert {"seed", "backup", "corrupt_original", "restore_to_temp", "verify",
            "upgrade_rollback", "verify_after_rollback"} <= set(steps)

    # 恢复核验：token 可解密（1 个授权分身），撤权仍有效（1 个撤权分身）
    tokens = result["verify"]["tokens"]
    assert tokens["authorized"] == 1
    assert tokens["decrypted"] == 1, "备份恢复后 token 必须可用测试密钥解密"
    assert tokens["revoked"] == 1
    assert tokens["revoked_still_revoked"] == 1, "撤权分身恢复后必须仍是撤权且无密文"

    # 组织图/任务/知识版本/回执可核对：与备份时刻计数快照一致
    verify = result["verify"]
    assert verify["mismatches"] == []
    counts = verify["counts"]
    assert counts["organizations"] == 1
    assert counts["organization_units"] == 1
    assert counts["positions"] == 1
    assert counts["memberships"] == 1
    assert counts["tasks"] == 1
    assert counts["runs"] == 1
    assert counts["knowledge"]["total"] == 1
    assert counts["knowledge"]["revision_sum"] == 2
    assert counts["outbox"].get("sent") == 1
    assert counts["outbox"].get("delivery_unknown") == 1

    # 升级回退耗时是本机实测值（秒级，取正数）
    assert result["rollbackSeconds"] > 0


def test_restore_refuses_tampered_backup(work):
    deploy = work / "deploy" / "data"
    from ops.backup import _seed_deploy_db

    _seed_deploy_db(deploy / "agent.db", token_secret=TOKEN_SECRET)
    backup_dir = work / "backup"
    backup(deploy / "agent.db", backup_dir)

    # 篡改备份里的 DB 文件：恢复必须拒绝，不能静默恢复坏数据
    db_copy = backup_dir / "agent.db"
    data = bytearray(db_copy.read_bytes())
    data[len(data) // 2] ^= 0xFF
    db_copy.write_bytes(bytes(data))

    with pytest.raises(ValueError) as excinfo:
        restore(backup_dir, work / "restored")
    assert "integrity" in str(excinfo.value)


def test_verify_detects_wrong_key(work):
    deploy = work / "deploy" / "data"
    from ops.backup import _seed_deploy_db

    _seed_deploy_db(deploy / "agent.db", token_secret=TOKEN_SECRET)
    backup_dir = work / "backup"
    manifest = backup(deploy / "agent.db", backup_dir)
    restore(backup_dir, work / "restored")

    wrong_key = verify_restored(work / "restored" / "agent.db",
                                token_secret="a-completely-different-secret")
    assert not wrong_key["ok"]
    assert any("解密失败" in p for p in wrong_key["problems"]), wrong_key["problems"]

    right_key = verify_restored(work / "restored" / "agent.db",
                                token_secret=TOKEN_SECRET, expected=manifest["counts"])
    assert right_key["ok"]


def test_rollback_upgrade_replaces_broken_deploy_db(work):
    deploy = work / "deploy" / "data"
    from ops.backup import _db_opens, _seed_deploy_db

    _seed_deploy_db(deploy / "agent.db", token_secret=TOKEN_SECRET)
    backup_dir = work / "backup"
    backup(deploy / "agent.db", backup_dir)

    # 升级把库写坏：回退到最近一致备份后库必须重新可打开
    (deploy / "agent.db").write_bytes(b"broken upgrade")
    assert not _db_opens(deploy / "agent.db")
    result = rollback_upgrade(backup_dir, deploy)
    assert result["ok"]
    assert result["rollbackSeconds"] >= 0
    assert _db_opens(deploy / "agent.db")
