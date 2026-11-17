"""DE-34 持久搜索状态存储：父代、候选、协议、RNG、待完成 Case 与 Attempt 全部落盘。

对应 SKILL-EVOLUTION-SEARCH §11（多代搜索状态、成本与可靠恢复）与
NATIVE-ARCHITECTURE-INTEGRATION §6（数据契约）：

- **落盘位置**：只写 DE-29 搜索命名空间
  ``<base>/<tenant>/<space>/<lineage>/<search_id>/``（``state.json`` 状态机文档、
  ``pool.json``（DE-33 CandidatePool）、``candidates/<cid>/`` 候选制品、
  ``feedback/`` 反馈记录、``receipts/`` 挂载/清理回执）；
  不复制第二份调度真相，不写生产技能目录（复用 DE-29 冻结只读守卫）。
- **状态文档**：phase（搜索阶段）、round（当前轮次与阶段断点：父代、patch、
  候选、逐 Case 终态、开放预留）、rng/sampler 状态、成本归集账本、取消/暂停
  标记、清理回执。每次保存都是一次原子写（唯一临时文件 → fsync → os.replace，
  复用 ``agents.session.atomic_write_text`` 口径），旧会话没有状态文件按未启动处理。
- **generation CAS**：状态文档带单调 ``generation``；``save()`` 以读入时的
  generation 为乐观锁——磁盘 generation 与期望不符（另一 worker 已推进状态）
  → ``StoreConflict``，旧 worker 不能覆盖新状态（接 DE-11/14 的代际语义）。
- **跨实例/跨进程原子区（R08）**：generation 比较与文件替换不是不可分割的——
  原子替换只保证单文件不写半截。因此所有「读 generation→比较→写」全程
  （``save``/``initialize``/租约认领与推进）都持有命名空间级 OS 文件互斥锁
  （``namespace_exclusive_lock``：win32 msvcrt / posix flock，内核持有、
  持有者崩溃即释放）；``claim_lease`` 在锁内重读磁盘最新状态作判定依据，
  不依赖内存副本，也不依赖 ``_disk_generation`` 的读取时序——任意交错下
  两个实例/进程并发认领恰好一个成功。
- **worker 租约**：``claim_lease`` 以 owner+lease_until 防两 worker 并发推进；
  租约过期可被接管（接管不改 generation 语义，只换 owner）。
- **幂等可恢复**：状态写入与外部副作用解耦——副作用前先落"预留/在途"标记，
  完成后再落"终态+结算"；恢复方据此对账（控制器 DE-34 负责对账策略，
  本模块只保证标记与终态原子可见）。
- **协议/源身份绑定**：initialize 时记录 manifest 摘要与协议指纹；恢复时
  ``verify_binding`` 比对——manifest 或协议漂移即拒绝续用旧状态（不重算指纹，
  直接 fail-closed）。

时间来源：``created_at/updated_at`` 用真实墙钟（记账用途）；判定逻辑（CAS、
阶段推进、租约比较）全部使用调用方传入的 ``now``（控制器注入固定时钟，离线可测）。
"""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .contracts import (
    MANIFEST_NAME,
    SearchManifest,
    search_namespace,
)
from .pool import CandidatePool, POOL_FILE_NAME

STATE_SCHEMA_VERSION = 1
CONTROLLER_ALGORITHM_VERSION = "skill-search-controller-v1"
STATE_FILE_NAME = "state.json"
LOCK_FILE_NAME = ".state.lock"
LOCK_POLL_SECONDS = 0.005
DEFAULT_LOCK_TIMEOUT_SECONDS = 30.0

# 搜索级阶段（§11 状态机）
PHASE_DRAFT = "draft"
PHASE_READINESS = "readiness_check"
PHASE_BASELINE = "baseline_evaluating"
PHASE_SEARCHING = "searching"
PHASE_FINALIZING = "finalizing"
PHASE_AWAITING_REVIEW = "awaiting_review"
PHASE_COMPLETED = "completed"
SEARCH_PHASES = (
    PHASE_DRAFT,
    PHASE_READINESS,
    PHASE_BASELINE,
    PHASE_SEARCHING,
    PHASE_FINALIZING,
    PHASE_AWAITING_REVIEW,
    PHASE_COMPLETED,
)

