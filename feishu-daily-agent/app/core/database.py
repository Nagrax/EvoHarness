"""SQLAlchemy 引擎与会话管理。

测试隔离是硬约束：`FEISHU_TEST_MODE=1` 时只接受内存库或系统临时目录下的 SQLite
文件，避免像原型那样在导入时把 `data/agent.db` 直接 `drop_all`。
"""
from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


def _is_test_safe_url(url: str) -> bool:
    if not url.startswith("sqlite"):
        return False
    if ":memory:" in url or url in {"sqlite://", "sqlite:///"}:
        return True
    path = url.split("sqlite:///", 1)[-1].split("?", 1)[0]
    if not path:
        return True
    candidate = Path(path)
    if not candidate.is_absolute():
        return False
    temp_root = Path(tempfile.gettempdir()).resolve()
    try:
        return candidate.resolve().is_relative_to(temp_root)
    except (OSError, ValueError):
        return False


def build_engine(url: str | None = None, *, test_mode: bool | None = None) -> Engine:
    settings = get_settings()
    target = url or settings.database_url
    is_test = settings.test_mode if test_mode is None else test_mode
    if is_test and not _is_test_safe_url(target):
        raise RuntimeError(
            "测试模式禁止连接业务数据库；请把 DATABASE_URL 指向系统临时目录或 :memory:。"
        )
    connect_args: dict = {}
    if target.startswith("sqlite"):
        # 实时事件处理跑在线程池里（4 worker），SQLite 默认 5s 忙等会直接抛
        # "database is locked"（2026-11-10 实机联调复现）。这里放宽忙等超时，
        # 并在连接建立后开启 WAL + 延长 busy_timeout，让并发写排队而不是报错。
        connect_args = {
            "check_same_thread": False,
            "timeout": 30,
        }
    engine = create_engine(
        target, connect_args=connect_args, pool_pre_ping=True, future=True
    )
    if target.startswith("sqlite"):
        from sqlalchemy import event as sa_event

        @sa_event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - 连接级钩子
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.execute("PRAGMA synchronous=NORMAL")
            finally:
                cursor.close()

    return engine


_settings = get_settings()
engine = build_engine(_settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def configure_engine(url: str, *, test_mode: bool | None = None) -> Engine:
    """测试与运维工具可显式切换引擎；返回新引擎。"""
    global engine, SessionLocal
    engine = build_engine(url, test_mode=test_mode)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    return engine


@contextlib.contextmanager
def session_scope() -> Iterator[Session]:
    """事务型会话上下文，自动提交/回滚/关闭。"""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Session:
    """获取裸会话（调用方负责关闭）。"""
    return SessionLocal()
