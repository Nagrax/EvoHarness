"""DE-15：预算原子预留、结算归集与保守估算。

R04 修复后：限额校验+预留是产品事务边界内的单条原子 SQL（INSERT...SELECT...WHERE），
判定不依赖 Python 层先读后写——并发测试一律直接调用正式入口，不加任何测试侧锁。
"""
from pathlib import Path
import json
import os
import subprocess
import sys
import threading
import time
import warnings

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import SADeprecationWarning
from sqlalchemy.orm import sessionmaker

from evoharness.governance import budget
from evoharness.governance.budget import BudgetError
from evoharness.governance.models import BudgetLimit
from evoharness.models import Base


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as s:
        yield s
    engine.dispose()


def test_unconfigured_space_never_auto_spends(session):
    budget.set_limit(session, scope_kind="org", scope_id="org", limit_tokens=1000)
    # org 配置但 space 未配置：生产空间不自动付费
    with pytest.raises(BudgetError, match="budget_not_configured:space-x"):
        budget.reserve(session, org_id="org", space_id="space-x", category="chat", tokens=10)
    budget.set_limit(session, scope_kind="space", scope_id="space-x", limit_tokens=100)
    ok = budget.reserve(session, org_id="org", space_id="space-x", category="chat", tokens=10)
    assert ok.status == "reserved"


def test_unscoped_reservation_is_allowed_without_sqlalchemy_deprecation(session):
    with warnings.catch_warnings():
        warnings.simplefilter("error", SADeprecationWarning)
        row = budget.reserve(session, category="dreaming", tokens=1)
    assert row.status == "reserved"


def test_limit_enforced_across_scopes(session):
    budget.set_limit(session, scope_kind="org", scope_id="org", limit_tokens=100)
    budget.set_limit(session, scope_kind="space", scope_id="s1", limit_tokens=60)
    budget.set_limit(session, scope_kind="principal", scope_id="alice", limit_tokens=30)
    # principal 层最先约束
    with pytest.raises(BudgetError, match="budget_exhausted:principal:alice"):
        budget.reserve(session, org_id="org", space_id="s1", principal_id="alice",
                       category="chat", tokens=50)
    budget.reserve(session, org_id="org", space_id="s1", principal_id="alice",
                   category="chat", tokens=30)
    budget.set_limit(session, scope_kind="principal", scope_id="alice", limit_tokens=200)
    # space 层约束接续生效（人工补额度后可继续）
    budget.reserve(session, org_id="org", space_id="s1", principal_id="alice",
                   category="chat", tokens=30)
    with pytest.raises(BudgetError, match="budget_exhausted:space:s1"):
        budget.reserve(session, org_id="org", space_id="s1", principal_id="alice",
                       category="chat", tokens=1)


def test_settle_release_and_truthful_accounting(session):
    budget.set_limit(session, scope_kind="org", scope_id="org", limit_tokens=100)
    first = budget.reserve(session, org_id="org", category="chat", tokens=40, estimated=True)
    # 真实 usage 少于预留：只记真实值，额度回到可用池
    budget.settle(session, first.reservation_id, used_tokens=25)
    # 失败调用同样归集（不留免费副作用）
    failed = budget.reserve(session, org_id="org", category="judge", tokens=10)
    budget.settle(session, failed.reservation_id, used_tokens=8, ok=False)
    # 取消调用：释放预留不泄漏
    cancelled = budget.reserve(session, org_id="org", category="chat", tokens=50)
    budget.release(session, cancelled.reservation_id)
    # 释放后 100 限额内可再预留 65（已消耗 25+8=33）
    again = budget.reserve(session, org_id="org", category="chat", tokens=65)
    assert again.status == "reserved"
    with pytest.raises(BudgetError, match="budget_exhausted:org:org"):
        budget.reserve(session, org_id="org", category="chat", tokens=3)
    summary = budget.usage_summary(session, org_id="org")
    assert summary["used_total"] == 33
    assert summary["failed_settled"] == 1
    assert summary["estimated_settled"] == 1
    assert summary["by_category"] == {"chat": 25, "judge": 8}
    # 已关闭预留不能重复结算/释放
    with pytest.raises(BudgetError, match="reservation_already_closed"):
        budget.settle(session, first.reservation_id, used_tokens=1)
    with pytest.raises(BudgetError, match="reservation_not_found"):
        budget.release(session, "missing-reservation")


# ── 并发原子性（R04）：判定在数据库原子语句内，测试侧不加任何锁 ────────────────


def _file_db(tmp_path: Path):
    engine = create_engine(
        "sqlite:///" + str(tmp_path / "budget.db"), future=True,
        connect_args={"timeout": 30})  # SQLite 层 busy 超时：并发写串行化，不是测试侧锁
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