# 非终态阻塞（waiting 语义：Later，不消耗失败次数）
STATUS_RUNNING = "running"
STATUS_PAUSED = "paused"
STATUS_BLOCKED_BUDGET = "blocked_budget"
STATUS_BLOCKED_ENVIRONMENT = "blocked_environment"
STATUS_BLOCKED_SOURCE_CHANGED = "blocked_source_changed"
STATUS_BLOCKED_PROTOCOL_DRIFT = "blocked_protocol_drift"
STATUS_CANCELLED = "cancelled"
STATUS_FAILED = "failed"
STATUS_CLEANUP_FAILED = "cleanup_failed"
WAITING_STATUSES = {STATUS_PAUSED, STATUS_BLOCKED_BUDGET, STATUS_BLOCKED_ENVIRONMENT}
TERMINAL_STATUSES = {
    PHASE_COMPLETED,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_CLEANUP_FAILED,
    STATUS_BLOCKED_SOURCE_CHANGED,
    STATUS_BLOCKED_PROTOCOL_DRIFT,
}

# 轮次内阶段断点（§11 轮次状态；恢复从断点续跑，不重抽父代/不重生成 patch）
ROUND_PHASE_NONE = ""
ROUND_PHASE_PARENT = "parent_selection"
ROUND_PHASE_FEEDBACK = "feedback"
ROUND_PHASE_MUTATION = "mutation"
ROUND_PHASE_VALIDATION = "validation"
ROUND_PHASE_DEPLOYMENT = "deployment"
ROUND_PHASE_EVALUATION = "evaluation"
ROUND_PHASE_JUDGING = "judging"
ROUND_PHASES = (
    ROUND_PHASE_PARENT,
    ROUND_PHASE_FEEDBACK,
    ROUND_PHASE_MUTATION,
    ROUND_PHASE_VALIDATION,
    ROUND_PHASE_DEPLOYMENT,
    ROUND_PHASE_EVALUATION,
    ROUND_PHASE_JUDGING,
)


class StoreConflict(RuntimeError):
    """generation CAS 失败：磁盘状态已被另一 worker 推进（旧 generation 不得覆盖）。"""

    def __init__(self, *, expected_generation: int, disk_generation: int, path: str):
        self.code = "stale_generation"
        self.expected_generation = int(expected_generation)
        self.disk_generation = int(disk_generation)
        self.path = str(path)
        super().__init__(
            f"stale_generation: expected generation {self.expected_generation} "
            f"but disk has {self.disk_generation} at {self.path}"
        )


class StoreCorruption(RuntimeError):
    """状态文件存在但不可解析/被外部改坏：不猜状态，拒绝恢复。"""

    def __init__(self, code: str, path: str):
        self.code = code
        self.path = str(path)
        super().__init__(f"{code}: {self.path}")


class StoreLockTimeout(RuntimeError):
    """命名空间互斥锁获取超时：另一进程/线程长期持有（不覆盖、不抢占，如实报错）。"""

    def __init__(self, path: str, timeout: float):
        self.code = "store_lock_timeout"
        self.path = str(path)
        self.timeout = float(timeout)
        super().__init__(f"store_lock_timeout: {self.path} (waited {self.timeout}s)")


@contextmanager
def namespace_exclusive_lock(
    namespace: str | Path, *, timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    lock_name: str = LOCK_FILE_NAME,
) -> Iterator[Path]:
    """搜索命名空间级跨实例/跨进程互斥锁（R08 原子区的唯一入口）。

    - OS 级文件字节锁：win32 ``msvcrt.locking`` / posix ``fcntl.flock``，
      锁由内核随句柄持有——同进程内不同句柄（多线程）同样互斥；
    - **锁异常安全**：持有者崩溃/进程死亡时内核自动释放，其他实例可接管
      （不留下永久死锁的锁文件）；
    - 获取失败在 deadline 内轮询等待，超时抛 ``StoreLockTimeout``（fail-closed，
      绝不无锁写入）。
    """
    directory = Path(namespace)
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / lock_name
    handle = os.open(str(lock_path), os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
    deadline = time.monotonic() + max(0.0, float(timeout))
    acquired = False
    try:
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise StoreLockTimeout(str(lock_path), float(timeout)) from None
                time.sleep(LOCK_POLL_SECONDS)
        yield lock_path
    finally:
        if acquired:
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass  # 释放失败只影响显式语义；关闭句柄同样由内核释放
        os.close(handle)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> int:
    """原子写 JSON（复用 agents.session.atomic_write_text 的唯一临时文件口径）。"""
    from ..session import atomic_write_text

    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, text)
    return len(text.encode("utf-8"))


