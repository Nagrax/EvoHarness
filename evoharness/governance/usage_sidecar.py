"""子进程真实用量上报（DE-15 路径 7）。

迭代器给每个子进程设置 ``EVOHARNESS_USAGE_SIDECAR`` 路径；子进程每次模型
调用后调用 ``write_usage``（累加），迭代器退出后读取并删除。缺失/坏文件
返回 None——调用方据此走保守结算，不把"没上报"当成 0。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

USAGE_SIDECAR_ENV = "EVOHARNESS_USAGE_SIDECAR"


def write_usage(tokens: int, calls: int = 1) -> None:
    path = (os.environ.get(USAGE_SIDECAR_ENV) or "").strip()
    if not path:
        return
    target = Path(path)
    current = read_usage(path) or {"tokens": 0, "calls": 0}
    payload = {"tokens": max(0, int(current.get("tokens", 0))) + max(0, int(tokens)),
               "calls": max(0, int(current.get("calls", 0))) + max(0, int(calls))}
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(temporary, target)


def read_usage(path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return {"tokens": max(0, int(data.get("tokens", 0))),
                "calls": max(0, int(data.get("calls", 0)))}
    except (TypeError, ValueError):
        return None
