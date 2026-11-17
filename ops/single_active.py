"""单活写实例守卫：第二实例明确拒绝启动并给出原因（DE-23）。

为什么需要两层
--------------
- 同一台机器：OS 级排他锁（Windows `msvcrt.locking` / POSIX `fcntl.flock`）是
  权威仲裁——持有者崩溃时内核自动释放锁，第二实例能立即感知并接管。
- 跨主机（共享同一个业务库）：SQLite/单库没有多写者协调，用 `system_state`
  里的租约做乐观 CAS 认领：只有"当前租约已过期或空缺"才允许写入新租约，
  写入以"旧值精确匹配"为条件，两个主机同时认领只有一个成功。

拒绝必须给原因：`SingleActiveRefused` 带 reason_code、持有者信息与下一步动作，
不允许出现"启动失败但不知道谁在跑"的排障黑洞。

单活的口径：**写实例**（supervisor 主管的常驻服务）单活；只读面板/CLI 不在此列。
"""
from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from sqlalchemy import update

from evoharness.clock import utcnow
from evoharness.models import SystemState

DEFAULT_LEASE_SECONDS = 90


@dataclass
class SingleActiveRefused(Exception):
    """第二实例启动被拒绝；reason_code + 持有者信息 + 恢复动作。"""

    reason_code: str
    holder: dict
    next_action: str = ""

    def __str__(self) -> str:  # 让 CLI/日志直接可读
        return (
            f"{self.reason_code}: 已有单活实例在运行 "
            f"holder={json.dumps(self.holder, ensure_ascii=False)}"
            + (f"；下一步：{self.next_action}" if self.next_action else "")
        )


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def pid_alive(pid: int) -> bool:
    """只做存活探测，绝不发信号（Windows 上 os.kill(pid,0) 会直接杀进程）。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)
    import errno

    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM  # EPERM 说明进程存在但无权限
    return True


class FileProcessLock:
    """同主机排他锁：持有即独占，崩溃自动释放。

    锁文件本身只做 OS 锁（Windows 上被锁区域对其他句柄不可读），
    持有者信息写在旁边的 `*.holder.json` 观测文件——第二实例被拒时
    能读到"谁在跑"，排障不进黑洞。
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.info_path = Path(str(path) + ".holder.json")
        self._fd: int | None = None

    # -- OS 锁原语 ----------------------------------------------------------
    def _try_lock(self, fd: int) -> bool:
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                return False
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(self, fd: int) -> None:
        if os.name == "nt":
            import msvcrt

            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl

            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass

    # -- 生命周期 -----------------------------------------------------------
    def acquire(self, holder: dict) -> None:
        """拿到 OS 锁并写入持有者信息；被占用抛 SingleActiveRefused。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        if not self._try_lock(fd):
            os.close(fd)
            info = self.read_holder()
            raise SingleActiveRefused(
                reason_code="single_active_already_running",
                holder=info or {"lock": str(self.path)},
                next_action=(
                    "先确认既有实例是否应继续运行；确认误启动则不要重复拉起，"
                    "需要接管时先停掉持有者（ops/supervisor stop）再启动"
                ),
            )
        self._fd = fd
        try:
            self.info_path.write_text(
                json.dumps(holder, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass  # 观测信息写失败不影响锁的正确性

    def read_holder(self) -> dict | None:
        try:
            raw = self.info_path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            value = json.loads(raw)
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    def release(self) -> None:
        if self._fd is None:
            return
        self._unlock(self._fd)
        os.close(self._fd)
        self._fd = None
        for path in (self.path, self.info_path):
            try:
                path.unlink()
            except OSError:
                pass


def read_db_lease(session, key: str) -> dict | None:
    row = session.get(SystemState, key)
    if row is None:
        return None
    try:
        value = json.loads(row.value)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def claim_db_lease(
    session,
    *,
    key: str,
    holder: dict,
    lease_seconds: int,
    now: Callable[[], datetime] = utcnow,
) -> None:
    """跨主机租约认领（乐观 CAS）：持有未过期租约即拒绝，过期/空缺可接管。

    CAS 条件是"旧 value 精确匹配"：两个主机同时认领同一把过期租约时，
    数据库保证只有一个 UPDATE 生效，另一个必须拒绝。
    """
    moment = now()
    payload = dict(holder)
    payload["lease_until"] = _iso(moment + timedelta(seconds=int(lease_seconds)))
    new_value = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    row = session.get(SystemState, key)
    if row is None:
        existing = None
        session.add(SystemState(key=key, value=new_value))
        session.flush()
        return
    existing = row.value
    try:
        current = json.loads(existing)
    except (TypeError, ValueError):
        current = None
    if isinstance(current, dict):
        until = _parse_iso(str(current.get("lease_until") or ""))
        mine = current.get("holder_id") == holder.get("holder_id")
        if until is not None and until > moment and not mine:
            raise SingleActiveRefused(
                reason_code="single_active_lease_held",
                holder=current,
                next_action=(
                    f"租约 {current.get('lease_until')} 前不允许第二写实例；"
                    "确认持有者已下线则等租约过期后重试"
                ),
            )
    result = session.execute(
        update(SystemState)
        .where(SystemState.key == key, SystemState.value == existing)
        .values(value=new_value)
    )
    session.flush()
    if result.rowcount != 1:
        raise SingleActiveRefused(
            reason_code="single_active_lease_taken",
            holder=read_db_lease(session, key) or {},
            next_action="另一实例刚刚认领了租约；本实例不得作为写实例启动",
        )


def renew_db_lease(
    session, *, key: str, holder_id: str, lease_seconds: int,
    now: Callable[[], datetime] = utcnow,
) -> bool:
    moment = now()
    row = session.get(SystemState, key)
    if row is None:
        return False
    try:
        current = json.loads(row.value)
    except (TypeError, ValueError):
        return False
    if not isinstance(current, dict) or current.get("holder_id") != holder_id:
        return False
    until = _parse_iso(str(current.get("lease_until") or ""))
    if until is None or until <= moment:
        return False  # 租约已过期被接管，续约失败（调用方应退出写者角色）
    current["lease_until"] = _iso(moment + timedelta(seconds=int(lease_seconds)))
    new_value = json.dumps(current, ensure_ascii=False, sort_keys=True)
    result = session.execute(
        update(SystemState)
        .where(SystemState.key == key, SystemState.value == row.value)
        .values(value=new_value)
    )
    session.flush()
    return result.rowcount == 1


def release_db_lease(session, *, key: str, holder_id: str) -> bool:
    row = session.get(SystemState, key)
    if row is None:
        return False
    try:
        current = json.loads(row.value)
    except (TypeError, ValueError):
        return False
    if not isinstance(current, dict) or current.get("holder_id") != holder_id:
        return False
    session.delete(row)
    session.flush()
    return True


@dataclass
class SingleActiveGuard:
    """单活守卫：文件锁（同主机权威）+ 数据库租约（跨主机权威）。

    - `acquire()` 成功返回持有者信息；被拒抛 `SingleActiveRefused`。
    - `release()` 优雅退出时调用；崩溃时 OS 锁自动释放，DB 租约等过期后被接管。
    - clock 可注入，供离线演练"持有者崩溃→过期→接管"。
    """

    lock_path: Path
    lease_key: str
    session_factory: Callable[[], object] | None = None
    lease_seconds: int = DEFAULT_LEASE_SECONDS
    clock: Callable[[], datetime] = utcnow
    holder: dict = field(default_factory=dict)
    _file_lock: FileProcessLock | None = field(default=None, init=False, repr=False)
    _released: bool = field(default=False, init=False, repr=False)

    def _make_holder(self) -> dict:
        moment = self.clock()
        return {
            "holder_id": self.holder.get("holder_id") or f"{socket.gethostname()}:{os.getpid()}",
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started_at": _iso(moment),
            "role": self.holder.get("role", "writer"),
        }

    def acquire(self) -> dict:
        holder = self._make_holder()
        self._file_lock = FileProcessLock(self.lock_path)
        # 文件锁先行：同主机第二实例在这里就被拒（给出持有者 PID/启动时间）。
        self._file_lock.acquire(holder)
        if self.session_factory is not None:
            try:
                session = self.session_factory()
                try:
                    claim_db_lease(
                        session, key=self.lease_key, holder=holder,
                        lease_seconds=self.lease_seconds, now=self.clock,
                    )
                    session.commit()
                finally:
                    session.close()
            except SingleActiveRefused:
                self._file_lock.release()
                raise
        self.holder = holder
        self._released = False
        return holder

    def renew(self) -> bool:
        if self.session_factory is None or not self.holder:
            return True
        session = self.session_factory()
        try:
            ok = renew_db_lease(
                session, key=self.lease_key,
                holder_id=self.holder.get("holder_id", ""),
                lease_seconds=self.lease_seconds, now=self.clock,
            )
            session.commit()
        finally:
            session.close()
        return ok

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self.session_factory is not None and self.holder:
            session = self.session_factory()
            try:
                release_db_lease(
                    session, key=self.lease_key,
                    holder_id=self.holder.get("holder_id", ""),
                )
                session.commit()
            finally:
                session.close()
        if self._file_lock is not None:
            self._file_lock.release()
        self.holder = {}  # 释放后不再持有单活身份（观测口径：holder 空 = 未持锁）

    def db_lease_status(self) -> dict | None:
        if self.session_factory is None:
            return None
        session = self.session_factory()
        try:
            return read_db_lease(session, self.lease_key)
        finally:
            session.close()


def takeover_ready(lease: dict | None, *, now: Callable[[], datetime] = utcnow) -> bool:
    """观测函数：DB 租约是否已可接管（空缺或过期）。供面板/演练判断。"""
    if not lease:
        return True
    until = _parse_iso(str(lease.get("lease_until") or ""))
    return until is None or until <= now()


def wait_for_lease_expiry(
    lease_key: str, session_factory, *, timeout_seconds: float = 120.0,
    poll_interval: float = 0.05, clock: Callable[[], datetime] | None = None,
) -> bool:
    """等真实租约过期（接管演练用）；真实时钟等待，测试里用注入 clock 免等。"""
    clock = clock or (lambda: datetime.fromtimestamp(time.time()))
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        session = session_factory()
        try:
            if takeover_ready(read_db_lease(session, lease_key), now=clock):
                return True
        finally:
            session.close()
        time.sleep(poll_interval)
    return False