def manifest_digest(manifest: SearchManifest) -> str:
    """manifest 冻结身份摘要：落盘前后比对，检测 manifest 文档被替换/漂移。"""
    from .contracts import _canonical_sha256

    return _canonical_sha256(manifest.to_dict())


def _default_state(*, manifest: SearchManifest, case_ids: list[str], config: dict[str, Any]) -> dict[str, Any]:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "controller_version": CONTROLLER_ALGORITHM_VERSION,
        "search_id": manifest.search_id,
        "tenant": manifest.tenant,
        "space": manifest.space,
        "lineage": manifest.lineage,
        "bundle_hash": manifest.bundle_hash,
        "protocol_fingerprint": manifest.eval_protocol_fingerprint,
        "manifest_digest_value": manifest_digest(manifest),
        "s_case_ids": sorted({str(cid) for cid in case_ids}),
        "case_ids": sorted({str(cid) for cid in case_ids}),
        "config": dict(config or {}),
        "generation": 0,
        "owner": "",
        "lease_until": 0.0,
        "phase": PHASE_DRAFT,
        "status": STATUS_RUNNING,
        "pause_reason": "",
        "block_reason": "",
        "created_at": now,
        "updated_at": now,
        # 基线阶段断点：逐 Case 终态 + 完整性门结果（重启不重跑已完成 Case）
        "baseline": {"cases": {}, "finalize": None, "admitted": False},
        # 当前轮次断点（completed_rounds 之外只有一条活动轮次：初期单变异链）
        "round": {
            "index": 0,
            "round_id": "",
            "phase": ROUND_PHASE_NONE,
            "parent_id": "",
            "parent_draw": None,
            "feedback_ref": "",
            "patch": None,
            "patch_meta": {},
            "candidate_id": "",
            "candidate_ref": "",
            "candidate_meta": {},
            "cases": {},
            "finalize": None,
            "infra_failures": 0,
            "failure_attempts": 0,
        },
        "completed_rounds": [],
        "sampler": {"seed": 0, "state": None},
        "finalization": {"cases": {}, "verification": None, "champion": "", "champion_meta": {}},
        "cleanup": {"restored": False, "receipt": {}, "can_publish": False, "release_blockers": []},
        "costs": {},
        "open_reservations": {},
        "cancel_requested": False,
        "cancel_note": "",
        "pause_requested": False,
        "history": [],
    }


