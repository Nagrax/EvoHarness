"""Single-round operational lease; Task/Run state stays in the business domain."""
from __future__ import annotations

import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


def process_marker(pid: int) -> str | None:
    """Safe process identity without localized tasklist output or Windows signals."""
    if pid <= 0:
        return None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.GetProcessTimes.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return "unknown" if ctypes.get_last_error() == 5 else None
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
                return None
            created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(system), ctypes.byref(user)):
                return "unknown"
            return str((created.dwHighDateTime << 32) | created.dwLowDateTime)
        finally:
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle(handle)
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        started = raw[raw.rfind(")") + 2:].split()[19]
        boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        return boot + ":" + started
    except (FileNotFoundError, ProcessLookupError):
        return None
    except (OSError, IndexError):
        try:
            os.kill(pid, 0)
            return "unknown"
        except ProcessLookupError:
            return None
        except PermissionError:
            return "unknown"


def _connect(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=5, isolation_level=None)
    db.execute("CREATE TABLE IF NOT EXISTS lease (singleton INTEGER PRIMARY KEY CHECK(singleton=1), generation INTEGER NOT NULL, token TEXT NOT NULL, pid INTEGER NOT NULL, marker TEXT, deadline REAL NOT NULL)")
    return db


@dataclass(frozen=True)
class RoundLease:
    path: Path
    generation: int
    token: str
    seconds: float

    @classmethod
    def acquire(cls, path: Path, *, seconds: float = 60) -> RoundLease | None:
        if seconds <= 0:
            raise ValueError("lease_seconds_must_be_positive")
        db = _connect(path)
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT generation, token, pid, marker, deadline FROM lease WHERE singleton=1").fetchone()
            now = time.monotonic()
            if row and row[1] and row[4] > now:
                marker = process_marker(row[2])
                if marker is not None and (marker == row[3] or "unknown" in (marker, row[3])):
                    db.rollback()
                    return None
            generation = row[0] + 1 if row else 1
            token = uuid.uuid4().hex
            db.execute("INSERT OR REPLACE INTO lease VALUES (1,?,?,?,?,?)", (generation, token, os.getpid(), process_marker(os.getpid()), now + seconds))
            db.commit()
            return cls(path, generation, token, seconds)
        finally:
            db.close()

    def current(self) -> bool:
        db = _connect(self.path)
        try:
            row = db.execute("SELECT token, generation, deadline FROM lease WHERE singleton=1").fetchone()
            return bool(row and row[0] == self.token and row[1] == self.generation and row[2] > time.monotonic())
        finally:
            db.close()

    def renew(self) -> bool:
        db = _connect(self.path)
        try:
            now = time.monotonic()
            return db.execute("UPDATE lease SET deadline=? WHERE singleton=1 AND token=? AND generation=? AND deadline>?", (now + self.seconds, self.token, self.generation, now)).rowcount == 1
        finally:
            db.close()

    def release(self) -> bool:
        db = _connect(self.path)
        try:
            return db.execute("UPDATE lease SET token='', deadline=0 WHERE singleton=1 AND token=? AND generation=?", (self.token, self.generation)).rowcount == 1
        finally:
            db.close()
