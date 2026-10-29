"""Loop 阶段状态机（OCTO优化交接文档.md 第 2 节改造二）。

--plan 长任务拆成 plan→execute→verify→deliver 四个显式阶段；每阶段独立会话文件
（项目级 .evoharness/sessions/<plan_id>/），manifest 是阶段机唯一落盘真相。

设计约束（见评审修订版设计）：
- 纯函数优先，可脱离 Agent 单测；零第三方依赖。
- manifest/段文件一律显式 encoding="utf-8" + 同目录临时文件 + os.replace 原子写
  （仓库已记录过默认 GBK 编码读写闭环 bug，README.md:139）。
- plan_id 沿用 session_id（uuid4().hex[:8]，agent.py _generate_plan_file_path 同款实践），
  目录名永不含用户文本。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

STAGES = ("plan", "execute", "verify", "deliver")
MANIFEST_FILENAME = "stage-manifest.json"


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_stage_record(name: str, *, close_reason: str = "", session_file: str = "") -> dict[str, Any]:
    return {
        "name": name,
        "entered_at": utc_now(),
        "exited_at": "",
        "status": "in_progress",
        "summary": "",
        "artifacts": [],
        "tokens": {"input": 0, "output": 0},
        "folds": 0,
        "close_reason": close_reason,
        "session_file": session_file,
        # 边界开启时记录的交接 payload（resume 恢复到该段开段状态用）；
        # 首段（plan）与预算强制续段没有交接，缺省 None。
        "handover": None,
        "advisory_mode": "",
    }


def new_manifest(plan_id: str, plan_dir: str, *, start_stage: str = "plan") -> dict[str, Any]:
    first = new_stage_record(start_stage, session_file=f"stage-01-{start_stage}.json")
    return {
        "version": 1,
        "plan_id": plan_id,
        "plan_dir": plan_dir,
        "current_stage": start_stage,
        "stage_index": 1,
        "stages": [first],
    }


def is_valid_transition(current: str, nxt: str) -> bool:
    """只允许沿固定序列前进一步；其余（回退/跳跃/同名）一律 False。"""
    if current not in STAGES or nxt not in STAGES:
        return False
    return STAGES.index(nxt) == STAGES.index(current) + 1


def current_record(manifest: dict[str, Any] | None) -> dict[str, Any] | None:
    if not manifest:
        return None
    stages = manifest.get("stages")
    if isinstance(stages, list) and stages:
        return stages[-1]
    return None


def segment_filename(manifest: dict[str, Any], name: str) -> str:
    return f"stage-{len(manifest['stages']) + 1:02d}-{name}.json"


def open_segment(manifest: dict[str, Any], name: str, *, close_reason: str = "", advisory_mode: str = "") -> dict[str, Any]:
    """开新段：阶段推进（advance）与同阶段强制续段（split）共用。"""
    rec = new_stage_record(name, close_reason=close_reason, session_file=segment_filename(manifest, name))
    rec["advisory_mode"] = advisory_mode
    manifest["stages"].append(rec)
    manifest["current_stage"] = name
    manifest["stage_index"] = len(manifest["stages"])
    return rec


def close_record(
    record: dict[str, Any],
    *,
    status: str,
    summary: str = "",
    close_reason: str = "",
    tokens: dict[str, int] | None = None,
    folds: int | None = None,
    artifacts: list[dict[str, str]] | None = None,
) -> None:
    record["status"] = status
    record["exited_at"] = utc_now()
    if summary:
        record["summary"] = summary
    if close_reason:
        record["close_reason"] = close_reason
    if tokens is not None:
        record["tokens"] = {"input": int(tokens.get("input", 0)), "output": int(tokens.get("output", 0))}
    if folds is not None:
        record["folds"] = int(folds)
    if artifacts is not None:
        record["artifacts"] = normalize_artifacts(artifacts)


def normalize_artifacts(artifacts: list[Any] | None) -> list[dict[str, str]]:
    """确定性归一：{"path","why"} 或裸字符串 → [{"path","why"}]，按 path 去重。"""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in artifacts or []:
        if isinstance(item, dict):
            path = str(item.get("path") or "").strip()
            why = str(item.get("why") or "").strip()
        else:
            path = str(item or "").strip()
            why = ""
        if path and path not in seen:
            seen.add(path)
            out.append({"path": path, "why": why})
    return out


def merge_artifacts(*groups: list[Any] | None) -> list[dict[str, str]]:
    merged: list[Any] = []
    for group in groups:
        merged.extend(group or [])
    return normalize_artifacts(merged)


def write_manifest(plan_dir: Path | str, manifest: dict[str, Any]) -> None:
    """原子写：同目录临时文件 + os.replace；显式 utf-8。"""
    directory = Path(plan_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / MANIFEST_FILENAME
    tmp = directory / (MANIFEST_FILENAME + ".tmp")
    tmp.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    os.replace(str(tmp), str(path))


def load_manifest(plan_dir: Path | str) -> dict[str, Any] | None:
    """读 manifest；不存在/写坏（截断 JSON、非 dict、缺 stages）一律降级为 None，
    由调用方显式回退（吸取 deployment_tiers.json 缺损按缺省读的教训，README 已知边界节）。"""
    path = Path(plan_dir) / MANIFEST_FILENAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(data, dict) and isinstance(data.get("stages"), list) and data["stages"]:
        return data
    return None
