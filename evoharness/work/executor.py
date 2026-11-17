"""受限执行器：隔离工作区、固化版本、结构化检查点（DE-11）。

执行器只是 Run 的驱动：Run 成功退出不等于业务验收（done 唯一入口在
acceptance.review）。真实模型执行经适配器注入——生产装配 Harness Agent Loop，
离线测试用合成适配器，不触网、不消耗费用。
"""
from __future__ import annotations

import json
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from sqlalchemy.orm import Session

from evoharness.models import Task
from evoharness.work import execution, service
from evoharness.work.acceptance import register_artifact
from evoharness.work.execution import DEFAULT_LEASE_SECONDS

# 心跳续租默认间隔（秒）：须显著小于租约时长（默认 120s），可配置。
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 30.0

# 受限工具集：问答只读（无 shell 写权限）；编码在隔离根内放开读写与 shell。
READ_ONLY_TOOLS = frozenset({"read_file", "list_files", "grep_search", "tool_search", "read_knowledge",
                             "skill", "compact_context"})
CODING_TOOLS = frozenset(READ_ONLY_TOOLS | {"write_file", "edit_file", "run_shell",
                                            "enter_plan_mode", "exit_plan_mode"})


def guard_path(write_roots: list[str], path: str) -> str:
    """工作目录/写根越界拒绝：受限执行的第一道确定性闸门。"""
    resolved = Path(path).resolve()
    if not write_roots:
        raise PermissionError("write_outside_roots")
    for root in write_roots:
        base = Path(root).resolve()
        if resolved == base or base in resolved.parents:
            return resolved.as_posix()
    raise PermissionError("write_outside_roots")


class ExecutionAdapter(Protocol):
    kind: str

    def execute(self, workdir: Path, context: dict, allowed_tools: frozenset[str],
                guard: Callable[[str], str]) -> "AdapterResult": ...


@dataclass
class AdapterResult:
    ok: bool
    claimed_done: bool = False
    artifacts: list[tuple[str, str]] = field(default_factory=list)   # [(kind, relative_path)]
    checkpoint: dict = field(default_factory=dict)
    error: str = ""


def prepare_workspace(run, task: Task, *, source_tree: str | Path | None = None) -> tuple[Path, dict]:
    """在 Run 的授权输出根内建隔离工作区（可选复制源树副本）；返回工作区与固化版本记录。

    工作区必须是 output_root 的子目录：产物路径约束（授权输出根内）与执行写根
    保持同一棵树。版本记录进 checkpoint：repo commit、模型、prompt、工具集、
    上下文与知识快照由调用方在 context 里提供；本函数固化执行环境事实。

    R06 恢复语义：工作区已存在时不再抛 workspace_exists——读取原 checkpoint
    校验工作区归属（checkpoint 固化记录指向同一工作区，或无固化记录但路径即
    本 Run 专属目录），属于同一 run/generation 链则按新 generation 复用续跑，
    并把原 checkpoint 的未完成项（todo）带回给调用方传给适配器；固化记录指向
    其他工作区（不匹配）才拒绝。
    """
    root = Path(run.output_root).resolve() / f"run-{run.id[:12]}"
    checkpoint: dict = {}
    try:
        loaded = json.loads(run.checkpoint_json or "{}")
    except ValueError:
        loaded = {}
    if isinstance(loaded, dict):
        checkpoint = loaded
    reconciliation = (checkpoint.get("reconciliation")
                      if checkpoint.get("reconciliation") is not None else None)
    if root.exists():
        recorded = (checkpoint.get("frozen") or {}).get("workspace") if isinstance(
            checkpoint.get("frozen"), dict) else None
        if recorded and Path(recorded).resolve() != root:
            raise ValueError("workspace_exists")   # 固化记录与预期工作区不一致：不匹配
        # 同一 Run 的既有工作区：按新 generation 续跑，未完成项原样带回。
        frozen = {"workspace": root.as_posix(), "task_revision": task.revision,
                  "goal": task.goal, "workflow_kind": task.workflow_kind,
                  "resumed": True, "resumed_generation": run.generation,
                  "resumed_todo": list(checkpoint.get("todo") or []),
                  "source_tree": str(source_tree) if source_tree else ""}
        if reconciliation is not None:
            # N02：上一代的持久回执对账结论随 frozen 带回（接管不是全新重放）
            frozen["reconciliation"] = reconciliation
        return root, frozen
    root.mkdir(parents=True)
    if source_tree is not None:
        src = Path(source_tree).resolve()
        for item in src.iterdir():
            if item.name in {".git", "__pycache__", ".venv", "node_modules"}:
                continue
            if item.is_dir():
                shutil.copytree(item, root / item.name,
                                ignore=shutil.ignore_patterns("__pycache__", ".venv", "node_modules"))
            else:
                shutil.copy2(item, root / item.name)
    frozen = {"workspace": root.as_posix(), "task_revision": task.revision,
              "goal": task.goal, "workflow_kind": task.workflow_kind,
              "source_tree": str(source_tree) if source_tree else ""}
    if reconciliation is not None:
        # N02：对账结论不因工作区重建而丢失（接管不是全新重放）
        frozen["reconciliation"] = reconciliation
    return root, frozen