class SearchStore:
    """一次搜索任务的持久状态文档句柄（内存副本 + generation CAS 原子落盘）。

    用法：``load()``（或 ``initialize()``）→ 直接修改 ``store.state`` → ``save()``。
    ``save()`` 以读入时 generation 为乐观锁，成功后内存 generation 前进；
    冲突抛 ``StoreConflict``，调用方 ``reload()`` 后重新决策（不得盲写）。
    """

    def __init__(self, namespace: str | Path):
        self.namespace = Path(namespace)
        self.state: dict[str, Any] | None = None

    # ── 路径 ────────────────────────────────────────────────────────────────

    @property
    def state_path(self) -> Path:
        return self.namespace / STATE_FILE_NAME

    @property
    def manifest_path(self) -> Path:
        return self.namespace / MANIFEST_NAME

    @property
    def pool_path(self) -> Path:
        return self.namespace / POOL_FILE_NAME

    def candidate_dir(self, candidate_id: str) -> Path:
        cid = Path(str(candidate_id or "").strip()).name
        if not cid:
            raise ValueError("candidate_id_invalid")
        return self.namespace / "candidates" / cid

    def feedback_dir(self) -> Path:
        return self.namespace / "feedback"

    def receipt_path(self, name: str) -> Path:
        safe = Path(str(name or "").strip()).name
        if not safe:
            raise ValueError("receipt_name_invalid")
        return self.namespace / "receipts" / f"{safe}.json"

    # ── 生命周期 ────────────────────────────────────────────────────────────

    def exists(self) -> bool:
        return self.state_path.is_file()

    def initialize(
        self,
        *,
        manifest: SearchManifest,
        case_ids: list[str],
        config: dict[str, Any] | None = None,
        sampler_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """新建状态文档（已存在则拒绝——不覆盖既有搜索，恢复走 load）。

        存在性检查与首次落盘同持命名空间互斥锁：两实例并发 initialize 恰好一个成功。
        """
        self.namespace.mkdir(parents=True, exist_ok=True)
        with namespace_exclusive_lock(self.namespace):
            if self.state_path.is_file():
                raise StoreCorruption("state_already_exists", str(self.state_path))
            self.state = _default_state(manifest=manifest, case_ids=case_ids, config=config or {})
            if sampler_state is not None:
                self.state["sampler"] = dict(sampler_state)
            self.state["history"].append({"event": "initialized", "phase": PHASE_DRAFT})
            self._write_state_locked()
        return self.state

    def load(self) -> dict[str, Any]:
        """读取状态文档；不存在返回 None；坏文件抛 StoreCorruption（不猜状态）。"""
        path = self.state_path
        if not path.is_file():
            self.state = None
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StoreCorruption("state_unreadable", str(path)) from exc
        if not isinstance(data, dict):
            raise StoreCorruption("state_not_dict", str(path))
        if int(data.get("schema_version") or 0) > STATE_SCHEMA_VERSION:
            raise StoreCorruption("state_schema_newer", str(path))
        required = ("search_id", "generation", "phase", "status", "s_case_ids")
        missing = [key for key in required if key not in data]
        if missing:
            raise StoreCorruption(f"state_missing_fields:{','.join(missing)}", str(path))
        self.state = data
        return data

    def load_or_initialize(
        self,
        *,
        manifest: SearchManifest,
        case_ids: list[str],
        config: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """存在即恢复，不存在才初始化；返回 (state, created)。"""
        if self.exists():
            return self.load(), False
        return self.initialize(manifest=manifest, case_ids=case_ids, config=config), True

    def reload(self) -> dict[str, Any]:
        """丢弃内存副本并重读磁盘（StoreConflict 后的重新决策入口）。"""
        state = self.load()
        if state is None:
            raise StoreCorruption("state_vanished", str(self.state_path))
        return state

    def save(self) -> dict[str, Any]:
        """CAS 原子落盘：磁盘 generation 必须等于内存期望，成功后内存 +1。

        「读磁盘 generation → 比较 → 原子替换」全程持有命名空间互斥锁：
        比较与写入不可分割（``_disk_generation`` 的读取时序任意交错都不产生
        双写），两实例/两进程并发 save 恰好一个成功，另一个 ``StoreConflict``。
        """
        if self.state is None:
            raise StoreCorruption("state_not_loaded", str(self.state_path))
        with namespace_exclusive_lock(self.namespace):
            return self._write_state_locked()

    def _write_state_locked(self) -> dict[str, Any]:
        """CAS 写入（调用方必须已持有 ``namespace_exclusive_lock``）。"""
        expected = int(self.state.get("generation") or 0)
        disk = self._disk_generation()
        if disk != expected:
            raise StoreConflict(expected_generation=expected, disk_generation=disk, path=str(self.state_path))
        payload = dict(self.state)
        payload["generation"] = expected + 1
        payload["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        atomic_write_json(self.state_path, payload)
        self.state = payload
        return payload

    def _disk_generation(self) -> int:
        path = self.state_path
        if not path.is_file():
            return 0  # 全新搜索：generation 从 0 起步，首笔保存写 1
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return int(data.get("generation") or 0)
        except (OSError, ValueError, json.JSONDecodeError):
            return -2

    # ── worker 租约（防两 worker 并发推进同一次搜索） ────────────────────────

    def claim_lease(self, worker: str, *, now: float, lease_seconds: float = 300.0) -> bool:
        """租约空闲/过期则授予并落盘；他人持有效租约时返回 False（不抢占）。

        判定与写盘同持命名空间互斥锁，且判定依据是**锁内重读的磁盘最新状态**
        （不是内存副本、不经过 ``_disk_generation`` 的读取时序）：两个实例/进程
        并发认领同一命名空间恰好一个成功，另一个拿到 False。
        """
        if self.state is None:
            raise StoreCorruption("state_not_loaded", str(self.state_path))
        with namespace_exclusive_lock(self.namespace):
            disk = self.load()
            if disk is None:
                raise StoreCorruption("state_vanished", str(self.state_path))
            owner = str(disk.get("owner") or "")
            lease_until = float(disk.get("lease_until") or 0.0)
            if owner and owner != str(worker) and lease_until > float(now):
                return False  # 内存副本已同步为磁盘现状（owner 可如实上报）
            payload = dict(disk)
            payload["owner"] = str(worker)
            payload["lease_until"] = float(now) + float(lease_seconds)
            payload["generation"] = int(disk.get("generation") or 0) + 1
            payload["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            atomic_write_json(self.state_path, payload)
            self.state = payload
            return True

    def renew_lease(self, worker: str, *, now: float, lease_seconds: float = 300.0) -> bool:
        state = self.state
        if state is None:
            raise StoreCorruption("state_not_loaded", str(self.state_path))
        if str(state.get("owner") or "") != str(worker):
            return False
        with namespace_exclusive_lock(self.namespace):
            state["lease_until"] = float(now) + float(lease_seconds)
            self._write_state_locked()
        return True

    def release_lease(self, worker: str) -> bool:
        state = self.state
        if state is None:
            raise StoreCorruption("state_not_loaded", str(self.state_path))
        if str(state.get("owner") or "") != str(worker):
            return False
        with namespace_exclusive_lock(self.namespace):
            state["owner"] = ""
            state["lease_until"] = 0.0
            self._write_state_locked()
        return True

    # ── 绑定核验（协议/manifest 漂移拒绝续用旧状态） ─────────────────────────

    def verify_binding(self, manifest: SearchManifest) -> dict[str, Any]:
        """恢复前核对：manifest 摘要、bundle、协议指纹与落盘时一致才可续用。

        漂移归类：bundle 变化 → ``bundle_changed``（源漂移）；协议指纹变化 →
        ``protocol_changed``；其余文档级漂移 → ``manifest_drift``。全部 fail-closed。
        """
        state = self.state
        if state is None:
            raise StoreCorruption("state_not_loaded", str(self.state_path))
        current_digest = manifest_digest(manifest)
        if str(state.get("manifest_digest_value") or "") != current_digest:
            if str(state.get("bundle_hash") or "") != manifest.bundle_hash:
                return {"ok": False, "error": "bundle_changed", "reason": "bundle hash 与落盘时不一致（源已变化）"}
            if str(state.get("protocol_fingerprint") or "") != manifest.eval_protocol_fingerprint:
                return {
                    "ok": False,
                    "error": "protocol_changed",
                    "reason": "协议指纹与落盘时不一致：跨协议分数不可混用，旧分数不得续用",
                }
            return {
                "ok": False,
                "error": "manifest_drift",
                "reason": "manifest 文档与落盘时不一致（种子身份/协议/预算被改动）：拒绝续用旧状态与旧分数",
            }
        if str(state.get("bundle_hash") or "") != manifest.bundle_hash:
            return {"ok": False, "error": "bundle_changed", "reason": "bundle hash 与落盘时不一致"}
        if str(state.get("protocol_fingerprint") or "") != manifest.eval_protocol_fingerprint:
            return {
                "ok": False,
                "error": "protocol_changed",
                "reason": "协议指纹与落盘时不一致：跨协议分数不可混用，旧分数不得续用",
            }
        return {"ok": True, "reason": ""}

    # ── 成本归集账本（各阶段全部成本：预留/结算计数与 token 合计） ────────────

    def record_reservation(self, *, key: str, category: str, handle: str, tokens: int) -> None:
        state = self.state
        if state is None:
            raise StoreCorruption("state_not_loaded", str(self.state_path))
        state["open_reservations"][key] = {
            "handle": str(handle),
            "category": str(category),
            "tokens": int(tokens),
        }

    def pop_reservation(self, key: str) -> dict[str, Any] | None:
        state = self.state
        if state is None:
            raise StoreCorruption("state_not_loaded", str(self.state_path))
        return state["open_reservations"].pop(key, None)

    def record_settlement(
        self, *, category: str, reserved_tokens: int, used_tokens: int, ok: bool, calls: int = 1
    ) -> None:
        state = self.state
        if state is None:
            raise StoreCorruption("state_not_loaded", str(self.state_path))
        costs = state["costs"]
        entry = costs.setdefault(
            str(category), {"reserved_tokens": 0, "used_tokens": 0, "calls": 0, "failed_calls": 0, "released": 0}
        )
        entry["reserved_tokens"] += int(reserved_tokens)
        entry["used_tokens"] += int(used_tokens)
        entry["calls"] += int(calls)
        if not ok:
            entry["failed_calls"] += 1

    def record_release(self, *, category: str, tokens: int) -> None:
        state = self.state
        if state is None:
            raise StoreCorruption("state_not_loaded", str(self.state_path))
        entry = state["costs"].setdefault(
            str(category), {"reserved_tokens": 0, "used_tokens": 0, "calls": 0, "failed_calls": 0, "released": 0}
        )
        entry["released"] += int(tokens)

    def cost_summary(self) -> dict[str, Any]:
        state = self.state
        if state is None:
            raise StoreCorruption("state_not_loaded", str(self.state_path))
        return {category: dict(entry) for category, entry in state["costs"].items()}

    # ── 轮次与候选制品 ──────────────────────────────────────────────────────

    def append_history(self, event: str, **detail: Any) -> None:
        state = self.state
        if state is None:
            raise StoreCorruption("state_not_loaded", str(self.state_path))
        history = state.setdefault("history", [])
        history.append({"event": str(event), **detail})
        # 有界：状态文档不无限增长，完整事实在 pool/attempts 与 artifacts 里
        del history[:-200]

    def save_artifact(self, name: str | Path, payload: dict[str, Any], *, subdir: str | Path | None = None) -> Path:
        """结构化产物（反馈/回执/评测引用）原子落盘到命名空间；只接受纯文件名。"""
        if subdir is None:
            path = self.namespace / Path(name).name
        else:
            base = self.namespace / Path(subdir)
            base.mkdir(parents=True, exist_ok=True)
            path = base / Path(str(name)).name
        atomic_write_json(path, payload)
        return path

    def load_artifact(self, name: str | Path, *, subdir: str | Path | None = None) -> dict[str, Any] | None:
        if subdir is None:
            path = self.namespace / Path(name).name
        else:
            path = self.namespace / Path(subdir) / Path(str(name)).name
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def save_pool(self, pool: CandidatePool) -> dict[str, Any]:
        return pool.save(self.namespace)

    def load_pool(self, *, search_id: str | None = None) -> CandidatePool | None:
        if not self.pool_path.is_file():
            return None
        pool = CandidatePool.load(self.namespace)
        if search_id and pool.search_id != search_id:
            raise StoreCorruption("pool_search_id_mismatch", str(self.pool_path))
        return pool

    def save_receipt(self, name: str, payload: dict[str, Any]) -> Path:
        return self.save_artifact(name, payload, subdir="receipts")

    def load_receipt(self, name: str) -> dict[str, Any] | None:
        return self.load_artifact(name, subdir="receipts")


def open_search_store(
    manifest: SearchManifest, *, base_dir: str | Path | None = None
) -> "SearchStore":
    """按 DE-29 命名空间打开状态句柄（目录由 manifest 的 tenant/space/lineage/search_id 决定）。"""
    namespace = search_namespace(manifest, base_dir)
    return SearchStore(namespace)
