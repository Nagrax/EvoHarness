"""DE-35 同制品封存认证、显式采用与原 tier 接线（搜索增强验收第七步）。

对应 SKILL-EVOLUTION-SEARCH §13（最终认证、发布与回滚）、§4（H 一次性语义）、
NATIVE-ARCHITECTURE-INTEGRATION §7（晋级接缝行）/§8（发布事务与认证边界）
与 tasks.json DE-35 的 5 条验收场景：

- **五状态显式分开**：``search_champion → certified_candidate →
  adopted_experimental → staging → core`` 各自独立报告（``certification_state``），
  不折叠为一个 approved；证书/审批/health/tier/发送授权各回答不同问题。
- **认证只是候选资格**：``certify_candidate`` 产出 ReleaseCertificate（封存 H
  验证、同协议对照 active、审计包装纳入渲染 hash、真实样本量），但**不产生任何
  tier 变更**；搜索报告（pool/认证材料）永远不进 ``apply_eval_promotions``
  （晋级门 ``promotion_gate`` fail-closed 拒绝）。
- **显式采用**：``adopt_certified_candidate`` 复用 DE-18 ``publish_skill_bundle``
  （expected_version / expected_active_sha256 CAS + bundle hash 门），采用后按
  ``register_evolution_write`` 语义**回 experimental**；messaging_authorization
  恒为 not_granted——身份与发送授权不由证书推导。
- **H 一次性**：``create_holdout_seal`` / ``record_holdout_attempt`` 每个封存
  标识只允许一次已执行尝试；失败即 ``spent``（不得依 H 择第二名、不得继续调整后
  冒称同一 H 独立）；旧曲线按污染登记显式退役（``retire_holdout_as_exposed``，
  复用 eval.evolution_experiment 登记，不重造第二套）。
- **读后写原子性（R08）**：冠军锁定、封存集创建、H 尝试登记的「读盘判定→
  原子写入」全程持有搜索命名空间互斥锁（``store.namespace_exclusive_lock``，
  OS 级文件锁，跨实例/跨进程有效、持有者崩溃即释放）——并发双写不可能同时
  通过判定；``record_holdout_attempt`` 以锁内重读的磁盘封存为准（内存副本
  与磁盘不一致即拒绝，一次性语义不依赖调用方副本的新鲜度）。
- **晋级门**：采用后由**相同制品/协议的真实 score**（线上 usage，绑定同一制品
  hash）走既有 DE-18 healthy+champion+active hash 门晋级 staging；core 仍纯人工。
  存在认证采用记录时，晋级成绩必须绑定评测协议指纹（同协议才可对账）。
- **回执分离**：``rollback_receipt`` 组合 tier 回退（rollback 只降级）与内容
  现状——内容未恢复时如实报告（content_restored=False），恢复另走
  ``skill_tiers.restore_content``，两条记录绝不混写。

真实模型能力实验（预算/审核真值/隔离执行环境）另行 blockedOn；本模块全部
判定离线可验证：封存/认证/采用/晋级门的合成输入即可驱动，固定协议指纹。
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

from ..skill_evolution import get_evolution_dir
from . import protocol as sp
from .contracts import SearchManifest, search_namespace
from .store import atomic_write_json, namespace_exclusive_lock

# ── 五状态（显式阶梯，不折叠；报告字段逐一区分） ─────────────────────────────

STATE_SEARCH_CHAMPION = "search_champion"
STATE_CERTIFIED_CANDIDATE = "certified_candidate"
STATE_ADOPTED_EXPERIMENTAL = "adopted_experimental"
STATE_STAGING = "staging"
STATE_CORE = "core"
STATE_LADDER = (
    STATE_SEARCH_CHAMPION,
    STATE_CERTIFIED_CANDIDATE,
    STATE_ADOPTED_EXPERIMENTAL,
    STATE_STAGING,
    STATE_CORE,
)

CERTIFICATION_ALGORITHM_VERSION = "de35/certification/v1"

CHAMPION_LOCK_NAME = "champion-lock.json"
HOLDOUT_SEAL_NAME = "holdout-seal.json"
CERTIFICATE_NAME = "certificate.json"
ADOPTION_RECORD_NAME = "adoption.json"
ADOPTION_INDEX_NAME = "adoption-index.json"
HOLDOUT_EXPOSURE_NAME = "holdout-exposure.json"

EVAL_SOURCE_SEARCH = "skill_search"
SEARCH_POOL_REPORT_FIELD = "search_pool_report"

_MESSAGING_NOT_GRANTED = "not_granted"


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json_dumps(value).encode("utf-8")
    ).hexdigest()


def json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _reject(error: str, reason: str, **detail: Any) -> dict[str, Any]:
    return {"ok": False, "error": error, "reason": reason, **detail}


def _read_json_file(path: Path) -> dict[str, Any] | None:
    import json

    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


# ── 索引：skill 名 → 搜索命名空间记录（认证接缝查找的唯一入口） ───────────────


def adoption_index_path() -> Path:
    return get_evolution_dir() / "search" / ADOPTION_INDEX_NAME


def adoption_index_lock(namespace: str | Path | None = None) -> Any:
    """索引读改写的互斥锁（同进程/跨进程防丢更新；复用命名空间锁实现）。"""
    base = Path(namespace) if namespace else adoption_index_path().parent
    return namespace_exclusive_lock(base, lock_name=".index.lock")


def _load_index() -> dict[str, Any]:
    data = _read_json_file(adoption_index_path())
    if not data or not isinstance(data.get("skills"), dict):
        return {"version": 1, "updatedAt": "", "skills": {}}
    return data


def _save_index(index: dict[str, Any]) -> None:
    index["version"] = 1
    index["updatedAt"] = _utc_now()
    atomic_write_json(adoption_index_path(), index)


def _record_index_entry(manifest: SearchManifest, **patch: Any) -> dict[str, Any]:
    with adoption_index_lock():
        index = _load_index()
        skills = index.setdefault("skills", {})
        entry = dict(skills.get(manifest.skill_name) or {})
        entry.setdefault("search_dir", str(search_namespace(manifest)))
        entry.setdefault("search_id", manifest.search_id)
        entry.update({k: v for k, v in patch.items() if v})
        skills[manifest.skill_name] = entry
        _save_index(index)
    return entry


def _record_index_entry_for_skill(skill_name: str, **patch: Any) -> None:
    """按 skill 名增量刷新索引（adopt 阶段无 manifest 对象时使用）。"""
    name = str(skill_name or "").strip()
    if not name:
        return
    with adoption_index_lock():
        index = _load_index()
        skills = index.setdefault("skills", {})
        entry = dict(skills.get(name) or {})
        entry.update({k: v for k, v in patch.items() if v})
        skills[name] = entry
        _save_index(index)


def _index_entry_for(skill_name: str) -> dict[str, Any]:
    entry = _load_index().get("skills", {}).get(str(skill_name or "").strip())
    return dict(entry) if isinstance(entry, dict) else {}


def load_search_record(manifest: SearchManifest, filename: str) -> dict[str, Any] | None:
    """读取搜索命名空间内的认证记录（champion-lock / holdout-seal / certificate / adoption）。"""
    return _read_json_file(search_namespace(manifest) / filename)


def load_record_for_skill(skill_name: str, filename: str) -> dict[str, Any] | None:
    entry = _index_entry_for(skill_name)
    search_dir = str(entry.get("search_dir") or "")
    if not search_dir:
        return None
    return _read_json_file(Path(search_dir) / filename)


# ── 第一步：复用原实验隔离后锁定唯一候选（量化分数矩阵 → 单一 candidate ref） ──


def lock_champion_candidate(
    *,
    manifest: SearchManifest,
    champion_selection: dict[str, Any],
    pool: Any = None,
) -> dict[str, Any]:
    """锁定搜索冠军为唯一候选 ref（写 champion-lock.json，只增不改）。

    - ``champion_selection`` 是 DE-33 ``select_search_champion`` 的输出；
      ``search_champion`` 即池候选 ID（variant ID）。锁定时绑定候选的完整身份：
      content_hash / protocol_hash / 分数向量 / 相对种子累计修改量 / 创建序号。
    - ``pool``（DE-33 CandidatePool）提供时从池中解析候选记录并核对其
      protocol_hash 与 manifest 冻结协议指纹一致——不同协议的分数不可入证书。
    - 重复锁定同一候选幂等返回；锁定另一候选一律拒绝（H 只评估预先锁定的
      一位候选及其预先规定对照，§13）。
    """
    if not isinstance(champion_selection, dict) or not champion_selection.get("ok"):
        return _reject(
            "champion_selection_invalid",
            "搜索冠军选择结果无效，无法锁定唯一候选",
            selection_error=str((champion_selection or {}).get("error") or ""),
        )
    champion_id = str(champion_selection.get("search_champion") or "").strip()
    if not champion_id:
        return _reject("champion_id_missing", "冠军候选 ID 为空，拒绝锁定")
    if not str(champion_selection.get("algorithm_version") or ""):
        return _reject("champion_algorithm_unversioned", "冠军选择算法未声明版本，拒绝锁定（比较器必须冻结）")

    record: dict[str, Any] = {}
    if pool is not None:
        record = dict(pool.get_candidate(champion_id) or {})
        if not record:
            return _reject("champion_not_in_pool", f"冠军候选 {champion_id} 不在候选池中")
        if not record.get("admitted") or record.get("unusable"):
            return _reject("champion_not_usable", "冠军候选已不可用（admitted=False 或被标记 unusable），不得锁定")
        record_fp = str(record.get("protocol_hash") or "")
        if record_fp and record_fp != manifest.eval_protocol_fingerprint:
            return _reject(
                "champion_protocol_mismatch",
                "候选分数绑定协议与 manifest 冻结协议不一致：跨协议分数不可入证书",
                record_protocol=record_fp,
                manifest_protocol=manifest.eval_protocol_fingerprint,
            )

    lock = {
        "lock_version": 1,
        "search_id": manifest.search_id,
        "skill_name": manifest.skill_name,
        "candidate_id": champion_id,
        "content_hash": str(record.get("content_hash") or ""),
        "protocol_fingerprint": manifest.eval_protocol_fingerprint,
        "score_vector": dict(record.get("score_vector") or {}),
        "cumulative_seed_diff": record.get("cumulative_seed_diff"),
        "created_sequence": int(record.get("created_sequence") or 0),
        "pool_digest": str(getattr(pool, "content_digest", lambda: "")() or ""),
        "champion_tie_key": dict(champion_selection.get("champion_tie_key") or {}),
        "algorithm_version": str(champion_selection.get("algorithm_version") or ""),
        "locked_at": _utc_now(),
    }
    namespace = search_namespace(manifest)
    with namespace_exclusive_lock(namespace):
        existing = _read_json_file(namespace / CHAMPION_LOCK_NAME)
        if existing:
            same = (
                str(existing.get("candidate_id") or "") == champion_id
                and str(existing.get("protocol_fingerprint") or "") == manifest.eval_protocol_fingerprint
                and (not lock["content_hash"] or str(existing.get("content_hash") or "") == lock["content_hash"])
            )
            if not same:
                return _reject(
                    "champion_already_locked",
                    f"manifest {manifest.search_id} 已锁定候选 {existing.get('candidate_id')}："
                    "H 只评估预先锁定的候选，不得改锁他人（换候选须新开搜索）",
                    locked_candidate=str(existing.get("candidate_id") or ""),
                    requested_candidate=champion_id,
                )
            result_lock, note = existing, "重复锁定同一候选：幂等返回既有 lock"
        else:
            atomic_write_json(namespace / CHAMPION_LOCK_NAME, lock)
            result_lock, note = lock, "锁定唯一候选 ref（认证/封存只针对该候选）"
    _record_index_entry(manifest, locked_candidate_id=champion_id)
    return {"ok": True, "lock": result_lock, "note": note}


# ── 第二步：独立封存集 H（一次性语义；S3 纪律） ──────────────────────────────


def create_holdout_seal(
    *,
    manifest: SearchManifest,
    locked: dict[str, Any],
    sealed_holdout: dict[str, Any],
    h_cases: list[dict[str, Any]],
    contamination: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """为锁定候选建立封存 H 引用（ref+sha256+case_ids），题目过来源纪律门。

    复用 DE-30 ``assign_protocol_roles`` 的来源纪律：score 来源/已观测 split/
    命中污染登记的题不得充当 H；H 必须引用独立封存集。每个封存标识只允许
    锁定一位候选（``locked_candidate_id``）。
    """
    candidate_id = str((locked or {}).get("candidate_id") or "").strip()
    if not candidate_id:
        return _reject("lock_required", "先 lock_champion_candidate 锁定唯一候选再建封存集")
    if str((locked or {}).get("protocol_fingerprint") or "") != manifest.eval_protocol_fingerprint:
        return _reject("lock_protocol_mismatch", "锁定候选与 manifest 协议不一致")
    ref = str((sealed_holdout or {}).get("ref") or "").strip()
    sha = str((sealed_holdout or {}).get("sha256") or "").strip()
    ids = sorted({str(x).strip() for x in ((sealed_holdout or {}).get("case_ids") or []) if str(x).strip()})
    if not ref or not sha or not ids:
        return _reject("sealed_ref_invalid", "封存集引用必须包含非空 ref + sha256 + case_ids（不接受裸题）")
    requested: list[dict[str, Any]] = []
    for case in h_cases or []:
        entry = dict(case)
        entry["protocol_role"] = sp.ROLE_H
        requested.append(entry)
    roles = sp.assign_protocol_roles(requested, sealed_holdout=sealed_holdout, contamination=contamination)
    if not roles.get("ok"):
        return _reject(
            "holdout_role_rejected",
            "H 题目来源纪律不满足（score 来源/已观测 split/污染登记/缺封存引用/近重复泄漏）",
            violations=list(roles.get("violations") or []),
        )
    seal = {
        "seal_version": 1,
        "search_id": manifest.search_id,
        "skill_name": manifest.skill_name,
        "seal_ref": ref,
        "seal_sha256": sha,
        "case_ids": ids,
        "locked_candidate_id": candidate_id,
        "protocol_fingerprint": manifest.eval_protocol_fingerprint,
        "attempts": [],
        "spent": False,
        "spent_reason": "",
        "retired_as": "",
        "created_at": _utc_now(),
    }
    namespace = search_namespace(manifest)
    with namespace_exclusive_lock(namespace):
        existing = _read_json_file(namespace / HOLDOUT_SEAL_NAME)
        if existing:
            same = (
                str(existing.get("seal_ref") or "") == ref
                and str(existing.get("seal_sha256") or "") == sha
                and [str(x) for x in (existing.get("case_ids") or [])] == ids
                and str(existing.get("locked_candidate_id") or "") == candidate_id
            )
            if same:
                return {"ok": True, "seal": existing, "note": "重复创建同一封存集：幂等返回既有 seal"}
            return _reject(
                "holdout_seal_conflict",
                "该搜索已存在不同封存集：同一 manifest 不得更换 H（重开需新搜索标识）",
                existing_ref=str(existing.get("seal_ref") or ""),
            )
        atomic_write_json(namespace / HOLDOUT_SEAL_NAME, seal)
    return {"ok": True, "seal": seal, "role_check": {"case_ids": ids}}


def load_holdout_seal(manifest: SearchManifest) -> dict[str, Any] | None:
    return load_search_record(manifest, HOLDOUT_SEAL_NAME)


def record_holdout_attempt(
    *,
    manifest: SearchManifest,
    locked: dict[str, Any],
    seal: dict[str, Any],
    verdicts: dict[str, dict[str, Any]],
    passed: bool | None = None,
    note: str = "",
) -> dict[str, Any]:
    """登记封存 H 的唯一一次已执行尝试（一次性；失败即终局）。

    - 逐题回执必须**恰好覆盖**封存 case_ids（缺题/多题拒绝——不填零、不编造）；
    - 尝试候选必须是锁定候选：他人成绩不得走同一个 H（不得依 H 择第二名）；
    - seal 已 spent（无论成败）→ 拒绝再跑：继续调整后仍称同一 H 独立被禁止，
      重开需要新的独立封存标识；
    - 失败 → ``spent=True`` + ``holdout_failed_no_repick``，本轮不发布；
    - 一次性判定以**锁内重读的磁盘封存**为准（读盘→判定→写盘同持命名空间互斥锁）：
      内存副本与磁盘不一致即拒绝，两个并发登记恰好一个成功（R08）。
    """
    candidate_id = str((locked or {}).get("candidate_id") or "").strip()
    if not candidate_id:
        return _reject("lock_required", "缺少锁定候选，无法登记 H 尝试")
    if not isinstance(seal, dict) or not seal.get("seal_ref"):
        return _reject("seal_required", "封存记录缺失：先 create_holdout_seal")
    namespace = search_namespace(manifest)
    with namespace_exclusive_lock(namespace):
        disk_seal = _read_json_file(namespace / HOLDOUT_SEAL_NAME)
        if disk_seal is None:
            return _reject(
                "holdout_seal_missing",
                "磁盘封存记录缺失：H 只认落盘事实，先 create_holdout_seal 再登记尝试",
            )
        for field in ("seal_ref", "seal_sha256", "locked_candidate_id"):
            if str(disk_seal.get(field) or "") != str(seal.get(field) or ""):
                return _reject(
                    "holdout_seal_stale",
                    f"传入封存副本与磁盘不一致（{field}）：重新 load 后再登记，不得覆盖磁盘事实",
                )
        if [str(x) for x in (disk_seal.get("case_ids") or [])] != [str(x) for x in (seal.get("case_ids") or [])]:
            return _reject("holdout_seal_stale", "传入封存副本 case_ids 与磁盘不一致：重新 load 后再登记")
        if bool(disk_seal.get("spent")) or disk_seal.get("attempts"):
            return _reject(
                "holdout_seal_spent",
                f"封存集 {disk_seal.get('seal_ref')} 已消耗（spent={disk_seal.get('spent')}，已登记尝试 "
                f"{len(disk_seal.get('attempts') or [])} 次）：H 只执行一次，重开需新的独立封存标识",
                spent_reason=str(disk_seal.get("spent_reason") or ""),
            )
        if str(disk_seal.get("locked_candidate_id") or "") != candidate_id:
            return _reject(
                "holdout_candidate_mismatch",
                f"封存集锁定候选为 {disk_seal.get('locked_candidate_id')}：不得用同一个 H 评估其他候选"
                "（H 失败后依 H 择第二名被禁止）",
            )
        expected_ids = [str(x) for x in (disk_seal.get("case_ids") or [])]
        got_ids = sorted({str(k).strip() for k in (verdicts or {}) if str(k).strip()})
        if got_ids != sorted(expected_ids):
            return _reject(
                "holdout_verdict_incomplete",
                "逐题回执必须恰好覆盖封存 case_ids（缺题/多题拒绝，不填零不编造）",
                expected=sorted(expected_ids),
                got=got_ids,
            )
        normalized: dict[str, dict[str, Any]] = {}
        all_passed = True
        for case_id in expected_ids:
            verdict = dict(verdicts.get(case_id) or {})
            score = verdict.get("score")
            case_passed = bool(verdict.get("passed"))
            if score is not None:
                try:
                    score = float(score)
                except Exception:
                    return _reject("holdout_score_invalid", f"Case {case_id} 分数不可解析：拒绝编造终态")
            if not case_passed:
                all_passed = False
            normalized[case_id] = {
                "passed": case_passed,
                "score": score,
                "terminal_status": str(verdict.get("terminal_status") or ("pass" if case_passed else "fail")),
            }
        if passed is not None:
            all_passed = bool(passed)
        attempt = {
            "attempt_id": f"h-{_canonical_sha256({'seal': disk_seal.get('seal_ref'), 'candidate': candidate_id})[:12]}",
            "candidate_id": candidate_id,
            "content_hash": str(locked.get("content_hash") or ""),
            "verdicts": normalized,
            "passed": all_passed,
            "note": str(note or ""),
            "attempted_at": _utc_now(),
        }
        disk_seal["attempts"] = [attempt]
        disk_seal["spent"] = True
        disk_seal["spent_reason"] = "holdout_executed_once" if all_passed else "holdout_failed_no_repick"
        atomic_write_json(namespace / HOLDOUT_SEAL_NAME, disk_seal)
    # 调用方副本同步为终态（返回对象沿用既有形状；磁盘为唯一事实源）
    seal.update({"attempts": [attempt], "spent": True, "spent_reason": disk_seal["spent_reason"]})
    return {
        "ok": True,
        "attempt": attempt,
        "seal": seal,
        "passed": all_passed,
        "note": (
            "H 通过并已消耗（one-shot）：证书可引用本次回执"
            if all_passed
            else "H 失败：本轮不发布；不得依 H 择第二名或继续调整后冒称同一 H 独立"
        ),
    }


def retire_holdout_as_exposed(
    *,
    seal: dict[str, Any],
    path: str | Path | None = None,
    reason: str = "",
) -> dict[str, Any]:
    """H 已消耗/已读曲线按已暴露登记（复用原污染登记，不重造第二套）。

    原 H 退役为开发/selection 数据；后续任何候选不得再把同一批题称为
    从未观测过的独立封存 H。登记只合并不删除旧证据。
    """
    case_ids = [str(x) for x in (seal.get("case_ids") or []) if str(x).strip()]
    if not case_ids:
        return _reject("holdout_case_ids_missing", "封存记录缺 case_ids，无法登记暴露")
    target = Path(path) if path else (get_evolution_dir() / "search" / HOLDOUT_EXPOSURE_NAME)
    registry = sp.register_observed_cases(
        case_ids,
        reason=reason
        or "DE-35 封存 H 已执行/已暴露：原 H 退役为开发/selection 数据，不得再冒称独立封存集",
        evidence=[str(seal.get("seal_ref") or ""), f"candidate:{seal.get('locked_candidate_id') or ''}"],
        path=target,
    )
    seal["retired_as"] = "selection_or_dev"
    return {"ok": True, "registry": registry, "path": str(target), "retired_as": "selection_or_dev"}


# ── 第三步：封存认证（候选资格；不晋级） ─────────────────────────────────────


def certify_candidate(
    *,
    manifest: SearchManifest,
    locked: dict[str, Any],
    packaged: dict[str, Any],
    holdout_seal: dict[str, Any] | None = None,
    active_comparison: dict[str, Any] | None = None,
    s_case_ids: list[str] | None = None,
    v_case_ids: list[str] | None = None,
    reviewer: str = "",
    release_gate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """同制品封存认证：锁定唯一候选 + 最终渲染制品（含审计包装）+ 同协议对照。

    拒绝面（全部 fail-closed，零副作用）：
    - 源漂移：``manifest.verify_integrity`` 发现 SKILL.md/辅助脚本变化 →
      ``blocked_source_changed``（S4：搜索期间 active 被更新不得覆盖）；
    - 锁定协议不一致 / 冠军未锁定；
    - 审计包装未纳入渲染 hash（测 A 发 B 的包装偏差被拒绝）；
    - H 已执行且失败 → ``holdout_failed``（本轮不发布，不择第二名）；
    - 对照 active 的比较跨协议 → ``active_comparison_protocol_mismatch``。

    通过时产出 ReleaseCertificate（certified_candidate）：只是**候选资格**，
    不含任何 tier 变更；证据不足项如实列入 ``publish_blockers``（不冒称已具备
    发布条件）。证书样本量只来自真实计数（S/V/H 结构与封存回执），线上
    usage 恒为 0——证书不填造真实样本量。
    """
    # DE-13：搜索报告 release_gate 未通过时不得进入认证（失败门不可被认证绕过）。
    if release_gate is not None and not bool((release_gate or {}).get("passed")
                                             or (release_gate or {}).get("ok")):
        return _reject("release_gate_not_passed",
                       "搜索报告 release_gate 未通过：认证拒绝（先修复退化/门槛）",
                       violations=list((release_gate or {}).get("violations") or []))
    integrity = manifest.verify_integrity()
    if not integrity.get("ok"):
        return _reject(
            str(integrity.get("reason") or "bundle_integrity_failed"),
            "冻结种子制品在认证时已漂移：认证拒绝（S4：期间被原维护更新不得覆盖，"
            "重开新搜索而非 rebase）",
            drifted=list(integrity.get("drifted") or []),
        )
    candidate_id = str((locked or {}).get("candidate_id") or "").strip()
    if not candidate_id:
        return _reject("lock_required", "认证前必须锁定唯一候选（search_champion）")
    if str((locked or {}).get("protocol_fingerprint") or "") != manifest.eval_protocol_fingerprint:
        return _reject("lock_protocol_mismatch", "锁定候选与 manifest 冻结协议不一致")
    if not isinstance(packaged, dict) or not packaged.get("ok"):
        return _reject(
            "final_artifact_missing",
            "缺少受信包装器最终渲染制品：认证只针对最终交付形态（先包装、后评测）",
        )
    final_content_hash = str(packaged.get("final_content_hash") or "")
    final_bundle_sha = str(packaged.get("final_bundle_sha256") or "")
    final_text_sha = str(packaged.get("final_text_sha256") or "")
    release_bundle_sha = str(packaged.get("release_bundle_sha256") or "")
    if not all([final_content_hash, final_bundle_sha, final_text_sha, release_bundle_sha]):
        return _reject("final_artifact_hashes_missing", "最终制品 hash 不完整（final_content/bundle/text/release）")
    # 渲染后 hash 记录：审计包装必须已纳入最终 bundle hash（复算校验，防测 A 发 B）
    from evoharness.knowledge.release import _canonical_bundle_hash

    recomputed = _canonical_bundle_hash(
        {
            "name": str(packaged.get("bundle_name") or ""),
            "description": str(packaged.get("bundle_description") or ""),
            "when_to_use": str(packaged.get("bundle_when_to_use") or ""),
            "instructions": str(packaged.get("final_body") or ""),
        }
    )
    if recomputed != final_bundle_sha:
        return _reject(
            "wrapper_not_reflected_in_hash",
            "最终渲染 hash 复算不符：审计包装未纳入被认证制品（不得测原始子文档发带 Notes 的不同制品）",
            recomputed=recomputed,
            declared=final_bundle_sha,
        )
    # 同制品一致门：最终包装解包后的裸候选（release_bundle_sha256 与
    # skill_content_hash 同构）必须就是锁定的那一位候选制品（防测 A 认 B）。
    locked_hash = str((locked or {}).get("content_hash") or "")
    if locked_hash and locked_hash != release_bundle_sha:
        return _reject(
            "final_artifact_not_locked_candidate",
            "最终包装对应的裸候选与锁定候选制品不一致：认证只针对锁定候选的最终形态（防测 A 发 B）",
            locked_content_hash=locked_hash,
            release_bundle_sha256=release_bundle_sha,
        )

    holdout_outcome = "not_run"
    holdout_detail: dict[str, Any] = {}
    if holdout_seal is not None:
        if str(holdout_seal.get("protocol_fingerprint") or "") != manifest.eval_protocol_fingerprint:
            return _reject("holdout_protocol_mismatch", "封存集协议与 manifest 不一致")
        attempts = [a for a in (holdout_seal.get("attempts") or []) if isinstance(a, dict)]
        if attempts:
            attempt = attempts[-1]
            if str(attempt.get("candidate_id") or "") != candidate_id:
                return _reject(
                    "holdout_candidate_mismatch",
                    "封存尝试候选与锁定候选不一致：不得依 H 择第二名",
                )
            holdout_outcome = "passed" if attempt.get("passed") else "failed"
            holdout_detail = {
                "attempt_id": str(attempt.get("attempt_id") or ""),
                "case_ids": [str(x) for x in (holdout_seal.get("case_ids") or [])],
                "seal_ref": str(holdout_seal.get("seal_ref") or ""),
                "seal_sha256": str(holdout_seal.get("seal_sha256") or ""),
            }
            if holdout_outcome == "failed":
                return _reject(
                    "holdout_failed",
                    "封存 H 失败：本轮不发布；不得依 H 择第二名或继续调整后冒称同一 H 独立"
                    "（旧曲线按已暴露登记退役）",
                    holdout=holdout_detail,
                )
        elif bool(holdout_seal.get("spent")):
            return _reject("holdout_seal_spent", "封存集已消耗但无登记尝试：记录不完整，拒绝认证")

    comparison: dict[str, Any] = {}
    comparison_blockers: list[str] = []
    if isinstance(active_comparison, dict) and active_comparison:
        fp = str(active_comparison.get("eval_protocol_fingerprint") or "")
        if fp != manifest.eval_protocol_fingerprint:
            return _reject(
                "active_comparison_protocol_mismatch",
                "与 active 的对照比较跨协议（旧冠军不同日期/模型/样本的历史均分不得当对照）",
                comparison_protocol=fp,
                manifest_protocol=manifest.eval_protocol_fingerprint,
            )
        active_hash = str(active_comparison.get("active_snapshot_hash") or "")
        if not active_hash:
            return _reject("active_comparison_unbound", "同协议对照缺少 active 制品 hash（不绑制品的比较不可用）")
        comparison = {
            "same_protocol": True,
            "eval_protocol_fingerprint": fp,
            "active_snapshot_hash": active_hash,
            "candidate_average_score": float(active_comparison.get("candidate_average_score") or 0.0),
            "active_average_score": float(active_comparison.get("active_average_score") or 0.0),
            "source": str(active_comparison.get("source") or ""),
        }
    else:
        comparison_blockers.append("no_same_protocol_active_comparison")

    if holdout_outcome == "not_run":
        comparison_blockers.append("no_sealed_holdout_verification")
    promotion_grade = (
        sp.PROMOTION_GRADE_SEALED
        if holdout_outcome == "passed"
        else sp.PROMOTION_GRADE_SELECTION if (v_case_ids or []) else sp.PROMOTION_GRADE_DEV_ONLY
    )

    # 样本量：只来自真实计数（S 分数向量长度 / V 题 / H 封存回执），线上 usage 恒 0
    score_vector = dict((locked or {}).get("score_vector") or {})
    s_count = len([c for c in (s_case_ids or []) if str(c).strip()]) or len(score_vector)
    h_count = len(holdout_detail.get("case_ids") or []) if holdout_outcome != "not_run" else 0
    sample_counts = {
        "S": int(s_count),
        "V": len([c for c in (v_case_ids or []) if str(c).strip()]),
        "H": int(h_count),
        "online_usage": 0,
        "counts_verified": True,
        "note": "样本量来自真实输入结构与封存回执；证书不填造线上 usage 样本量（S2）",
    }

    certificate = {
        "certificate_version": 1,
        "certificate_id": "cert-"
        + _canonical_sha256(
            {
                "search_id": manifest.search_id,
                "candidate_id": candidate_id,
                "final_bundle_sha256": final_bundle_sha,
                "protocol": manifest.eval_protocol_fingerprint,
            }
        )[:16],
        "state": STATE_CERTIFIED_CANDIDATE,
        "search_id": manifest.search_id,
        "skill_name": manifest.skill_name,
        "candidate_id": candidate_id,
        "locked": {
            "content_hash": str((locked or {}).get("content_hash") or ""),
            "algorithm_version": str((locked or {}).get("algorithm_version") or ""),
            "locked_at": str((locked or {}).get("locked_at") or ""),
        },
        "artifact": {
            "final_content_hash": final_content_hash,
            "final_bundle_sha256": final_bundle_sha,
            "final_text_sha256": final_text_sha,
            "release_bundle_sha256": release_bundle_sha,
            "wrapper": {
                "lesson": str(packaged.get("wrapper_lesson") or ""),
                "rationale": str(packaged.get("wrapper_rationale") or ""),
                "body_chars": int(packaged.get("wrapper_diff_chars") or 0),
                "included_in_render_hash": True,
            },
        },
        "protocol": {
            "eval_protocol_fingerprint": manifest.eval_protocol_fingerprint,
            "promotion_grade": promotion_grade,
        },
        "holdout": {"outcome": holdout_outcome, **holdout_detail},
        "active_comparison": comparison,
        "sample_counts": sample_counts,
        "messaging_authorization": _MESSAGING_NOT_GRANTED,
        "certification_only": True,
        "promotion_decision": "none — 认证只是候选资格，不直接晋级（显式采用 + 同制品真实 score 才走 tier 门）",
        "reviewer": str(reviewer or ""),
        "issued_at": _utc_now(),
        "algorithm_version": CERTIFICATION_ALGORITHM_VERSION,
        "cas": {
            "expected_version": str(manifest.expected_version or ""),
            "expected_active_sha256": str(manifest.expected_active_sha256 or ""),
        },
        "publish_blockers": list(comparison_blockers),
        "release_gate": dict(release_gate or {}) if release_gate is not None else {},
        "skill_dir": str(manifest.skill_dir or ""),
    }
    namespace = search_namespace(manifest)
    atomic_write_json(namespace / CERTIFICATE_NAME, certificate)
    _record_index_entry(manifest, certificate_id=certificate["certificate_id"])
    return {
        "ok": True,
        "certificate": certificate,
        "state": STATE_CERTIFIED_CANDIDATE,
        "publish_blockers": comparison_blockers,
        "publish_blockers_note": (
            "存在未满足条件：结果是「不具备完整发布证据」，不是「无收益/无问题」（§13）"
            if comparison_blockers
            else ""
        ),
        "tier_changed": False,
        "note": "认证不晋级：候选资格证书，显式采用后回 experimental，同制品真实 score 才走 tier 门",
    }


# ── 第四步：显式采用（复用 DE-18 publish_skill_bundle；采用后回 experimental） ─


def adopt_certified_candidate(
    *,
    certificate: dict[str, Any],
    packaged: dict[str, Any] | None = None,
    bundle: dict[str, Any] | None = None,
    expected_version: str | None = None,
    expected_active_sha256: str | None = None,
    actor: str = "release",
    reason: str = "",
) -> dict[str, Any]:
    """显式采用认证候选：唯一发布入口是 DE-18 ``publish_skill_bundle``。

    - 采用必须绑定批准时的制品：bundle["sha256"] == 证书 release_bundle_sha256
      （packaged 路径经 mutator.build_release_bundle 构造，天然一致）；
    - CAS：缺省取证书冻结的 expected_version / expected_active_sha256——
      搜索期间 active 被原维护更新或投影重建 → ``source_changed`` 拒绝，
      绝不覆盖新用户决定（S4）；
    - 采用后按 ``register_evolution_write`` 语义**回 experimental**
      （adopted_experimental 状态、tier=experimental），不自动晋级；
    - messaging_authorization 恒 not_granted（DE-18 release 语义，不由证书推导）。
    """
    if not isinstance(certificate, dict) or not certificate.get("certificate_id"):
        return _reject("certificate_required", "显式采用需要有效证书（先 certify_candidate）")
    gate = certificate.get("release_gate") if isinstance(certificate, dict) else None
    if isinstance(gate, dict) and gate and not (gate.get("passed") or gate.get("ok")):
        return _reject("release_gate_not_passed",
                       "证书绑定的 release_gate 未通过：拒绝采用（不发布退化制品）",
                       violations=list(gate.get("violations") or []))
    import os as _os
    if _os.environ.get("EVOHARNESS_REQUIRE_RELEASE_GATE", "").strip().lower()             in {"1", "true", "on", "yes"} and not (isinstance(gate, dict)
                                                   and (gate.get("passed") or gate.get("ok"))):
        return _reject("release_gate_required",
                       "组织模式要求证书携带通过的 release_gate（EVOHARNESS_REQUIRE_RELEASE_GATE=1）")
    if str(certificate.get("state") or "") != STATE_CERTIFIED_CANDIDATE:
        return _reject(
            "certificate_state_invalid",
            f"证书状态为 {certificate.get('state')}：只有 certified_candidate 可显式采用",
        )
    skill_name = str(certificate.get("skill_name") or "").strip()
    if not skill_name:
        return _reject("skill_name_missing", "证书缺 skill_name")
    artifact = certificate.get("artifact") if isinstance(certificate.get("artifact"), dict) else {}
    release_bundle_sha = str(artifact.get("release_bundle_sha256") or "")

    publish_kwargs_extra: dict[str, Any] = {}
    if packaged is not None:
        if bundle is not None:
            return _reject("bundle_source_ambiguous", "packaged 与 bundle 二选一（采用绑定唯一制品来源）")
        from .mutator import build_release_bundle

        built = build_release_bundle(packaged)
        if not built.get("ok"):
            return _reject("release_bundle_build_failed", str(built.get("reason") or "无法从最终包装构造发布 bundle"))
        bundle = built["bundle"]
        publish_kwargs_extra = dict(built.get("publish_kwargs") or {})
    if not isinstance(bundle, dict) or not bundle:
        return _reject("bundle_required", "显式采用需要发布 bundle（packaged 或 bundle 二选一）")
    declared_sha = str(bundle.get("sha256") or "")
    if release_bundle_sha and declared_sha and declared_sha != release_bundle_sha:
        return _reject(
            "adoption_bundle_mismatch",
            "发布 bundle 与证书批准制品不一致：采用必须绑定批准时的 bundle（防测 A 发 B）",
            certificate_bundle=release_bundle_sha,
            bundle_sha256=declared_sha,
        )
    cas = certificate.get("cas") if isinstance(certificate.get("cas"), dict) else {}
    if expected_version is None:
        expected_version = str(cas.get("expected_version") or "") or None
    if expected_active_sha256 is None:
        expected_active_sha256 = str(cas.get("expected_active_sha256") or "") or None

    from evoharness.knowledge.release import publish_skill_bundle

    published = publish_skill_bundle(
        skill_name=skill_name,
        bundle=bundle,
        expected_version=expected_version,
        expected_active_sha256=expected_active_sha256,
        actor=actor,
        lesson=str(publish_kwargs_extra.get("lesson") or (artifact.get("wrapper") or {}).get("lesson") or reason or "adopt certified search candidate"),
        reason=reason or publish_kwargs_extra.get("reason") or "explicit adoption of certified search candidate (DE-35)",
    )
    if not published.get("ok"):
        error = str(published.get("error") or "publish_failed")
        note = ""
        if error == "source_changed":
            note = (
                "搜索期间 active 被原维护更新或投影重建（CAS source_changed）：不得覆盖新用户决定，"
                "采用被拒（S4）；搜索结果可保留但须重开搜索/重新认证"
            )
        elif error == "release_busy":
            note = "另一发布事务持有锁（release_busy）：稍后重试，不并发覆盖"
        return {"ok": False, "error": error, "note": note, "detail": {k: v for k, v in published.items() if k != "ok"}}

    # 发布后实际加载校验：磁盘上的内容 hash 必须等于证书批准的最终制品
    from ..skill_tiers import _current_active_content_hash

    delivered_hash = _current_active_content_hash(skill_name)
    certified_hash = str(artifact.get("final_content_hash") or "")
    verified = bool(certified_hash) and delivered_hash == certified_hash
    if certified_hash and not verified:
        return {
            "ok": False,
            "error": "post_publish_artifact_mismatch",
            "note": "发布后实际制品与认证材料不一致：需要 rollback+restore_content 恢复，不记录成功采用",
            "certified_content_hash": certified_hash,
            "delivered_content_hash": delivered_hash,
        }

    adoption_id = "adopt-" + _canonical_sha256(
        {"certificate_id": certificate.get("certificate_id"), "sha256": published.get("sha256"), "at": published.get("released_at")}
    )[:16]
    record = {
        "adoption_version": 1,
        "adoption_id": adoption_id,
        "state": STATE_ADOPTED_EXPERIMENTAL,
        "skill_name": skill_name,
        "search_id": str(certificate.get("search_id") or ""),
        "candidate_id": str(certificate.get("candidate_id") or ""),
        "certificate_id": str(certificate.get("certificate_id") or ""),
        "released": {
            "path": str(published.get("path") or ""),
            "sha256": str(published.get("sha256") or ""),
            "version": str(published.get("version") or ""),
            "bundle_sha256": str(published.get("bundle_sha256") or ""),
        },
        "final_content_hash_verified": verified,
        "certified_final_content_hash": certified_hash,
        "tier_after": str(published.get("tier") or ""),
        "messaging_authorization": str(published.get("messaging_authorization") or _MESSAGING_NOT_GRANTED),
        "publish_blockers": [str(b) for b in (certificate.get("publish_blockers") or [])] if isinstance(certificate.get("publish_blockers"), list) else [],
        "adopted_with_blockers": bool(certificate.get("publish_blockers")),
        "actor": str(actor or ""),
        "reason": str(reason or ""),
        "adopted_at": str(published.get("released_at") or _utc_now()),
        "note": "采用即回 experimental（register_evolution_write 语义）；缺真实使用证据保持 experimental（S2）",
    }
    search_dir = _index_entry_for(skill_name).get("search_dir")
    if search_dir:
        atomic_write_json(Path(str(search_dir)) / ADOPTION_RECORD_NAME, record)
    _record_index_entry_for_skill(skill_name, adoption_id=adoption_id)
    # 审计行（informational；rebuild_tiers_from_audit 只认 tier_change，不会误读）
    try:
        from ..skill_evolution import _append_jsonl
        from ..skill_tiers import tier_audit_path

        _append_jsonl(
            tier_audit_path(),
            {
                "event": "search_adoption",
                "time": _utc_now(),
                "skill": skill_name,
                "adoption_id": adoption_id,
                "certificate_id": str(certificate.get("certificate_id") or ""),
                "candidate_id": str(certificate.get("candidate_id") or ""),
                "released_version": record["released"]["version"],
                "released_sha256": record["released"]["sha256"],
                "tier_after": record["tier_after"],
                "messaging_authorization": record["messaging_authorization"],
                "state": STATE_ADOPTED_EXPERIMENTAL,
                "certification_only": True,
                "actor": actor,
            },
        )
    except Exception:
        pass
    return {
        "ok": True,
        "state": STATE_ADOPTED_EXPERIMENTAL,
        "adoption": record,
        "tier": record["tier_after"],
        "messaging_authorization": record["messaging_authorization"],
        "certificate": "unchanged",
        "note": "显式采用完成：技能回 experimental；晋级仍由同制品/协议真实 score 走既有 healthy+champion+hash 门",
    }


# ── 晋级接缝：搜索报告不直接晋级；采用记录存在时成绩必须绑定协议 ─────────────


def promotion_gate(item: dict[str, Any], skill_name: str) -> dict[str, Any]:
    """DE-35 晋级预检（``skill_tiers.apply_eval_promotions`` 逐 skill 调用）。

    - 搜索报告（``eval_source=skill_search`` / ``search_pool_report``）一律拒绝：
      搜索池/封存/认证材料不是真实线上成绩，不得触发 tier 晋级；
    - 存在认证**采用记录**时：晋级成绩必须绑定评测协议指纹（eval_protocol_fingerprint），
      否则拒绝（同协议才可与认证记录对账；同制品门由既有 active hash 门承担）；
    - 无认证记录的原生技能 → ``{"ok": True}`` 零变化（S5：不因搜索认证要求全局禁用）。
    """
    artifacts = item.get("artifacts") if isinstance(item.get("artifacts"), dict) else {}
    source = str(artifacts.get("eval_source") or "")
    if source == EVAL_SOURCE_SEARCH or artifacts.get(SEARCH_POOL_REPORT_FIELD):
        return {
            "ok": False,
            "code": "search_report_not_promotion_evidence",
            "why": "搜索报告不直接晋级：认证/池/封存结果只是候选资格，真实线上 score 才可走 tier 门（DE-35）",
        }
    adoption = load_record_for_skill(skill_name, ADOPTION_RECORD_NAME)
    if adoption is None:
        return {"ok": True, "note": "no_search_adoption_record"}
    certificate_id = str(adoption.get("certificate_id") or "")
    report_fp = str(artifacts.get("eval_protocol_fingerprint") or "")
    if not report_fp:
        return {
            "ok": False,
            "code": "protocol_unbound",
            "why": "存在认证采用记录：晋级成绩必须绑定评测协议指纹（eval_protocol_fingerprint）才可对账（DE-35 同协议门）",
            "certificate_id": certificate_id,
        }
    return {
        "ok": True,
        "certificate_id": certificate_id,
        "adoption_id": str(adoption.get("adoption_id") or ""),
        "eval_protocol_fingerprint": report_fp,
        "note": "协议已绑定：同制品一致性由 active_snapshot_hash 门裁决（DE-18）",
    }


# ── 报告字段：五状态显式区分（certificate/adoption/tier 三个事实源合一） ──────


def certification_state(skill_name: str) -> dict[str, Any]:
    """skill 的五状态阶梯报告（只读，不改任何状态）。

    ``states`` 五个键一一对应 search_champion / certified_candidate /
    adopted_experimental / staging / core；``active`` 标记该状态是否已到达。
    三个事实源各自独立：搜索命名空间记录（champion/certificate/adoption）、
    deployment tier（skill_tiers）。发送授权恒 not_granted（有采用记录时展示）。
    """
    name = str(skill_name or "").strip()
    tier = ""
    try:
        from ..skill_tiers import get_tier

        tier = get_tier(name)
    except Exception:
        tier = ""
    lock = load_record_for_skill(name, CHAMPION_LOCK_NAME) or {}
    certificate = load_record_for_skill(name, CERTIFICATE_NAME) or {}
    adoption = load_record_for_skill(name, ADOPTION_RECORD_NAME) or {}
    states: dict[str, Any] = {}
    for state in STATE_LADDER:
        if state == STATE_SEARCH_CHAMPION:
            reached = bool(lock.get("candidate_id"))
            detail = (
                {
                    "candidate_id": str(lock.get("candidate_id") or ""),
                    "search_id": str(lock.get("search_id") or ""),
                    "locked_at": str(lock.get("locked_at") or ""),
                }
                if reached
                else {}
            )
        elif state == STATE_CERTIFIED_CANDIDATE:
            reached = bool(certificate.get("certificate_id"))
            detail = (
                {
                    "certificate_id": str(certificate.get("certificate_id") or ""),
                    "issued_at": str(certificate.get("issued_at") or ""),
                    "certification_only": True,
                }
                if reached
                else {}
            )
        elif state == STATE_ADOPTED_EXPERIMENTAL:
            reached = bool(adoption.get("adoption_id"))
            detail = (
                {
                    "adoption_id": str(adoption.get("adoption_id") or ""),
                    "released_version": str((adoption.get("released") or {}).get("version") or ""),
                    "tier_after": str(adoption.get("tier_after") or ""),
                    "messaging_authorization": str(adoption.get("messaging_authorization") or _MESSAGING_NOT_GRANTED),
                }
                if reached
                else {}
            )
        else:
            reached = tier == state
            detail = {"tier": tier} if reached else {}
        states[state] = {"state": state, "active": reached, **detail}
    return {
        "skill": name,
        "states": states,
        "current_tier": tier,
        "certification_only": True,
        "messaging_authorization": str(adoption.get("messaging_authorization") or _MESSAGING_NOT_GRANTED)
        if adoption
        else _MESSAGING_NOT_GRANTED,
        "note": "五状态显式分开：认证/采用不折叠为 tier；staging/core 仍由既有门与人工决定",
    }


# ── 回执组合：tier 回退与内容恢复分离（S4 后半） ─────────────────────────────


def rollback_receipt(
    skill_name: str,
    *,
    expected_restored_sha256: str = "",
    reason: str = "",
    actor: str = "user",
) -> dict[str, Any]:
    """staging 回退回执：tier 降级与内容现状**分开**记录，内容未恢复如实报告。

    - tier 部分：复用 ``skill_tiers.rollback``（只降级，绝不改文件）；
    - 内容部分：与采用回执的发布制品 bytes hash 对照——内容仍是认证候选制品时
      ``content_restored=False`` 并明确 next_action（恢复另走 restore_content）；
      提供 ``expected_restored_sha256``（恢复目标快照 hash）时按其判定是否已恢复。
    """
    from ..skill_evolution import resolve_skill_file
    from ..skill_tiers import rollback

    name = str(skill_name or "").strip()
    if not name:
        return _reject("skill_name_missing", "skill name is required")
    rb = rollback(name, reason=reason or "certification rollback receipt (DE-35)")
    if not rb.get("ok"):
        return {"ok": False, "error": str(rb.get("error") or "rollback_failed"), "tier_rolled_back": False}
    adoption = load_record_for_skill(name, ADOPTION_RECORD_NAME) or {}
    released_sha = str((adoption.get("released") or {}).get("sha256") or "")
    skill_file = resolve_skill_file(name, target="active")
    current_sha = ""
    if skill_file is not None and skill_file.is_file():
        current_sha = hashlib.sha256(skill_file.read_bytes()).hexdigest()
    matches_certificate = bool(released_sha) and current_sha == released_sha
    expected = str(expected_restored_sha256 or "").strip()
    restored = bool(expected) and current_sha == expected
    # 如实报告：只有「提供了期望恢复 hash 且当前内容与之一致」才算已恢复；
    # 无法判定时 content_restored=False，绝不把"内容未动"说成"已恢复"。
    content_restored = restored
    receipt = {
        "ok": True,
        "skill": name,
        "tier": {"from_tier": rb.get("from_tier"), "to_tier": rb.get("to_tier"), "trigger": rb.get("trigger")},
        "tier_rolled_back": True,
        "content": {
            "current_sha256": current_sha,
            "matches_certificate": matches_certificate,
            "expected_restored_sha256": expected,
            "restored": restored,
        },
        "content_restored": content_restored,
        "actor": actor,
        "audit_event": "manual_rollback",
    }
    if matches_certificate and not restored:
        receipt["note"] = (
            "tier 已降级，但 active 内容仍是认证候选制品（content_restored=False）："
            "rollback 不等于内容回退——恢复另走 skill_tiers.restore_content 并刷新索引"
        )
        receipt["next_action"] = "restore_content"
    elif restored:
        receipt["note"] = "tier 已降级且 active 内容已恢复为期望版本（两条回执一致）"
    elif expected:
        receipt["note"] = "tier 已降级，但当前内容与期望恢复版本不一致（content_restored=False）：人工核对内容来源"
    else:
        receipt["note"] = (
            "tier 已降级；未提供期望恢复版本 hash，内容是否恢复未判定——"
            f"当前内容{'仍是' if matches_certificate else '不是'}认证候选制品，恢复动作另走 restore_content"
        )
    return receipt


# ── 源漂移守卫（认证/采用前的显式检查入口） ──────────────────────────────────


def source_drift_guard(manifest: SearchManifest) -> dict[str, Any]:
    """搜索期间 active 被原维护更新或投影重建时的显式拒绝（S4 前半）。"""
    integrity = manifest.verify_integrity()
    if integrity.get("ok"):
        return {"ok": True, "reason": ""}
    return {
        "ok": False,
        "error": str(integrity.get("reason") or "blocked_source_changed"),
        "skill_source_changed": bool(integrity.get("skill_source_changed")),
        "drifted": list(integrity.get("drifted") or []),
        "note": "搜索期间源/制品已漂移：不得覆盖（CAS source_changed），重开新搜索并重新审核",
    }
