"""跨入口统一配置解析（DE-02）：同一输入得到同一 ResolvedConfig。

- 数据库：显式参数优先，其次 ``EVOHARNESS_DATABASE_URL``；缺库给可读错误，
  不静默落到隐式路径。
- SQLite 相对路径按调用方给出的 base_dir 解析（与其他入口共用
  ``storage.normalize_database_url``）。
- 产物/输出根由数据库位置派生（文件型 SQLite 的同级 ``api-artifacts``），
  不再由各入口各自拼 ``data/``。
- ``describe()`` 只报告来源与非秘密字段，密码一律隐去。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from sqlalchemy.engine import make_url

from evoharness.storage import normalize_database_url, sqlite_database_path

DATABASE_ENV = "EVOHARNESS_DATABASE_URL"


@dataclass(frozen=True)
class ResolvedConfig:
    database_url: str
    database_source: str  # "arg" | "env"
    artifacts_dir: Path | None
    output_root: Path

    def describe(self) -> dict:
        try:
            safe_database = make_url(self.database_url).render_as_string(hide_password=True)
        except Exception:  # noqa: BLE001 — 诊断输出不得因 URL 形态崩
            safe_database = "<unparsed>"
        return {
            "database": safe_database,
            "databaseSource": self.database_source,
            "artifactsDir": str(self.artifacts_dir) if self.artifacts_dir else "",
            "outputRoot": str(self.output_root),
        }


def resolve_config(*, db: str = "", artifacts: str | Path | None = None,
                   output_root: str | Path | None = None,
                   env: Mapping[str, str] | None = None,
                   base_dir: str | Path | None = None) -> ResolvedConfig:
    env = env if env is not None else os.environ
    base = Path(base_dir) if base_dir is not None else Path.cwd()
    raw = str(db or "").strip()
    source = "arg"
    if not raw:
        raw = str(env.get(DATABASE_ENV) or "").strip()
        source = "env"
    if not raw:
        raise ValueError("database_not_configured")
    url = normalize_database_url(raw, base_dir=base)
    if artifacts is not None:
        artifacts_dir: Path | None = Path(artifacts)
    else:
        try:
            artifacts_dir = sqlite_database_path(url, base_dir=base).parent / "api-artifacts"
        except ValueError:
            artifacts_dir = None
    root = Path(output_root) if output_root is not None else (
        (artifacts_dir or base) / "runs")
    return ResolvedConfig(database_url=url, database_source=source,
                          artifacts_dir=artifacts_dir, output_root=root)