class LeaseHeartbeat:
    """R05：Run 执行租约心跳——适配器执行期间后台定期续租。

    - 每 tick 先读 execution.utcnow() 再续租：控制时钟被替换时（测试/演练），
      心跳按被控制的时钟续期，模拟长执行同样生效；
    - tick 失败（stale/连接问题）只记录不抛出：租约是否仍有效由
      execution.transition 的 fencing 在写回时裁决，主流程不被心跳异常打断；
    - 提供 session_factory 时 tick 走独立事务/连接（生产装配）；否则复用
      execute_run 的会话——心跳线程只在适配器执行期存活（主线程此时不触碰
      会话），stop() join 后主线程才继续，天然串行。
    """

    def __init__(self, session: Session, run_id: str, *, worker: str, generation: int,
                 interval: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
                 lease_seconds: int = DEFAULT_LEASE_SECONDS,
                 session_factory=None, on_error: Callable[[Exception], None] | None = None):
        self._session = session
        self._run_id = run_id
        self._worker = worker
        self._generation = int(generation)
        self._interval = max(float(interval), 0.001)
        self._lease_seconds = int(lease_seconds)
        self._session_factory = session_factory
        self._on_error = on_error
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.ticks = 0
        self.errors: list[str] = []

    def tick(self) -> bool:
        """同步续租一次（状态转换前的显式续租与后台 tick 共用同一实现）。"""
        try:
            now = execution.utcnow()
            if self._session_factory is not None:
                with self._session_factory.begin() as scoped:
                    execution.renew_lease(scoped, self._run_id, worker=self._worker,
                                          generation=self._generation, now=now,
                                          lease_seconds=self._lease_seconds)
            else:
                execution.renew_lease(self._session, self._run_id, worker=self._worker,
                                      generation=self._generation, now=now,
                                      lease_seconds=self._lease_seconds)
            self.ticks += 1
            return True
        except Exception as exc:  # 心跳失败不抛入主流程，记录后由 fencing 裁决
            self.errors.append(repr(exc))
            if self._on_error is not None:
                try:
                    self._on_error(exc)
                except Exception:  # noqa: BLE001
                    pass
            return False

    def _loop(self) -> None:
        while not self._stop_event.wait(self._interval):
            self.tick()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name=f"lease-heartbeat-{self._run_id[:12]}")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=max(self._interval + 5.0, 10.0))
            self._thread = None


def _fresh_task(session: Session, task_id):
    """终态转换前强制重读任务事实。

    durable 模式下执行会话是长会话且 expire_on_commit=False——管理员在适配器
    执行期并发提交的取消/暂停对身份映射不可见，直接 session.get 会拿到旧
    status， fencing 校验将被旧事实骗过。显式 expire 让下一次访问重读数据库。
    """
    if task_id is None:
        return None
    task = session.get(Task, task_id)
    if task is not None:
        session.expire(task)
    return task


def _finish_failed(session: Session, *, run_id: str, task_id, worker: str, generation: int,
                   validate_context, checkpoint: dict, durable: bool) -> bool:
    """失败回写：优先走状态机 transition；工作合同已消失（任务取消/暂停/完成
    或授权撤销）时经 execution.abort 受控终止（fencing CAS），Run 不悬挂。

    返回 True 表示 Run 被取消（任务侧已 cancelled），False 表示按 failed 落账。
    """
    cancelled = False
    _fresh_task(session, task_id)
    try:
        execution.transition(session, run_id, worker=worker, generation=generation,
                             state="failed", validate_context=validate_context,
                             checkpoint=checkpoint)
    except ValueError:
        task = session.get(Task, task_id) if task_id is not None else None
        cancelled = task is not None and task.status == "cancelled"
        try:
            # 原因留痕（N02×N03）：受控终止的 Run 行携带真实触发原因
            # （如执行中取消的 run_cancelled），不只留下笼统的 fenced abort。
            reason = str(checkpoint.get("error") or "") if isinstance(checkpoint, dict) else ""
            execution.abort(session, run_id, worker=worker, generation=generation,
                            state="cancelled" if cancelled else "failed",
                            error="work_contract_gone: fenced abort without replay"
                                  + (f" ({reason[:200]})" if reason else ""))
        except ValueError:
            pass   # 租约已失效：recover_expired 是唯一出口，不与恢复路径竞争
    if durable:
        session.commit()
    return cancelled


