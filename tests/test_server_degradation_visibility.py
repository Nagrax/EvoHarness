"""server/app.py 三处降级可见性回归（2026-10-29 判读）。

三处都属"安全降级但缺可见性"：
1. 淘汰会话时 abort 失败 → 僵尸任务（对象移除但任务仍在跑）
2. 换凭据时 abort 失败 → 同上
3. 修复成功后缓存刷新失败 → 用户看到"修复成功"却要重启才生效（状态不一致）
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

SRC = REPO / "server" / "app.py"


def test_abort_failures_are_logged():
    text = SRC.read_text(encoding="utf-8")
    assert "[sessions] abort failed for" in text, "淘汰路径的 abort 失败必须可见"
    assert "[sessions] abort failed on credential switch" in text, (
        "换凭据路径的 abort 失败必须可见")
    # 两处都不得再是无注释的 pass
    for marker in ("abort failed for", "abort failed on credential switch"):
        idx = text.find(marker)
        assert idx > 0
        window = text[max(0, idx - 260): idx]
        assert "except Exception as error" in window, f"{marker} 必须捕获为具名变量"


def test_eviction_still_removes_session():
    """记录失败不得影响淘汰本身（AGENTS/LOCKS 仍要清）。"""
    text = SRC.read_text(encoding="utf-8")
    idx = text.find("[sessions] abort failed for")
    tail = text[idx: idx + 400]
    assert "LOCKS.pop(sid, None)" in tail


def test_cache_refresh_failure_surfaces_to_caller():
    """修复成功但缓存未刷新必须回传，否则前端会误报"已生效"。"""
    text = SRC.read_text(encoding="utf-8")
    assert 'result["cacheRefreshError"]' in text
    assert 'result["hint"]' in text
    assert "需重启服务才会生效" in text
    # 在 repair_apply 函数体内检查：刷新只在修复成功时发生，且最终 return result
    start = text.find("async def repair_apply")
    assert start > 0
    end = text.find("@app.", start + 10)
    body = text[start: end if end > 0 else start + 2000]
    assert 'if result.get("ok"):' in body, "只在修复成功时才谈缓存刷新"
    assert "cacheRefreshError" in body
    assert "return result" in body


def test_repair_still_returns_result_on_cache_failure():
    """缓存刷新失败不得抛异常（否则修复结果丢失）。"""
    text = SRC.read_text(encoding="utf-8")
    idx = text.find("[repair] cache refresh failed")
    assert idx > 0
    tail = text[idx: idx + 300]
    assert "raise" not in tail, "不得把缓存刷新失败变成 500"
