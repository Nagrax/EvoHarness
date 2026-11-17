"""技能制品发布事务（DE-18，NATIVE-ARCHITECTURE-INTEGRATION §8 最小离线版）。

显式采用入口：champion/认证候选的最终 bundle 经本函数写入 active——
先比较当前 active hash/期望版本（CAS）→ 校验最终 bundle（内容 hash、producer_kind
允许 direct 写入）→ 复用现有 history JSONL 与审计（快照先行，旧内容不丢）→
原子写切换 → 刷新缓存 → 记录实际加载回执 {path, sha256, version}。

许可边界（三张许可分开，不折叠为一个 approved）：
- 知识事实发布 = 本函数（技能制品写入 active，发布后回 experimental，不自动晋级）；
- SOP 试用/认证 = 状态机（healthy）+ 部署分区（staging/core）各自的门，本函数不触碰；
- 对外自动发信权限 = 发信授权体系，本函数永不授予——返回值中
  messaging_authorization 恒为 "not_granted"，调用方不得把它当对外许可。

投影文件（producer-kind==projection / managed-by==experience.skill_sync）拒绝发布：
投影由源服务重建，直接写投影等于制造第二事实源（§4 单写者）。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

ALLOWED_PRODUCER_KINDS = {"", "direct"}
PUBLISH_LOCK_NAME = ".publish.lock"


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _canonical_bundle_hash(bundle: dict[str, Any]) -> str:
    """最终 bundle 的内容指纹：四个内容字段的规范化 sha256（与 skill_content_hash 同构）。"""
    canonical = {
        "name": str(bundle.get("name") or ""),
        "description": str(bundle.get("description") or ""),
        "when_to_use": str(bundle.get("when_to_use") or bundle.get("when-to-use") or ""),
        "instructions": str(bundle.get("instructions") or ""),
    }
    return hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def publish_skill_bundle(
    *,
    skill_name: str,
    bundle: dict[str, Any],
    expected_version: str | None = None,
    expected_active_sha256: str | None = None,
    actor: str = "release",
    lesson: str = "",
    reason: str = "",
) -> dict[str, Any]:
    """把认证/冠军候选的最终 bundle 显式发布为 active 技能制品。

    - expected_version（CAS）：提供时与当前 active meta 版本不一致 → 拒绝
      （error=source_changed），不覆盖期间发生的新用户决定（在线演化/投影重建）。
    - expected_active_sha256（CAS）：提供时与当前 active 文件字节 hash 不一致 → 同上。
    - bundle["sha256"]：提供时必须等于 bundle 规范化内容 hash（bundle_hash_mismatch），
      防止评测材料与发布内容不是同一份制品。
    - bundle["producer_kind"]：只允许 ""/"direct"；"projection" 等一律拒绝。
    - 成功后技能回 experimental（复用 evolve_skill_file 的 register_evolution_write
      语义），不自动晋级——晋级仍由既有 healthy+champion 双门 + 制品一致性门裁决。

    返回失败时 {"ok": False, "error": ...}；成功时 {"ok": True, "path", "sha256",
    "version", ...}——sha256 是写入后从磁盘重读的实际加载内容指纹。
    """
    from agents.skill_evolution import (
        evolve_skill_file,
        resolve_skill_file,
        skill_state_read_only,
    )
    from agents.frontmatter import parse_frontmatter

    if skill_state_read_only():
        return {"ok": False, "error": "holdout read-only mode: skill release disabled"}
    name = str(skill_name or "").strip()
    if not name:
        return {"ok": False, "error": "skill name is required"}
    bundle = bundle if isinstance(bundle, dict) else {}
    instructions = str(bundle.get("instructions") or "").strip()
    if not instructions:
        return {"ok": False, "error": "bundle_instructions_required"}

    # ── §4 单写者：投影只由源重建，发布入口拒绝认领投影制品 ──
    producer_kind = str(bundle.get("producer_kind") or "").strip()
    if producer_kind not in ALLOWED_PRODUCER_KINDS:
        return {
            "ok": False,
            "error": "producer_kind_not_directly_publishable",
            "producer_kind": producer_kind,
        }

    # ── bundle 自身完整性：bundle name 与目标一致、内容 hash 与认证材料一致 ──
    bundle_name = str(bundle.get("name") or "").strip()
    if bundle_name and bundle_name != name:
        return {"ok": False, "error": "bundle_name_mismatch", "bundle_name": bundle_name}
    bundle_hash = _canonical_bundle_hash({**bundle, "name": bundle_name or name})
    expected_bundle_hash = str(bundle.get("sha256") or "").strip()
    if expected_bundle_hash and expected_bundle_hash != bundle_hash:
        return {
            "ok": False,
            "error": "bundle_hash_mismatch",
            "expected_bundle_sha256": expected_bundle_hash,
            "actual_bundle_sha256": bundle_hash,
        }

    skill_file = resolve_skill_file(name, target="active")
    if not skill_file:
        return {"ok": False, "error": "skill_not_found", "skill": name}
    raw = skill_file.read_text(encoding="utf-8-sig")
    current_meta = parse_frontmatter(raw).meta
    if current_meta.get("producer-kind") == "projection" or current_meta.get("managed-by") == "experience.skill_sync":
        return {"ok": False, "error": "managed_projection_source_owned", "file": str(skill_file)}

    # ── §8：比较当前 active hash/授权版本（fail-closed CAS） ──
    current_version = str(current_meta.get("version", ""))
    current_sha256 = _file_sha256(skill_file)
    if expected_version is not None and current_version != str(expected_version):
        return {
            "ok": False,
            "error": "source_changed",
            "expected_version": str(expected_version),
            "current_version": current_version,
            "file": str(skill_file),
        }
    if expected_active_sha256 is not None and current_sha256 != str(expected_active_sha256):
        return {
            "ok": False,
            "error": "source_changed",
            "expected_active_sha256": str(expected_active_sha256),
            "current_active_sha256": current_sha256,
            "file": str(skill_file),
        }

    # ── 锁定目标（同目录 O_EXCL 锁，与 write_managed_projection 同款） ──
    lock = skill_file.parent / PUBLISH_LOCK_NAME
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return {"ok": False, "error": "release_busy", "file": str(skill_file)}
    os.close(descriptor)
    try:
        # ── 写版本与 history/provenance/部署意图：复用 evolve_skill_file
        # （快照先行落 history JSONL → CAS 复核 → 原子替换 → usage.jsonl evolve 行
        #   → register_evolution_write 回 experimental 并重置 tier 缓存） ──
        result = evolve_skill_file(
            skill_name=name,
            lesson=lesson or reason or "publish certified skill bundle",
            rationale=reason or "explicit bundle adoption via publish_skill_bundle",
            target="active",
            actor=actor,
            instructions=instructions,
            description=str(bundle.get("description") or ""),
            when_to_use=str(bundle.get("when_to_use") or bundle.get("when-to-use") or ""),
            expected_version=expected_version,
        )
        if not result.get("ok"):
            return {
                "ok": False,
                "error": str(result.get("error") or "publish_failed"),
                "detail": {k: v for k, v in result.items() if k != "ok"},
            }

        # ── 实际加载回执：写入后从磁盘重读，不信任内存字符串 ──
        receipt_sha256 = _file_sha256(skill_file)
        published_meta = parse_frontmatter(skill_file.read_text(encoding="utf-8-sig")).meta
        published_version = str(published_meta.get("version", ""))
        tier_now = None
        try:
            from agents.skill_tiers import get_tier

            tier_now = get_tier(name)
        except Exception:
            tier_now = ""
        receipt = {
            "path": str(skill_file),
            "sha256": receipt_sha256,
            "version": published_version,
        }
        # 发布回执进审计流（event=release；rebuild_tiers_from_audit 只认 tier_change，
        # 不会把发布行误当 tier 变更）与 usage 台账。
        audit_row = {
            "event": "release",
            "time": _utc_now(),
            "skill": name,
            "actor": actor,
            "reason": str(reason or ""),
            "bundle_sha256": bundle_hash,
            "previous_version": current_version,
            **receipt,
            "tier_after_release": tier_now,
            "messaging_authorization": "not_granted",
        }
        try:
            from agents.skill_evolution import _append_jsonl, get_evolution_dir
            from agents.skill_tiers import tier_audit_path

            _append_jsonl(tier_audit_path(), audit_row)
            _append_jsonl(get_evolution_dir() / "usage.jsonl", {**audit_row, "event": "release"})
        except Exception:
            pass
        try:
            from agents.skills import reset_skill_cache

            reset_skill_cache()
        except Exception:
            pass
        return {
            "ok": True,
            "skill": name,
            **receipt,
            "bundle_sha256": bundle_hash,
            "previous_version": current_version,
            "tier": tier_now,
            "released_at": audit_row["time"],
            # 许可边界显式回传：技能制品发布 ≠ 对外自动发信授权，也不等于认证完成。
            "messaging_authorization": "not_granted",
            "certification": "unchanged",
        }
    finally:
        try:
            lock.unlink(missing_ok=True)
        except OSError:
            pass
