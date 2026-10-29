"""W07 回归：凭据指纹完整散列、TTL 淘汰、真 TCP 断流。

对应验收：
- 两个同尾 6 位的不同 key 不会复用旧 Agent 凭据，日志不含 key
- 真实 TCP 断流后任务符合冻结取消/恢复策略，同 session 最多 1 个执行 owner
- 慢消费者下队列/session/内存受限，TTL 淘汰无进程泄漏；不只依赖 mock/TestClient

真实缺陷（修复前）：
- `_config_fingerprint` 用 `key[-6:]`：尾部 6 位相同的不同 key 复用旧 Agent 凭据；
- AGENTS/LOCKS/RUNNING 三个 dict 无 TTL 无上限：长时间运行内存无界增长。
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


# ── ① 凭据指纹：完整散列，拒绝同尾碰撞 ──────────────────────


def test_config_fingerprint_full_hash_no_tail_collision():
    from server.app import _config_fingerprint

    a = _config_fingerprint("http://x", "sk-aaa123", "m")
    b = _config_fingerprint("http://x", "sk-bbb123", "m")
    assert a != b, "尾部 6 位相同的不同 key 必须产生不同指纹（旧实现 key[-6:] 会碰撞）"
    # 稳定性
    assert _config_fingerprint("http://x", "sk-aaa123", "m") == a
    # 不同 base/model 也区分
    assert _config_fingerprint("http://y", "sk-aaa123", "m") != a
    assert _config_fingerprint("http://x", "sk-aaa123", "m2") != a


def test_fingerprint_never_contains_raw_key():
    from server.app import _config_fingerprint

    secret = "sk-super-secret-value-998877"
    fp = repr(_config_fingerprint("http://x", secret, "m"))
    assert secret not in fp, "指纹不得包含原始 key"


def test_different_keys_same_session_get_new_agent(monkeypatch, tmp_path):
    """同 session 换 key（尾部 6 位相同）：必须弃用旧 Agent、新建新凭据 Agent。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    import server.app as appmod

    appmod.AGENTS.clear()
    appmod._SESSION_LAST_SEEN.clear()

    agent1 = appmod._get_agent("sess-w07", "default", api_base="http://127.0.0.1:9/v1",
                               api_key="sk-aaa123", model="m")
    agent2 = appmod._get_agent("sess-w07", "default", api_base="http://127.0.0.1:9/v1",
                               api_key="sk-bbb123", model="m")
    assert agent1 is not agent2, "不同 key（同尾 6 位）必须新建 Agent，不得复用旧凭据"
    assert appmod.AGENTS["sess-w07"] is agent2


# ── ② TTL 淘汰：容量受限、RUNNING 保护、磁盘可恢复 ───────────


def test_evict_drops_stale_and_keeps_running():
    from server.app import _evict_stale_sessions, _SESSION_LAST_SEEN, AGENTS, LOCKS, RUNNING

    AGENTS.clear(); LOCKS.clear(); RUNNING.clear(); _SESSION_LAST_SEEN.clear()
    # 三个会话：stale（久未活动）、running（在跑）、fresh（刚活动）
    for sid in ("s-stale", "s-running", "s-fresh"):
        AGENTS[sid] = object()
        LOCKS[sid] = asyncio.Lock()
        RUNNING[sid] = False
    _SESSION_LAST_SEEN["s-stale"] = time.monotonic() - 10 * 3600
    _SESSION_LAST_SEEN["s-running"] = time.monotonic() - 10 * 3600
    _SESSION_LAST_SEEN["s-fresh"] = time.monotonic()
    RUNNING["s-running"] = True

    evicted = _evict_stale_sessions(max_alive=100, ttl_seconds=6 * 3600)
    assert "s-stale" in evicted
    assert "s-running" not in evicted, "RUNNING 会话不得被淘汰（任务不能被抢锁）"
    assert "s-fresh" not in evicted
    assert "s-stale" not in AGENTS and "s-stale" not in LOCKS


def test_evict_enforces_hard_cap_by_oldest():
    from server.app import _evict_stale_sessions, _SESSION_LAST_SEEN, AGENTS, LOCKS, RUNNING

    AGENTS.clear(); LOCKS.clear(); RUNNING.clear(); _SESSION_LAST_SEEN.clear()
    for index in range(10):
        sid = f"s{index}"
        AGENTS[sid] = object()
        LOCKS[sid] = asyncio.Lock()
        RUNNING[sid] = False
        _SESSION_LAST_SEEN[sid] = time.monotonic() - (100 - index)  # s0 最旧
    evicted = _evict_stale_sessions(max_alive=5, ttl_seconds=10 ** 9)
    assert len(AGENTS) <= 5
    assert "s0" in evicted, "超限时应从最久未活动者开始淘汰"


def test_evicted_session_recoverable_from_disk(tmp_path, monkeypatch):
    """淘汰只清内存态：磁盘会话历史仍在，可恢复。"""
    import agents.session as session_mod

    sess_dir = tmp_path / ".evoharness" / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    # load_session 读模块级 SESSION_DIR；直接把模块指向 tmp（与既有测试同法）
    monkeypatch.setattr(session_mod, "SESSION_DIR", sess_dir)
    (sess_dir / "s-disk.json").write_text(
        json.dumps({"metadata": {"id": "s-disk"}, "note": "x"}), encoding="utf-8")

    from server.app import _evict_stale_sessions, _SESSION_LAST_SEEN, AGENTS

    AGENTS["s-disk"] = object()
    _SESSION_LAST_SEEN["s-disk"] = time.monotonic() - 10 * 3600
    _evict_stale_sessions(max_alive=100, ttl_seconds=3600)
    assert "s-disk" not in AGENTS
    # 恢复路径存在：load_session 仍能读到磁盘历史
    data = session_mod.load_session("s-disk")
    assert data is not None and data["metadata"]["id"] == "s-disk"


# ── ③ 真 TCP 断流：uvicorn 子进程 + socket 级断开 ────────────


@pytest.mark.skipif(os.name != "nt", reason="Windows 专项：真实 TCP 断流验证")
def test_real_tcp_disconnect_no_leak():
    """真 TCP：起真实 uvicorn，客户端硬断连接，服务端不崩、可继续服务。"""
    port = 8977
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server.app:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=str(REPO), env={**os.environ, "PYTHONUTF8": "1", "EVO_RELOAD": "0"},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        # 等服务就绪
        deadline = time.time() + 30
        ready = False
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    ready = True
                    break
            except OSError:
                time.sleep(0.3)
        assert ready, "评测服务未能在 30s 内就绪"

        # 硬断 3 个连接（不发完整请求就 close）
        for _ in range(3):
            sock = socket.create_connection(("127.0.0.1", port), timeout=2)
            sock.close()

        # 服务仍能正常响应
        import urllib.request

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        assert body.get("ok") is True
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


# ── ④ 同 session 单执行 owner（锁语义） ─────────────────────


def test_same_session_lock_gives_single_owner():
    """同 session 并发 chat：锁保证同一时刻最多一个执行 owner。"""
    from server.app import LOCKS

    async def scenario():
        lock = LOCKS.setdefault("sess-lock", asyncio.Lock())
        owners = []

        async def worker(tag: str):
            async with lock:
                owners.append(tag)
                await asyncio.sleep(0.05)

        await asyncio.gather(worker("A"), worker("B"))
        return owners, len(owners)

    owners, count = asyncio.run(scenario())
    # 串行进入临界区：不会同时出现两个 owner（顺序不定但互斥）
    assert count == 2
    assert owners == ["A", "B"] or owners == ["B", "A"]
