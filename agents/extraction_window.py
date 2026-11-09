#!/usr/bin/env python3
"""待抽取 Skill 窗口的持久状态（按 session_id 落在会话档案旁）。

窗口原先只在 `Agent` 实例内存里：进程退出/Web 重连后既无法恢复，也无法区分
"没有反馈"与"窗口丢失"。这里沿用 `agents/session.py` 的原子写，把窗口、
领取状态、尝试次数和最后一次错误一起落盘；会话 JSON 的 schema 不变，旧会话
没有该文件时按无窗口处理。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from typing import Any

from agents import session as session_store

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
CLAIM_LEASE_SECONDS = 300
MAX_ATTEMPTS = 3
STATUSES = {"pending", "claimed", "completed", "failed"}
_LOCK_SUFFIX = ".lock"
_THREAD_LOCK = threading.Lock()


class ExtractionWindowError(Exception):
    """窗口状态不可写或载荷非法。"""


def _window_dir():
    return session_store.SESSION_DIR / "extraction"


def _path(session_id: str):
    return _window_dir() / f"{session_id}.json"


def _lock_path(session_id: str):
    return _window_dir() / f"{session_id}.lock"


def new_window_id() -> str:
    return uuid.uuid4().hex


def _now() -> float:
    return time.time()


def _atomic_record(session_id: str, record: dict[str, Any]) -> None:
    if not session_store.is_safe_session_id(session_id):
        raise ExtractionWindowError("unsafe_session_id")
    text = json.dumps(record, indent=2, ensure_ascii=False, default=str) + "\n"
    session_store.atomic_write_text(_path(session_id), text)


def _normalize(record: Any) -> dict[str, Any] | None:
    if not isinstance(record, dict):
        return None
    try:
        if int(record.get("schemaVersion") or SCHEMA_VERSION) > SCHEMA_VERSION:
            return None
    except (TypeError, ValueError):
        return None
    window = record.get("window")
    if not isinstance(window, dict):
        return None
    window_id = str(record.get("windowId") or window.get("window_id") or "").strip()
    if not window_id:
        return None
    status = str(record.get("status") or "pending").strip().lower()
    if status not in STATUSES:
        status = "pending"
    try:
        attempts = max(0, int(record.get("attempts") or 0))
    except (TypeError, ValueError):
        attempts = 0
    try:
        claimed_at = float(record.get("claimedAt") or 0.0)
    except (TypeError, ValueError):
        claimed_at = 0.0
    window["window_id"] = window_id
    return {
        "schemaVersion": SCHEMA_VERSION,
        "windowId": window_id,
        "status": status,
        "attempts": attempts,
        "claimedAt": claimed_at,
        "claimedBy": str(record.get("claimedBy") or ""),
        "completedAt": record.get("completedAt"),
        "updatedAt": record.get("updatedAt"),
        "lastError": str(record.get("lastError") or ""),
        "summary": str(record.get("summary") or ""),
        "window": window,
    }


def load_window(session_id: str) -> dict[str, Any] | None:
    if not session_store.is_safe_session_id(session_id):
        return None
    try:
        path = _path(session_id)
        if not path.is_file():
            return None
        raw = path.read_text(encoding="utf-8")
        return _normalize(json.loads(raw))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _record(
    session_id: str,
    window: dict[str, Any],
    *,
    status: str,
    attempts: int,
    claimed_at: float = 0.0,
    claimed_by: str = "",
    completed_at: float | None = None,
    last_error: str = "",
    summary: str = "",
) -> dict[str, Any]:
    window_id = str(window.get("window_id") or "").strip() or new_window_id()
    window["window_id"] = window_id
    record = {
        "schemaVersion": SCHEMA_VERSION,
        "windowId": window_id,
        "status": status if status in STATUSES else "pending",
        "attempts": max(0, int(attempts)),
        "claimedAt": claimed_at,
        "claimedBy": claimed_by,
        "completedAt": completed_at,
        "updatedAt": _now(),
        "lastError": last_error,
        "summary": summary,
        "window": window,
    }
    _atomic_record(session_id, record)
    return _normalize(record) or record


def save_pending_window(session_id: str, window: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(window, dict):
        raise ExtractionWindowError("window_not_dict")
    return _record(session_id, window, status="pending", attempts=0)


def _acquire_file_lock(session_id: str) -> int | None:
    path = _lock_path(session_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            str(path),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
    except FileExistsError:
        try:
            if _now() - path.stat().st_mtime > CLAIM_LEASE_SECONDS:
                path.unlink(missing_ok=True)
                return _acquire_file_lock(session_id)
        except OSError as exc:
            # 陈旧锁清理失败（并发争抢/文件系统抖动）：本轮视为未获得锁，调用方可重试
            logger.debug("stale lock cleanup failed for %s: %s", session_id, exc)
            return None
        return None
    except OSError:
        return None
    return descriptor


def _release_file_lock(session_id: str, descriptor: int | None) -> None:
    if descriptor is not None:
        try:
            os.close(descriptor)
        except OSError as exc:
            logger.debug("lock fd already closed for %s: %s", session_id, exc)
    try:
        _lock_path(session_id).unlink(missing_ok=True)
    except OSError as exc:
        logger.debug("lock file unlink failed for %s: %s", session_id, exc)


def claim_window(
    session_id: str,
    window_id: str,
    *,
    feedback: str = "",
    now: float | None = None,
) -> dict[str, Any] | None:
    """领取窗口；已领取未过期、已完成或超出重试上限时返回 None。"""
    session_id = str(session_id or "")
    window_id = str(window_id or "").strip()
    if not window_id or not session_store.is_safe_session_id(session_id):
        return None
    current_time = _now() if now is None else float(now)
    with _THREAD_LOCK:
        descriptor = _acquire_file_lock(session_id)
        if descriptor is None:
            return None
        try:
            record = load_window(session_id)
            if record is None or record["windowId"] != window_id:
                return None
            status = record["status"]
            if status == "completed":
                return None
            if status == "claimed" and record["claimedAt"] + CLAIM_LEASE_SECONDS > current_time:
                return None
            attempts = int(record["attempts"]) + 1
            if attempts > MAX_ATTEMPTS:
                record.update(
                    status="failed",
                    attempts=attempts - 1,
                    updatedAt=current_time,
                    lastError="max_attempts",
                )
                _atomic_record(session_id, record)
                return None
            window = dict(record["window"])
            messages = list(window.get("messages") or [])
            if feedback:
                messages.append({"role": "user", "content": feedback})
            window["messages"] = messages[-10:]
            window["next_user_feedback"] = feedback
            claimed = _record(
                session_id,
                window,
                status="claimed",
                attempts=attempts,
                claimed_at=current_time,
                claimed_by=str(os.getpid()),
            )
            return claimed
        finally:
            _release_file_lock(session_id, descriptor)


def mark_completed(
    session_id: str,
    window_id: str,
    *,
    summary: str = "",
    now: float | None = None,
) -> dict[str, Any] | None:
    current = load_window(session_id)
    if current is None or current["windowId"] != str(window_id or ""):
        return None
    return _record(
        session_id,
        dict(current["window"]),
        status="completed",
        attempts=int(current["attempts"]),
        completed_at=_now() if now is None else float(now),
        summary=summary,
    )


def mark_failed(
    session_id: str,
    window_id: str,
    *,
    error: str,
    now: float | None = None,
) -> dict[str, Any] | None:
    current = load_window(session_id)
    if current is None or current["windowId"] != str(window_id or ""):
        return None
    return _record(
        session_id,
        dict(current["window"]),
        status="failed",
        attempts=int(current["attempts"]),
        claimed_at=float(current["claimedAt"] or 0.0),
        last_error=str(error or "unknown_error")[:400],
    )


def recover_window(
    session_id: str,
    *,
    now: float | None = None,
    reclaim_claimed: bool = False,
) -> dict[str, Any] | None:
    """恢复时读取窗口。

    过期未完成的领取转为 failed；`reclaim_claimed=True` 用于新进程/页面恢复：
    进程已不存在，保留 claimed 只会让窗口永远无法重试。
    """
    current_time = _now() if now is None else float(now)
    record = load_window(session_id)
    if record is None:
        return None
    claimed_here = record.get("claimedBy") == str(os.getpid())
    if record["status"] == "claimed" and (
        (reclaim_claimed and not claimed_here)
        or record["claimedAt"] + CLAIM_LEASE_SECONDS <= current_time
    ):
        expired = record["claimedAt"] + CLAIM_LEASE_SECONDS <= current_time
        failed = mark_failed(
            session_id,
            record["windowId"],
            error=(
                "claim_expired_before_completion"
                if expired
                else "claim_interrupted_before_completion"
            ),
            now=current_time,
        )
        return failed or record
    return record


def clear_window(session_id: str) -> None:
    if not session_store.is_safe_session_id(session_id):
        return
    try:
        _path(session_id).unlink(missing_ok=True)
    except OSError as exc:
        logger.debug("window file unlink failed for %s: %s", session_id, exc)
    try:
        _lock_path(session_id).unlink(missing_ok=True)
    except OSError as exc:
        logger.debug("window lock unlink failed for %s: %s", session_id, exc)