def execute_run(session: Session, run_id: str, *, adapter: ExecutionAdapter,
                worker: str,
                source_tree: str | Path | None = None,
                validate_context,
                lease_seconds: int = DEFAULT_LEASE_SECONDS,
                heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
                heartbeat: bool = True,
                session_factory=None,
                checker_receipts=None,
                phase_hook=None) -> dict:
    """驱动一次受限执行：认领→运行→产物/检查点→等待验收或失败。

    - checker_receipts：R03 小接口补充（向后兼容，缺省 None 行为不变）——
      产物登记时的 R07 检查回执（kind → receipt dict）；传 callable 时在登记
      期惰性求值（适配器在真实检查后才签发回执）。不影响既有调用方。
    - 模型自述完成（claimed_done）但没有产物时：Run 记 failed，失败可见
    - adapter 异常与准备工作区异常同样落到 failed + error，不静默（R06）
    - Run 成功只到 waiting_acceptance；业务 done 必须走 acceptance.review
    - R05：全程具备租约续期能力——每次状态转换前显式续租一次；适配器执行
      期间由后台心跳线程按 heartbeat_interval 定期续租（每 tick 读
      execution.utcnow，控制时钟替换同样生效）。heartbeat=False 关闭全部
      续租（回归对照：复现修复前的 stale_run_lease 旧行为）。
    - N02 持久模式（session_factory 给定时启用，生产 Worker 装配）：
      认领/租约/执行意图在适配器执行前独立短事务 COMMIT——崩溃后新连接可见
      claimed/running + generation + lease_owner；适配器全程运行在数据库写
      事务之外；回执（产物/检查点/终态）在适配器返回后独立短事务提交；
      心跳用独立 session_factory 连接续租（与主线程 BudgetGateway 并发安全，
      管理员取消/撤权可随时提交）。phase_hook(phase) 在每个持久相提交后
      回调（崩溃注入/观测点：claimed / adapter_enter / adapter_exit /
      finalized）。session_factory=None 保持旧语义（调用方单事务提交）。
    """
    durable = session_factory is not None
    run = execution.claim(session, run_id, worker=worker, validate_context=validate_context,
                          lease_seconds=lease_seconds)
    if run is None:
        return {"claimed": False}
    task = session.get(Task, run.task_id)
    service.transition_to(session, task.id, to_state="running", actor=worker)
    if durable:
        # N02：认领（generation/lease_owner）与执行意图先于适配器持久提交——
        # 崩溃后接管进程按租约/回执对账，不把在途 Run 当全新任务盲重放。
        session.commit()
    # R05：心跳承载续租；heartbeat=False 时为 None，转换前续租一并关闭。
    # N02：session_factory 给定时心跳走独立连接/事务——必须先提交认领，
    # 独立连接才看得到租约；主线程在适配器执行期不占写事务，取消/撤权/
    # 预算回执可并发提交。
    heartbeat_ = LeaseHeartbeat(session, run_id, worker=worker, generation=run.generation,
                                interval=heartbeat_interval, lease_seconds=lease_seconds,
                                session_factory=session_factory) if heartbeat else None
    if heartbeat_ is not None:
        heartbeat_.tick()   # 转换前显式续租
    execution.transition(session, run_id, worker=worker, generation=run.generation,
                         state="running", validate_context=validate_context)
    if durable:
        session.commit()
    if phase_hook is not None:
        phase_hook("claimed")

    workdir: Path | None = None
    frozen: dict = {}
    result: AdapterResult | None = None
    try:
        # R06：准备工作区移入异常处理范围——workspace 异常转 Run 失败，不裸抛。
        workdir, frozen = prepare_workspace(run, task, source_tree=source_tree)
        allowed = CODING_TOOLS if task.workflow_kind != "qa" else READ_ONLY_TOOLS
        write_roots = [str(workdir)] if allowed & {"write_file", "edit_file", "run_shell"} else []

        def _guard(path: str) -> str:
            return guard_path(write_roots, path)

        context = json.loads(run.context_json) | {"frozen": frozen}
        if frozen.get("resumed"):
            # R06：按新 generation 续跑——原 checkpoint 未完成项传给适配器。
            context["resume_todo"] = list(frozen.get("resumed_todo") or [])
            context["resumed_generation"] = frozen.get("resumed_generation")
        if frozen.get("reconciliation") is not None:
            # N02：接管执行携带上一代的持久回执对账结论——不是全新重放。
            context["reconciliation"] = frozen.get("reconciliation")
        # BP-02：dryrun 边界由执行器执行——适配器必须显式声明 dryrun 能力
        # （自身保证不调付费 provider、不做真实远端副作用）；未声明者拒绝执行，
        # 不产生任何适配器副作用（不是只改显示标签）。
        if context.get("mode") == "dryrun" and not getattr(adapter, "dryrun_safe", False):
            raise RuntimeError(
                f"dryrun_adapter_not_safe:{type(adapter).__name__}")
        if heartbeat_ is not None:
            heartbeat_.start()   # 适配器执行期间后台续租
        if phase_hook is not None:
            phase_hook("adapter_enter")
        try:
            result = adapter.execute(workdir, context, frozenset(allowed), _guard)
        finally:
            if heartbeat_ is not None:
                heartbeat_.stop()   # join 后主线程才触碰会话，天然串行
        if phase_hook is not None:
            phase_hook("adapter_exit")   # 适配器已返回、回执未写：崩溃注入点
    except Exception as exc:  # adapter / workspace 崩溃：失败可见，不静默
        if heartbeat_ is not None:
            heartbeat_.stop()
        if heartbeat_ is not None:
            heartbeat_.tick()   # 失败回写前显式续租（失败仅记录，fencing 裁决）
        cancelled = _finish_failed(
            session, run_id=run_id, task_id=run.task_id, worker=worker,
            generation=run.generation, validate_context=validate_context,
            checkpoint={"phase": "execute", "error": repr(exc), **frozen}, durable=durable)
        return {"claimed": True, "ok": False, "error": repr(exc), "aborted": cancelled,
                "workspace": str(workdir) if workdir is not None else ""}

    if not result.ok:
        if heartbeat_ is not None:
            heartbeat_.tick()
        cancelled = _finish_failed(
            session, run_id=run_id, task_id=run.task_id, worker=worker,
            generation=run.generation, validate_context=validate_context,
            checkpoint={**result.checkpoint, "frozen": frozen, "error": result.error},
            durable=durable)
        return {"claimed": True, "ok": False, "error": result.error, "aborted": cancelled,
                "workspace": str(workdir)}

    # 终态转换前显式续租：放在产物登记之前——durable 模式下登记开启主事务
    # 写锁，独立心跳连接在写锁内必然锁冲突（SQLite 单写者）。
    if heartbeat_ is not None:
        heartbeat_.tick()
    # 产物登记：声称完成却拿不出产物 = 失败可见
    registered = []
    receipts = checker_receipts() if callable(checker_receipts) else (checker_receipts or {})
    for kind, relative in result.artifacts:
        registered.append(register_artifact(session, run, kind=kind,
                                            path=str(Path(workdir) / relative),
                                            checker_receipt=receipts.get(kind)))
    if result.claimed_done and not registered:
        cancelled = _finish_failed(
            session, run_id=run_id, task_id=run.task_id, worker=worker,
            generation=run.generation, validate_context=validate_context,
            checkpoint={"frozen": frozen, "error": "claimed_done_without_artifacts"},
            durable=durable)
        return {"claimed": True, "ok": False, "aborted": cancelled,
                "error": "claimed_done_without_artifacts", "workspace": str(workdir)}

    checkpoint = {"goal": task.goal,
                  "done": result.checkpoint.get("done", []),
                  "todo": result.checkpoint.get("todo", []),
                  "diff": result.checkpoint.get("diff", ""),
                  "next": result.checkpoint.get("next", ""),
                  "side_effects": result.checkpoint.get("side_effects", []),
                  "proposals": result.checkpoint.get("proposals", []),
                  "frozen": frozen}
    if "knowledge" in result.checkpoint:
        checkpoint["knowledge"] = result.checkpoint["knowledge"]
    _fresh_task(session, run.task_id)   # 终态转换前重读：执行期取消不被旧事实骗过
    execution.transition(session, run_id, worker=worker, generation=run.generation,
                         state="waiting_acceptance", validate_context=validate_context,
                         checkpoint=checkpoint)
    if durable:
        # N02：回执（产物登记 + 检查回执 + 终态检查点）独立短事务持久提交。
        session.commit()
    if phase_hook is not None:
        phase_hook("finalized")
    return {"claimed": True, "ok": True, "claimed_done": result.claimed_done,
            "artifacts": [a.kind for a in registered],
            "resumed": bool(frozen.get("resumed")),
            "heartbeat_ticks": heartbeat_.ticks if heartbeat_ is not None else 0,
            "workspace": str(workdir)}
