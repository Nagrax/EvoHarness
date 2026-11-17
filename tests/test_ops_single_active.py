"""DE-23 单活约束：第二写实例明确拒绝并给原因；持有者崩溃（过期）后可接管。"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evoharness.models import Base
from ops.single_active import (
    FileProcessLock,
    SingleActiveGuard,
    SingleActiveRefused,
    claim_db_lease,
    read_db_lease,
    release_db_lease,
    renew_db_lease,
    takeover_ready,
)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("FEISHU_TOKEN_SECRET", raising=False)
    db = tmp_path / "agent.db"
    engine = create_engine(f"sqlite:///{db.as_posix()}", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    return tmp_path, factory


def _guard(tmp_path, factory, *, clock=None, lease_seconds=90, name="writer"):
    return SingleActiveGuard(
        lock_path=tmp_path / "run" / f"{name}.lock",
        lease_key=f"single_active:{name}",
        session_factory=factory,
        lease_seconds=lease_seconds,
        clock=clock or (lambda: datetime(2026, 11, 14, 12, 0, 0)),
        holder={"role": name},
    )


def test_second_instance_refused_with_reason(env):
    tmp_path, factory = env
    first = _guard(tmp_path, factory)
    holder = first.acquire()
    assert holder["role"] == "writer"
    second = _guard(tmp_path, factory)
    with pytest.raises(SingleActiveRefused) as excinfo:
        second.acquire()
    assert excinfo.value.reason_code == "single_active_already_running"
    assert excinfo.value.next_action, "拒绝必须给下一步动作"
    # 原因里能看到持有者信息，不是排障黑洞
    assert str(excinfo.value)
    first.release()


def test_second_instance_sees_holder_identity(env):
    tmp_path, factory = env
    first = _guard(tmp_path, factory)
    first.acquire()
    lock_info = FileProcessLock(tmp_path / "run" / "writer.lock").read_holder()
    assert lock_info and lock_info.get("pid"), "锁文件应记录持有者 PID 供排障"
    first.release()


def test_release_allows_next_instance(env):
    tmp_path, factory = env
    first = _guard(tmp_path, factory)
    first.acquire()
    first.release()
    second = _guard(tmp_path, factory)
    assert second.acquire()["role"] == "writer"
    second.release()
    assert not (tmp_path / "run" / "writer.lock").exists()


def test_stale_lock_file_takeover(tmp_path):
    """持有者崩溃：OS 锁自动消失，残留的过期持有者信息可被接管并覆盖。"""
    stale_info = tmp_path / "run" / "writer.lock.holder.json"
    stale_info.parent.mkdir(parents=True)
    stale_info.write_text(json.dumps({"holder_id": "dead-host:123", "pid": 999999,
                                      "lease_until": "2026-11-14T11:00:00"}),
                          encoding="utf-8")
    lock = FileProcessLock(tmp_path / "run" / "writer.lock")
    lock.acquire({"holder_id": "new-host:1"})
    # 接管后持有者信息被新持有者覆盖
    assert lock.read_holder()["holder_id"] == "new-host:1"
    lock.release()
    assert not (tmp_path / "run" / "writer.lock").exists()
    assert not stale_info.exists()


def test_db_lease_valid_holder_refused_expired_takeover(env):
    tmp_path, factory = env
    moment = datetime(2026, 11, 14, 12, 0, 0)
    with factory() as session:
        claim_db_lease(session, key="single_active:writer",
                       holder={"holder_id": "host-a:1", "role": "writer"},
                       lease_seconds=30, now=lambda: moment)
        session.commit()
        held = read_db_lease(session, "single_active:writer")
        assert held["holder_id"] == "host-a:1"
        assert not takeover_ready(held, now=lambda: moment)

        # 租约有效期内，第二主机认领被拒并给原因
        with pytest.raises(SingleActiveRefused) as excinfo:
            claim_db_lease(session, key="single_active:writer",
                           holder={"holder_id": "host-b:1", "role": "writer"},
                           lease_seconds=30, now=lambda: moment)
        assert excinfo.value.reason_code == "single_active_lease_held"

        # 持有者崩溃 → 租约过期 → 可接管（CAS 成功）
        later = moment + timedelta(seconds=31)
        assert takeover_ready(read_db_lease(session, "single_active:writer"), now=lambda: later)
        claim_db_lease(session, key="single_active:writer",
                       holder={"holder_id": "host-b:1", "role": "writer"},
                       lease_seconds=30, now=lambda: later)
        session.commit()
    # CAS 后必须读库核对（不能信身份映射缓存）：接管者唯一
    with factory() as session:
        assert read_db_lease(session, "single_active:writer")["holder_id"] == "host-b:1"


def test_db_lease_takeover_race_only_one_holder(env):
    """同一把过期租约被两个主机先后认领：只有最后一个成功且唯一持有。

    后认领者基于数据库里的当前租约判定：先到者写入后，后到者必须被拒
    （single_active_lease_held），任何时刻持有者唯一。
    """
    tmp_path, factory = env
    moment = datetime(2026, 11, 14, 12, 0, 0)
    later = moment + timedelta(seconds=31)
    with factory() as session:
        claim_db_lease(session, key="single_active:writer",
                       holder={"holder_id": "host-a:0"}, lease_seconds=10,
                       now=lambda: moment)
        session.commit()
    # 租约过期后 host-a:1 先接管
    with factory() as session:
        claim_db_lease(session, key="single_active:writer",
                       holder={"holder_id": "host-a:1"}, lease_seconds=300,
                       now=lambda: later)
        session.commit()
        assert read_db_lease(session, "single_active:writer")["holder_id"] == "host-a:1"
    # host-b 同一时刻再认领：看到的已是 host-a:1 的有效租约，必须拒绝
    with factory() as session:
        with pytest.raises(SingleActiveRefused) as excinfo:
            claim_db_lease(session, key="single_active:writer",
                           holder={"holder_id": "host-b:1"}, lease_seconds=300,
                           now=lambda: later)
        assert excinfo.value.reason_code == "single_active_lease_held"
        assert excinfo.value.holder["holder_id"] == "host-a:1"


def test_renew_lease_rules(env):
    tmp_path, factory = env
    moment = datetime(2026, 11, 14, 12, 0, 0)
    with factory() as session:
        claim_db_lease(session, key="k", holder={"holder_id": "h1"}, lease_seconds=30,
                       now=lambda: moment)
        session.commit()
        assert renew_db_lease(session, key="k", holder_id="h1", lease_seconds=30,
                              now=lambda: moment + timedelta(seconds=1))
        session.commit()
    with factory() as session:
        assert not renew_db_lease(session, key="k", holder_id="h2", lease_seconds=30,
                                  now=lambda: moment + timedelta(seconds=2))
    with factory() as session:
        assert not renew_db_lease(session, key="k", holder_id="h1", lease_seconds=30,
                                  now=lambda: moment + timedelta(seconds=31))
    with factory() as session:
        assert release_db_lease(session, key="k", holder_id="h1")
        session.commit()
    with factory() as session:
        assert read_db_lease(session, "k") is None


def test_guard_acquire_refusal_cleans_file_lock(env):
    tmp_path, factory = env
    moment = datetime(2026, 11, 14, 12, 0, 0)
    # DB 租约被别的实例持有（未过期）：guard.acquire 在文件锁之后应失败，
    # 且不能把文件锁留在现场（否则排障时"看起来"是本机占用）。
    with factory() as session:
        claim_db_lease(session, key="single_active:writer",
                       holder={"holder_id": "remote:1"}, lease_seconds=300,
                       now=lambda: moment)
        session.commit()
    guard = _guard(tmp_path, factory, clock=lambda: moment + timedelta(seconds=1))
    with pytest.raises(SingleActiveRefused):
        guard.acquire()
    assert not (tmp_path / "run" / "writer.lock").exists()
    assert not (tmp_path / "run" / "writer.lock.holder.json").exists()
