"""路径与配置加载：所有集成模块共用。"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[1]
FEISHU_AGENT = REPO / "feishu-daily-agent"
INTEGRATION = REPO / "integration"


def _configured_path(name: str, default: Path) -> Path:
    value = Path(os.environ.get(name) or default).expanduser()
    return (value if value.is_absolute() else REPO / value).resolve()


DATA = _configured_path("EVOHARNESS_INTEGRATION_DATA", INTEGRATION / "data")
OUTBOX = DATA / "outbox"
INTAKE_LEDGER = DATA / "intake_ledger.json"
LOOP_LOCK = DATA / "iterate.lock"
REPORTS = DATA / "reports"

# 与上游脚本兼容（eval/loop_source_health 等历史产物路径约定）
UPSTREAM_LEDGER = _configured_path("EVOHARNESS_UPSTREAM_LEDGER",
    REPO / ".zcode/workflows/evoharness-feishu-delivery/runs/SELF-ITERATE")


def ensure_dirs() -> None:
    for d in (DATA, OUTBOX, REPORTS, UPSTREAM_LEDGER):
        d.mkdir(parents=True, exist_ok=True)


def load_feishu_env() -> dict:
    """读取 feishu-daily-agent/.env（不覆盖已存在的进程环境变量）。"""
    env = {}
    path = FEISHU_AGENT / ".env"
    if path.is_file():
        from dotenv import dotenv_values
        env = {key: value for key, value in dotenv_values(path, encoding="utf-8").items() if value is not None}
    merged = dict(env)
    merged.update({k: v for k, v in os.environ.items() if k.startswith("FEISHU_") or k in {
        "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "MATCHER_MODE", "DATABASE_URL"}})
    return merged


def venv_python() -> str:
    """优先用本仓库 .venv 的 python（保证依赖一致）。"""
    for cand in (REPO / ".venv" / "Scripts" / "python.exe", REPO / ".venv" / "bin" / "python"):
        if cand.is_file():
            return str(cand)
    return sys.executable


def server_url(env: dict | None = None) -> str:
    """Use the root server's PORT/HOST configuration; an explicit URL wins."""
    values = os.environ if env is None else env
    host = values.get("EVOHARNESS_HOST") or values.get("HOST") or "127.0.0.1"
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    value = values.get("EVOHARNESS_SERVER_URL") or f"http://{host}:{values.get('PORT') or '8800'}"
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("invalid_server_url: expected an HTTP URL without credentials/query/fragment")
    try:
        parsed.port
    except ValueError:
        raise ValueError("invalid_server_url: invalid port") from None
    return value.rstrip("/")


def feishu_database_path(env: dict | None = None) -> Path:
    """Resolve the same DATABASE_URL used by the Feishu service."""
    values = load_feishu_env() if env is None else env
    value = values.get("DATABASE_URL") or "sqlite:///data/agent.db"
    from evoharness.storage import sqlite_database_path
    return sqlite_database_path(value, base_dir=FEISHU_AGENT)


def configuration_summary() -> dict:
    """Only public configuration fields; never dump environment or credentials."""
    summary = {"repo": str(REPO), "data": str(DATA),
               "upstream_ledger": str(UPSTREAM_LEDGER), "errors": []}
    for name, resolve in (("server_url", server_url), ("database_path", feishu_database_path)):
        try:
            summary[name] = str(resolve())
        except ValueError as exc:
            summary["errors"].append({"field": name, "reason": str(exc)})
    summary["ok"] = not summary["errors"]
    return summary


if __name__ == "__main__":
    import json
    report = configuration_summary()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["ok"] else 1)


def feishu_owner_target(env: dict | None = None) -> tuple[str, str]:
    """(receive_id_type, receive_id)：chat_id > open_id > 邮箱。

    与 feishu-daily-agent 的 Settings.owner_target() 保持同一优先级：
    owner 的 open_id 在应用命名空间里可能未知，但 chat_id/邮箱仍可投递。
    """
    env = env or load_feishu_env()
    chat_id = (env.get("FEISHU_OWNER_CHAT_ID", "") or "").strip()
    if chat_id.startswith("oc_"):
        return "chat_id", chat_id
    owner = (env.get("FEISHU_OWNER_OPEN_ID", "") or "").strip()
    if owner.startswith("ou_"):
        return "open_id", owner
    email = (env.get("FEISHU_OWNER_EMAIL", "") or "").strip()
    if "@" in email:
        return "email", email
    return "open_id", ""


def feishu_live(env: dict | None = None) -> bool:
    """是否具备真实飞书发送条件：App ID/Secret 齐全且存在可用 owner 目标。"""
    env = env or load_feishu_env()
    app_id = env.get("FEISHU_APP_ID", "")
    secret = env.get("FEISHU_APP_SECRET", "")
    _type, target = feishu_owner_target(env)
    return bool(
        app_id.startswith("cli_")
        and secret
        and not secret.startswith("offline")
        and target
    )
