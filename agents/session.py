
#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

from typing import Any
import json
import os
import re
import time

SESSION_DIR = Path.home() / ".evoharness" / "sessions"
SCHEMA_VERSION = 2
# 前一有效版本：写盘崩溃/磁盘满/replace 失败时可回滚，不丢上一份好快照。
BACKUP_SUFFIX = ".prev"


class SessionSnapshotError(Exception):
    """快照不可用：区分"缺失""损坏""schema 不兼容"，避免调用方一律当作 empty。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fsync_dir(path: Path) -> None:
    """尽可能 fsync 目录项，让 rename 持久化；Windows 上目录 fsync 常不可用，忽略即可。"""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_text(path: Path, text: str) -> None:
    """唯一临时文件 → flush → fsync → os.replace 的原子写。

    不用固定 `.tmp` 名：并发写者会互相覆盖对方的临时文件，反而制造半截文件。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    descriptor = None
    try:
        descriptor = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = None
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(tmp), str(path))
        _fsync_dir(path.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def backup_file(path: Path, suffix: str = BACKUP_SUFFIX) -> bool:
    """把当前有效文件复制为 `<name>.prev`；不存在则返回 False。"""
    if not path.is_file():
        return False
    try:
        data = path.read_bytes()
    except OSError:
        return False
    atomic_write_bytes(path.with_name(path.name + suffix), data)
    return True


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    descriptor = None
    try:
        descriptor = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(tmp), str(path))
        _fsync_dir(path.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _session_path(session_id: str) -> Path:
    return SESSION_DIR / f"{session_id}.json"


def is_safe_session_id(session_id: str) -> bool:
    """会话 id 白名单：仅允许 [A-Za-z0-9_.-]，且禁止分隔符与 `..`。

    会话 id 会被拼成 SESSION_DIR/<id>.json，白名单是防路径穿越的读写两侧防线。
    """
    value = str(session_id or "")
    if not value or "/" in value or "\\" in value or ".." in value:
        return False
    return re.fullmatch(r"[A-Za-z0-9_.\-]{1,128}", value) is not None



def _ensure_dir() -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)


def get_project_session_dir() -> Path:
    d = Path.cwd() / ".evoharness" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_session(session_id: str, data: dict[str, Any]) -> None:
    """原子保存会话快照：先留上一份有效版本，再 reject-safe 替换。

    旧实现直接 write_text 覆盖：写盘中断/磁盘满会留下半截 JSON，下次启动即"会话消失"；
    并且异常会被上层吞掉，用户看不到任何诊断。
    """
    if not is_safe_session_id(session_id):
        return
    _ensure_dir()
    path = _session_path(session_id)
    payload = dict(data)
    payload.setdefault("schemaVersion", SCHEMA_VERSION)
    text = json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n"
    backup_file(path)
    atomic_write_text(path, text)


def get_stage_dir(plan_id: str) -> Path:
    """Loop 阶段机目录：项目级 .evoharness/sessions/<plan_id>/（与折叠记忆落盘同根）。

    plan_id 即激活会话的 session_id（uuid hex），目录名永不含用户文本。
    通过模块全局函数调用 get_project_session_dir，测试可 monkeypatch 重定向。
    """
    d = get_project_session_dir() / plan_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_stage_segment(plan_dir: Path | str, filename: str, data: dict[str, Any]) -> None:
    """段会话文件：显式 utf-8 + 同目录临时文件 + os.replace 原子写（崩溃不留半截文件）。"""
    path = Path(plan_dir) / filename
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    os.replace(str(tmp), str(path))


def load_stage_segment(plan_dir: Path | str, filename: str) -> dict[str, Any] | None:
    path = Path(plan_dir) / filename
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def load_session_detailed(session_id: str) -> tuple[dict[str, Any] | None, str]:
    """读取会话快照并区分四种状态：ok / missing / corrupt / incompatible。

    旧实现把损坏与缺失都吞成 None：用户看到的是"会话没了"，无从判断是首次启动、
    文件被截断，还是 schema 太旧。这里把原因显式带回，供 UI 显示脱敏诊断。
    """
    if not is_safe_session_id(session_id):
        return None, "invalid-id"
    path = _session_path(session_id)
    if not path.is_file():
        return None, "missing"
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None, "unreadable"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None, "corrupt"
    if not isinstance(data, dict):
        return None, "corrupt"
    version = data.get("schemaVersion")
    if version is not None:
        try:
            if int(version) > SCHEMA_VERSION:
                return None, "incompatible"
        except (TypeError, ValueError):
            return None, "corrupt"
    return data, "ok"


def load_session_fallback(session_id: str) -> tuple[dict[str, Any] | None, str]:
    """主文件不可用时回退到 `.prev`（写盘中断前的上一份有效快照）。

    只对 corrupt / unreadable 回退；missing 说明从未保存过，incompatible 说明版本太新，
    这两类回退到旧版本只会掩盖真实原因。
    """
    data, reason = load_session_detailed(session_id)
    if data is not None:
        return data, reason
    if reason not in {"corrupt", "unreadable"} or not is_safe_session_id(session_id):
        return None, reason
    main = _session_path(session_id)
    backup = main.with_name(main.name + BACKUP_SUFFIX)
    if not backup.is_file():
        return None, reason
    try:
        parsed = json.loads(backup.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, reason
    if not isinstance(parsed, dict):
        return None, reason
    return parsed, "recovered-from-backup"


def save_folded_session_memory(session_id: str, record: dict[str, Any]) -> None:
    d = get_project_session_dir()
    line = json.dumps(record, ensure_ascii=False, default=str)
    with (d / f"{session_id}.folded-memory.jsonl").open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    atomic_write_text(
        d / f"{session_id}.folded-memory.latest.json",
        json.dumps(record, indent=2, ensure_ascii=False, default=str) + "\n",
    )


def load_session(session_id: str) -> dict[str, Any] | None:
    data, _reason = load_session_detailed(session_id)
    return data


# 最近一次 list_sessions 跳过的坏文件（file + reason）。UI 可据此提示
# "某些会话打不开"，而不是让它们静默从列表里消失。
last_list_errors: list[dict[str, str]] = []


def list_sessions() -> list[dict[str, Any]]:
    """列出会话元信息；损坏文件不静默消失，而是记入 last_list_errors。"""
    global last_list_errors
    last_list_errors = []
    _ensure_dir()
    results = []
    for f in SESSION_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if "metadata" in data:
                results.append(data["metadata"])
            else:
                last_list_errors.append({"file": f.name, "reason": "no-metadata"})
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            last_list_errors.append({"file": f.name, "reason": f"{type(error).__name__}: {error}"[:160]})
    return results


def get_latest_session_id() -> str | None:
    sessions = list_sessions()
    if not sessions:
        return None
    sessions.sort(key=lambda s: s.get("startTime", ""), reverse=True)
    return sessions[0].get("id")
