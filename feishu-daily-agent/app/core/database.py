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
    connect_args = {"check_same_thread": False} if target.startswith("sqlite") else {}
    return create_engine(target, connect_args=connect_args, pool_pre_ping=True, future=True)


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