def test_concurrent_reservations_never_exceed_limit(tmp_path):
    """8 线程真并发直连正式入口（无测试侧锁/无手工 BEGIN IMMEDIATE）：
    限额 5×10=50，每线程预留 10 → 恰好 5 成功、其余 budget_exhausted，总预留≤限额。"""
    engine, factory = _file_db(tmp_path)
    with factory.begin() as s:
        budget.set_limit(s, scope_kind="org", scope_id="org", limit_tokens=50)
    outcomes = []
    start = threading.Barrier(8)

    def worker(i: int):
        try:
            with factory.begin() as s:
                budget.reserve(s, org_id="org", category="chat", tokens=10)
            outcomes.append("ok")
        except BudgetError:
            outcomes.append("exhausted")
        except Exception as exc:  # 不允许出现数据库级失败（应归为预算拒绝）
            outcomes.append(f"unexpected:{type(exc).__name__}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert outcomes.count("ok") == 5, outcomes
    assert outcomes.count("exhausted") == 3, outcomes
    with factory() as s:
        summary = budget.usage_summary(s, org_id="org")
        assert summary["reserved_open"] == 50
        assert summary["reserved_open"] <= 50  # 总预留不超限额
    engine.dispose()


def test_two_port_instances_concurrent_reserve_atomic(tmp_path):
    """两个独立 De15BudgetPort 实例并发 reserve：60+60 > 100 → 恰好一笔成功、总预留≤限额。"""
    from agents.skill_search.controller import De15BudgetPort

    engine, factory = _file_db(tmp_path)
    port_a = De15BudgetPort(factory, org_id="org", scope_prefix="searchA")
    port_b = De15BudgetPort(factory, org_id="org", scope_prefix="searchB")
    port_a.set_limits(org_tokens=100)
    outcomes = {}
    start = threading.Barrier(2)

    def worker(name: str, port: De15BudgetPort, tokens: int = 60):
        start.wait(timeout=30)
        try:
            outcomes[name] = port.reserve("audit", tokens, memo=name)
        except Exception as exc:
            outcomes[name] = exc

    threads = [threading.Thread(target=worker, args=("port-a", port_a)),
               threading.Thread(target=worker, args=("port-b", port_b))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    succeeded = [name for name, value in outcomes.items() if not isinstance(value, Exception)]
    assert len(succeeded) == 1, outcomes
    rejected = outcomes[[n for n in outcomes if n not in succeeded][0]]
    assert isinstance(rejected, BudgetError) and "budget_exhausted" in str(rejected)
    summary = port_a.usage_summary()
    assert summary["reserved_open"] == 60  # ≤ 100：第二笔被原子拒绝

    # 未超限时两实例并发都可成功（原子性不等于互斥拒绝）：先释放获胜方的预留
    winner_port = port_a if "port-a" in succeeded else port_b
    winner_port.release(outcomes[succeeded[0]]["handle"])
    port_a.set_limits(org_tokens=100)
    outcomes.clear()
    start = threading.Barrier(2)
    threads = [threading.Thread(target=worker, args=("port-a", port_a, 30)),
               threading.Thread(target=worker, args=("port-b", port_b, 30))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert all(not isinstance(v, Exception) for v in outcomes.values()), outcomes
    summary = port_b.usage_summary()
    assert summary["reserved_open"] == 60  # 30+30，总量不超限额
    engine.dispose()


def test_cross_process_reserve_never_exceeds_limit(tmp_path):
    """跨进程：两个真实子进程同时 reserve 60（限额 100）→ 恰好一个成功，总预留≤限额。"""
    engine, factory = _file_db(tmp_path)
    with factory.begin() as s:
        budget.set_limit(s, scope_kind="org", scope_id="org", limit_tokens=100)
    engine.dispose()

    db_path = str(tmp_path / "budget.db")
    go_file = tmp_path / "go.flag"
    child = r"""
import json, os, sys, time
sys.path.insert(0, os.environ["EVO_REPO"])
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from evoharness.models import Base
from evoharness.governance import budget

engine = create_engine("sqlite:///" + os.environ["EVO_DB"], connect_args={"timeout": 30})
factory = sessionmaker(bind=engine, expire_on_commit=False)
while not os.path.exists(os.environ["EVO_GO"]):
    time.sleep(0.005)
outcome = {"ok": False}
try:
    with factory.begin() as session:
        budget.reserve(session, org_id="org", category="chat", tokens=int(os.environ["EVO_TOKENS"]))
    outcome["ok"] = True
except Exception as exc:
    outcome["error"] = f"{type(exc).__name__}:{exc}"
print(json.dumps(outcome), flush=True)
"""
    env = dict(os.environ)
    env.update({
        "EVO_REPO": str(Path(__file__).resolve().parents[1]),
        "EVO_DB": db_path,
        "EVO_GO": str(go_file),
        "EVO_TOKENS": "60",
        "PYTHONIOENCODING": "utf-8",
    })
    children = [subprocess.Popen([sys.executable, "-c", child], env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for _ in range(2)]
    time.sleep(0.5)  # 两个进程都就位并轮询 go 标记
    go_file.write_text("go", encoding="utf-8")
    outputs = []
    for proc in children:
        stdout, stderr = proc.communicate(timeout=60)
        assert proc.returncode == 0, stderr
        outputs.append(json.loads(stdout.strip().splitlines()[-1]))
    succeeded = [o for o in outputs if o.get("ok")]
    assert len(succeeded) == 1, outputs
    engine, factory = _file_db(tmp_path)
    with factory() as s:
        summary = budget.usage_summary(s, org_id="org")
        assert summary["reserved_open"] == 60  # ≤ 100：另一进程被原子拒绝
    engine.dispose()


def test_concurrent_settle_and_release_are_one_shot(tmp_path):
    """结算/释放路径同样原子：并发重复结算恰好一次生效，并发释放/结算互斥。"""
    engine, factory = _file_db(tmp_path)
    with factory.begin() as s:
        budget.set_limit(s, scope_kind="org", scope_id="org", limit_tokens=1000)
        first = budget.reserve(s, org_id="org", category="chat", tokens=60)
        second = budget.reserve(s, org_id="org", category="chat", tokens=30)
    outcomes = {}
    start = threading.Barrier(2)

    def settle_worker(used: int):
        start.wait(timeout=30)
        try:
            with factory() as s:
                budget.settle(s, first.reservation_id, used_tokens=used)
                s.commit()
            outcomes[f"settle-{used}"] = "ok"
        except BudgetError as exc:
            outcomes[f"settle-{used}"] = str(exc)

    threads = [threading.Thread(target=settle_worker, args=(20,)),
               threading.Thread(target=settle_worker, args=(50,))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert list(outcomes.values()).count("ok") == 1, outcomes
    assert "reservation_already_closed" in list(outcomes.values()), outcomes
    first_used = 20 if outcomes.get("settle-20") == "ok" else 50
    with factory() as s:
        summary = budget.usage_summary(s, org_id="org")
        assert summary["used_total"] == first_used
        assert summary["reserved_open"] == 30  # 只结算了 first；second 仍占用

    # 并发释放 vs 结算：恰好一个成功
    outcomes.clear()
    start = threading.Barrier(2)

    def release_worker():
        start.wait(timeout=30)
        try:
            with factory() as s:
                budget.release(s, second.reservation_id)
                s.commit()
            outcomes["release"] = "ok"
        except BudgetError as exc:
            outcomes["release"] = str(exc)

    def settle_second():
        start.wait(timeout=30)
        try:
            with factory() as s:
                budget.settle(s, second.reservation_id, used_tokens=10)
                s.commit()
            outcomes["settle-second"] = "ok"
        except BudgetError as exc:
            outcomes["settle-second"] = str(exc)

    threads = [threading.Thread(target=release_worker),
               threading.Thread(target=settle_second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert list(outcomes.values()).count("ok") == 1, outcomes
    assert "reservation_already_closed" in list(outcomes.values()), outcomes
    with factory() as s:
        summary = budget.usage_summary(s, org_id="org")
        assert summary["reserved_open"] == 0  # 预留不再泄漏
        if summary["released"] == 1:
            assert summary["used_total"] == first_used  # 释放获胜：first 的结算用量
        else:
            assert summary["used_total"] == first_used + 10  # 结算获胜：只记一次真实用量
    engine.dispose()


def test_concurrent_set_limit_upserts_single_row(tmp_path):
    """并发 set_limit 原子 upsert：两线程同时配置同一 scope → 恰好一行，值取其一。"""
    engine, factory = _file_db(tmp_path)
    values = {333, 777}
    start = threading.Barrier(2)

    def worker(limit: int):
        start.wait(timeout=30)
        with factory() as s:
            budget.set_limit(s, scope_kind="org", scope_id="org", limit_tokens=limit)
            s.commit()

    threads = [threading.Thread(target=worker, args=(333,)),
               threading.Thread(target=worker, args=(777,))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    with factory() as s:
        rows = list(s.query(BudgetLimit).filter(
            BudgetLimit.scope_kind == "org", BudgetLimit.scope_id == "org"))
        assert len(rows) == 1
        assert rows[0].limit_tokens in values
    engine.dispose()
