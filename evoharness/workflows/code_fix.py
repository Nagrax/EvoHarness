"""DE-20 缺陷修复 SOP：重现→影响分析→回归→最小修复→全量门禁→差异/风险交付。

SOP 行（指南 §9.3 缺陷修复）：重现 → 影响分析 → 隔离修改 → 回归 → 交付差异；
停止/转人工条件：无法重现、生产权限缺失、预算用尽。

设计要点：
- 步骤 checkpoint 化：每步在 workspace/checkpoints/<序号>-<phase>.json 落盘结构化
  检查点——已完成证据、未完成项、下一动作、待对账副作用，不是对话摘要；Run 状态
  变更仍经 DE-11 的 execution.transition 租约/fencing 写入 run.checkpoint_json。
- 复用 DE-11：Run 认领/租约/fencing（evoharness.work.execution）与隔离工作区
  （executor.prepare_workspace：源树只读复制，写根=工作区，源树被改动即拒绝交付）。
- 先红后绿：新增回归测试必须在修复前真实失败（red），全量门禁在修复后真实通过
  （green）；引擎自称修完而门禁仍红 = 模型假完成，判失败并落证据。
- 不删测试凑绿：find_test_weakening 检测修复 diff 对测试文件断言/用例的删除，
  命中即拒绝该修复。
- 远端边界（DE-12 决定语义）：无远端端口或批准未绑定本次 diff/报告摘要时只产出
  可审阅本地产物；凭据缺失返回明确 blocked（waiting_authorization），不伪造
  提交/PR/上线成功。
- 取消传播（DE-14）：每个步骤边界检查在途取消；取消后不再执行新副作用，
  Run 置 paused、任务置 cancelled，取消事件消费留痕。
- 预算（DE-15）：每步预留→按真实 usage 结算；失败/超时/崩溃同样归集（未知用量
  保守全额）；耗尽转 waiting_budget（Later 等待语义，不计失败、不刷 attempts），
  额度恢复后可续跑，已完成步骤按检查点跳过不重跑。

引擎与测试运行器是注入端口：生产装配受限 Harness 子代理（agents/subagent.py
get_code_fix_engine_config）与受限 shell 测试运行器；离线测试注入合成实现，
不触网、不消耗费用。
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from evoharness.clock import utcnow
from evoharness.governance import budget, budget_gateway
from evoharness.ids import sha256_text
from evoharness.models import Task, TaskEvent
from evoharness.work import execution, executor, service, supplements
from evoharness.work.acceptance import register_artifact
from evoharness.work.models import ExecutionRun

PHASES = ("reproduce", "impact", "regression", "fix", "gate", "deliver")
DEFAULT_PHASE_TOKENS = 1000
MAX_EVIDENCE_CHARS = 4000
SKIP_TREE_PARTS = {"__pycache__", ".git", ".venv", "node_modules"}
# 可续跑的等待类 Run 状态（Later 语义）；恢复后按检查点跳过已完成步骤。
WAITING_RUN_STATES = frozenset({"waiting_budget", "waiting_resource", "waiting_external",
                                "waiting_authorization"})


# ── 注入端口 ──────────────────────────────────────────────────────


@dataclass
class PhaseOutcome:
    """引擎单步结果：ok=该步是否完成；claimed_done=引擎是否自称缺陷已修复。"""
    ok: bool
    summary: str = ""
    todo: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)   # 本步在工作区内产出的文件（相对路径）
    claimed_done: bool = False
    tokens_used: int = 0
    error: str = ""


@dataclass
class TestReport:
    ok: bool
    output: str = ""
    exit_code: int | None = None
    timed_out: bool = False
    ran: int = 0
    evidence: dict = field(default_factory=dict)


class FixEngine(Protocol):
    kind: str

    def run(self, phase: str, workdir: Path, context: dict,
            guard: Callable[[str], str]) -> PhaseOutcome: ...


class TestRunner(Protocol):
    def run(self, workdir: Path, phase: str) -> TestReport: ...


class ScriptTestRunner:
    """生产默认测试运行器：在工作区内以固定 argv 运行检查脚本。

    超时/非零退出/崩溃都是真实失败证据；输出截断保存；不触网。
    """

    kind = "script"

    def __init__(self, argv: list[str], *, timeout_seconds: float = 120.0,
                 max_chars: int = MAX_EVIDENCE_CHARS):
        self.argv = list(argv)
        self.timeout_seconds = timeout_seconds
        self.max_chars = max_chars

    def run(self, workdir: Path, phase: str) -> TestReport:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
        try:
            proc = subprocess.run(
                self.argv, cwd=str(workdir), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=self.timeout_seconds, env=env)
        except subprocess.TimeoutExpired as exc:
            output = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"")
            if not isinstance(output, str):
                output = output.decode("utf-8", "replace")
            return TestReport(ok=False, timed_out=True, exit_code=None,
                              output=output[: self.max_chars])
        except OSError as exc:
            return TestReport(ok=False, exit_code=None, output=f"runner_error: {exc}")
        output = ((proc.stdout or "") + (proc.stderr or "")).strip()
        return TestReport(ok=proc.returncode == 0, exit_code=proc.returncode,
                          output=output[: self.max_chars])


class RemoteCredentialsMissing(RuntimeError):
    """远端凭据缺失：必须显式 blocked，不得伪造提交/PR/上线成功。"""


@dataclass
class DeliveryApproval:
    """DE-12：批准最后绑定具体结果——必须携带本次 diff 与报告的内容摘要。"""
    approver: str
    target: str
    diff_sha256: str
    report_sha256: str


@dataclass
class RemotePayload:
    diff_text: str
    report_text: str
    diff_sha256: str
    report_sha256: str
    target: str


class RemotePublishPort(Protocol):
    kind: str

    def publish(self, payload: RemotePayload) -> dict: ...


# ── diff / 测试弱化检测 ───────────────────────────────────────────


def snapshot_tree(root: str | Path) -> dict[str, str]:
    """rel posix path -> sha256(bytes)；跳过缓存/依赖目录，不依赖 git。"""
    base = Path(root).resolve()
    out: dict[str, str] = {}
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        if SKIP_TREE_PARTS & set(path.parts):
            continue
        out[path.relative_to(base).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def snapshot_texts(root: str | Path) -> dict[str, str]:
    """rel posix path -> 文件文本内容（diff 基线）；跳过缓存/依赖目录。"""
    base = Path(root).resolve()
    out: dict[str, str] = {}
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        if SKIP_TREE_PARTS & set(path.parts):
            continue
        out[path.relative_to(base).as_posix()] = _read_text(path)
    return out


def compute_diff(root: str | Path, before: dict[str, str]) -> tuple[str, list[str], list[str]]:
    """工作区修复前后的统一 diff（不依赖 git）：diff 文本、变更文件、删除文件。

    before 来自 snapshot_texts（修复前的文本快照），当前树与基线做文本比对。
    """
    base = Path(root).resolve()
    after = snapshot_texts(base)
    changed: list[str] = []
    deleted: list[str] = []
    chunks: list[str] = []
    for rel in sorted(set(before) | set(after)):
        old_text = before.get(rel)
        new_text = after.get(rel)
        if old_text == new_text:
            continue
        if new_text is None:
            deleted.append(rel)
        changed.append(rel)
        diff = difflib.unified_diff((old_text or "").splitlines(keepends=True),
                                    (new_text or "").splitlines(keepends=True),
                                    fromfile=f"a/{rel}", tofile=f"b/{rel}")
        chunks.append("".join(diff))
    return "\n".join(chunks), changed, deleted


def is_test_path(rel: str) -> bool:
    parts = rel.split("/")
    name = parts[-1].lower()
    return (name.startswith("test_") and name.endswith(".py")) or "tests" in parts


def find_test_weakening(diff_text: str) -> list[str]:
    """检测修复 diff 是否删除既有测试断言/用例——不允许删测试凑绿。

    命中规则：测试文件（is_test_path）中被删除的行是 assert 语句、pytest.raises
    或 test 用例定义。非测试文件的断言变更不在此拦（由门禁与验收把关）。
    """
    violations: list[str] = []
    current: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current = line[len("+++ b/"):].strip()
            continue
        if line.startswith("--- ") or line.startswith("+++ "):
            continue
        if current and is_test_path(current) and line.startswith("-") \
                and not line.startswith("---"):
            removed = line[1:].strip()
            if removed.startswith("def test_") or removed.startswith("assert ") \
                    or "pytest.raises" in removed:
                violations.append(f"{current}: removed {removed[:120]}")
    return violations


# ── 检查点台账 ────────────────────────────────────────────────────


def _new_checkpoint(phase: str, status: str, *, done: list[str], todo: list[str],
                    next_action: str, side_effects: list[str],
                    extra: dict | None = None) -> dict:
    return {"phase": phase, "status": status, "done": list(done), "todo": list(todo),
            "next": next_action, "side_effects": list(side_effects), **(extra or {})}


def _write_step_checkpoint(workdir: Path, order: int, checkpoint: dict) -> str:
    directory = workdir / "checkpoints"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{order:02d}-{checkpoint['phase']}.json"
    path.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2, sort_keys=True),
                    encoding="utf-8")
    return path.as_posix()


def _load_completed(workdir: Path) -> dict[str, dict]:
    """读取步骤台账中已完成（status=done）的 SOP 步骤，供等待恢复后跳过重跑。"""
    out: dict[str, dict] = {}
    directory = workdir / "checkpoints"
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("status") == "done" and data.get("phase") in PHASES:
            out[data["phase"]] = data
    return out


def _truncate(text: str, limit: int = MAX_EVIDENCE_CHARS) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def _ensure_workspace(run: ExecutionRun, task: Task,
                      source_tree: str | Path | None) -> tuple[Path, dict]:
    # R06：工作区复用/校验收敛到 executor.prepare_workspace——已有工作区读取
    # 原 checkpoint 校验归属后按新 generation 续跑（未完成项由执行器带给适配器），
    # 不匹配才报 workspace_exists；不再各自维护一套复用口径。
    return executor.prepare_workspace(run, task, source_tree=source_tree)


# ── SOP 主流程 ────────────────────────────────────────────────────


def run_code_fix(session, run_id: str, *, engine: FixEngine, runner: TestRunner,
                 worker: str, source_tree: str | Path | None = None, validate_context,
                 remote: RemotePublishPort | None = None,
                 approval: DeliveryApproval | None = None,
                 phase_tokens: dict[str, int] | None = None,
                 lease_seconds: int = execution.DEFAULT_LEASE_SECONDS,
                 heartbeat_interval: float = executor.DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
                 heartbeat: bool = True) -> dict:
    """驱动缺陷修复 SOP。返回结构化摘要；任何失败都带真实证据与下一动作。

    - 全新 Run（queued）：认领后从 reproduce 顺序执行。
    - 等待类 Run（waiting_budget 等）：Later 恢复，按检查点跳过已完成步骤。
    - 其余状态拒绝驱动，不伪装成功。
    - R05：门禁/引擎单步可能超过固定租约——引擎与测试运行器执行期间由后台
      心跳线程续租（每 tick 读 execution.utcnow，控制时钟替换同样生效），
      每次 Run 状态转换前显式续租一次；heartbeat=False 关闭续租（回归对照）。
    """
    tokens = {phase: (phase_tokens or {}).get(phase, DEFAULT_PHASE_TOKENS) for phase in PHASES}
    run = session.get(ExecutionRun, run_id)
    if run is None:
        raise ValueError("run_not_found")
    task = session.get(Task, run.task_id)
    if task is None:
        raise ValueError("task_not_found")

    resumed = False
    if run.state == "queued":
        claimed = execution.claim(session, run_id, worker=worker, validate_context=validate_context,
                                  lease_seconds=lease_seconds)
        if claimed is None:
            return {"ok": False, "status": "not_claimed",
                    "next": "并发认领或租约冲突：稍后重试或检查执行器"}
        run = claimed
        service.transition_to(session, task.id, to_state="running", actor=worker)
    elif run.state in WAITING_RUN_STATES:
        # R05：等待类 Run 进入等待时已释放执行租约（LeaseRelease）；恢复执行前
        # 必须重新授权认领（reacquire：重验授权、重取租约、generation 递增），
        # 之后 waiting/claimed → running 的过渡仍由 execution.transition 裁决。
        run = execution.reacquire(session, run_id, worker=worker,
                                  validate_context=validate_context,
                                  lease_seconds=lease_seconds)
        run = execution.transition(session, run_id, worker=worker,
                                   generation=run.generation, state="running",
                                   validate_context=validate_context)
        resumed = True
    else:
        return {"ok": False, "status": run.state,
                "next": "run 状态不允许直接驱动；需要恢复请走 execution.recovery"}

    # R05：SOP 全程租约续期能力；generation 在认领/重取后已定型。
    beat = executor.LeaseHeartbeat(session, run_id, worker=worker, generation=run.generation,
                                   interval=heartbeat_interval, lease_seconds=lease_seconds) \
        if heartbeat else None

    workdir, frozen = _ensure_workspace(run, task, source_tree)
    context = json.loads(run.context_json)
    # 隔离边界基线：交付前源树必须与进入时逐字节一致（修复只允许落在 Run 工作区）。
    source_before = snapshot_tree(source_tree) if source_tree is not None else {}

    def _guard(path: str) -> str:
        return executor.guard_path([str(workdir)], path)

    def _transition_run(state: str, checkpoint: dict) -> None:
        # R05：每次 Run 状态转换前显式续租一次（失败仅记录，fencing 由 transition 裁决）。
        if beat is not None:
            beat.tick()
        # DE-11 语义：execution.transition 会把 run 状态镜像到 task.status（单一事实源），
        # 工作流不再经 service.transition_to 重复转换，只补审计事件。
        execution.transition(session, run_id, worker=worker, generation=run.generation,
                             state=state, validate_context=validate_context,
                             checkpoint=checkpoint)

    def _audit(event_type: str, detail: str) -> None:
        session.add(TaskEvent(task_id=task.id, event_type=event_type[:32], actor=worker,
                              detail=(detail or "")[:500]))

    order = 0

    def _ledger(checkpoint: dict) -> str:
        nonlocal order
        order += 1
        merged = {"goal": task.goal, **checkpoint, "frozen": frozen}
        return _write_step_checkpoint(workdir, order, merged)

    def _reserve(phase: str):
        return budget.reserve(session, org_id=task.org_id or "", space_id=task.space_id or "",
                              principal_id=run.actor_id or "", work_item_id=task.id,
                              run_id=run.id, category="code_fix",
                              tokens=tokens[phase], estimated=True)

    def _settle(reservation, *, used: int, ok: bool) -> None:
        if reservation is None:
            return
        budget.settle(session, reservation.reservation_id, used_tokens=max(0, used), ok=ok)

    def _record_of(reservation):
        """R03 预算网关：把阶段预留绑定为付费端口上下文（DE-20 引擎端口接线）。

        引擎/测试运行器内的模型调用（生产装配为网关感知 provider）凭该记录
        经 require_record 放行；无预留上下文的付费调用 fail-closed 拒绝。
        结算仍走既有 _settle（真实 usage 恰好结算一次）。
        """
        if reservation is None:
            return None
        return budget_gateway.BudgetRecord(
            reservation_id=reservation.reservation_id,
            org_id=reservation.org_id, space_id=reservation.space_id,
            principal_id=reservation.principal_id,
            work_item_id=int(reservation.work_item_id or 0),
            run_id=reservation.run_id, category=reservation.category,
            reserved_tokens=int(reservation.reserved_tokens))

    def _evidence_path(name: str) -> Path:
        directory = workdir / "evidence"
        directory.mkdir(parents=True, exist_ok=True)
        return directory / name

    def _fail(phase: str, error: str, *, report: TestReport | None = None,
              next_action: str, todo: list[str] | None = None) -> dict:
        # 真实失败证据落盘：失败输出摘要 + 预算消耗 + 下一动作，不伪装成功。
        body = ""
        if report is not None:
            head = ("timeout: runner exceeded time limit"
                    if report.timed_out else f"exit_code={report.exit_code}")
            body = f"{head}\n{_truncate(report.output) or '(no output)'}"
        elif error:
            body = error
        evidence_rel = ""
        if body:
            path = _evidence_path(f"failure-{phase}.txt")
            path.write_text(f"phase={phase}\nerror={error}\n{body}\n", encoding="utf-8")
            evidence_rel = path.relative_to(workdir).as_posix()
        checkpoint = _new_checkpoint(phase, "failed", done=done,
                                     todo=[*(todo or []), f"{phase} 失败：{error}"],
                                     next_action=next_action, side_effects=side_effects,
                                     extra={"error": error, "failure_evidence": evidence_rel})
        ledger_path = _ledger(checkpoint)
        _transition_run("failed", checkpoint)
        _audit("failed", f"{phase}: {error}; next={next_action}")
        session.flush()
        return {"ok": False, "status": "failed", "error": error,
                "checkpoint": ledger_path,
                "failure_evidence": (workdir / evidence_rel).as_posix() if evidence_rel else "",
                "phases_done": list(done), "side_effects": list(side_effects),
                "next": next_action}

    def _wait_budget(phase: str, exc: budget.BudgetError) -> dict:
        # 预算耗尽是等待（Later）不是失败：不计失败、不刷 attempts，恢复后续跑。
        checkpoint = _new_checkpoint(phase, "waiting_budget", done=done,
                                     todo=[f"{phase} 未执行：预算不可用"],
                                     next_action="预算恢复或人工补额度后唤醒续跑（已完成步骤按检查点跳过）",
                                     side_effects=side_effects,
                                     extra={"budget_error": f"{exc.code}:{exc.scope}"})
        ledger_path = _ledger(checkpoint)
        _transition_run("waiting_budget", checkpoint)
        task.waiting_reason = f"{phase} 预算不可用：{exc.code}:{exc.scope}"
        task.next_action = "预算恢复或人工补额度后唤醒续跑"
        _audit("waiting_budget", f"{phase}: {task.waiting_reason}; 唤醒条件=预算恢复")
        session.flush()
        return {"ok": False, "status": "waiting_budget",
                "error": f"{exc.code}:{exc.scope}", "checkpoint": ledger_path,
                "phases_done": list(done), "next": checkpoint["next"]}

    def _cancelled(phase: str) -> dict:
        # DE-14 取消传播：消费取消事件留痕，Run 置 paused，不再执行新副作用。
        pending = [row for row in supplements.pending(session, task.id)
                   if row.event_kind == "cancellation"]
        for row in pending:
            supplements.consume(session, row.id)
        events = [row.event_id for row in pending]
        checkpoint = _new_checkpoint(phase, "cancelled", done=done,
                                     todo=[f"{phase} 未执行：任务已取消"],
                                     next_action="取消生效；重开需携带新失败证据走 queued",
                                     side_effects=side_effects,
                                     extra={"cancelled_events": events})
        ledger_path = _ledger(checkpoint)
        _transition_run("paused", checkpoint)
        # 业务取消：paused → cancelled 是合法状态机转换；已发生副作用保留对账。
        service.transition_to(session, task.id, to_state="cancelled", actor=worker,
                              reason=f"取消传播：{','.join(events) or 'manual'}")
        session.flush()
        return {"ok": False, "status": "cancelled", "cancelled_events": events,
                "checkpoint": ledger_path, "phases_done": list(done),
                "workspace": workdir.as_posix(),
                "side_effects": list(side_effects),
                "next": "已取消，不再执行新副作用"}

    def _cancelled_pending() -> bool:
        return supplements.has_pending_cancellation(session, task.id)

    def _engine(phase: str, reservation):
        try:
            if beat is not None:
                beat.start()   # 引擎单步（可能远超固定租约）期间后台续租
            try:
                with budget_gateway.record_context(_record_of(reservation)):
                    outcome = engine.run(phase, workdir, context, _guard)
            finally:
                if beat is not None:
                    beat.stop()
        except Exception as exc:  # 引擎崩溃：失败可见，不静默；未知用量保守全额归集
            _settle(reservation, used=tokens[phase], ok=False)
            return None, _fail(phase, f"engine_crash: {exc!r}",
                               next_action="检查引擎适配器与工作区环境后重试")
        if not outcome.ok:
            _settle(reservation, used=outcome.tokens_used, ok=False)
            return None, _fail(phase, outcome.error or f"{phase}_failed",
                               next_action="按失败原因修正后重试", todo=list(outcome.todo))
        return outcome, None

    def _suite(phase: str, reservation) -> tuple[TestReport | None, dict | None]:
        try:
            if beat is not None:
                beat.start()   # 测试运行器（全量门禁可能远超固定租约）期间后台续租
            try:
                with budget_gateway.record_context(_record_of(reservation)):
                    report = runner.run(workdir, phase)
            finally:
                if beat is not None:
                    beat.stop()
        except Exception as exc:
            _settle(reservation, used=tokens[phase], ok=False)
            return None, _fail(phase, f"runner_crash: {exc!r}",
                               next_action="检查测试运行器配置与工作区状态")
        return report, None

    # ── 等待恢复：读取步骤台账，已完成步骤跳过并恢复证据 ──
    completed = _load_completed(workdir)
    done: list[str] = []
    side_effects: list[str] = []
    red: dict | None = None
    green: dict | None = None
    impact_summary = ""
    diff_text = ""
    diff_files: list[str] = []
    fix_claimed_done = False
    for phase in PHASES:
        data = completed.get(phase)
        if data is None:
            continue
        done.extend(data.get("done", []))
        side_effects.extend(data.get("side_effects", []))
        if phase == "impact":
            impact_summary = str(data.get("impact", ""))
        elif phase == "regression":
            red = data.get("red_evidence") or None
        elif phase == "fix":
            diff_files = list(data.get("diff_files", []))
            fix_claimed_done = True
            patch_file = workdir / "artifacts" / "fix.patch"
            if patch_file.is_file():
                diff_text = _read_text(patch_file)
        elif phase == "gate":
            green = data.get("green_evidence") or None

    start = _new_checkpoint("plan", "running",
                            done=done + ["Run 已进入 running，隔离工作区就绪"]
                            + (["等待恢复续跑：已完成步骤按检查点跳过"] if resumed else []),
                            todo=[f"待执行步骤：{'→'.join(p for p in PHASES if p not in completed)}"],
                            next_action=next((p for p in PHASES if p not in completed), "deliver"),
                            side_effects=side_effects, extra={"resumed": resumed})
    _ledger(start)
    if not resumed:
        _transition_run("running", start)
    session.flush()

    # ── 1. 重现：检查在未修复副本上必须失败；无法重现是停止/转人工条件 ──
    if "reproduce" not in completed:
        if _cancelled_pending():
            return _cancelled("reproduce")
        try:
            reservation = _reserve("reproduce")
        except budget.BudgetError as exc:
            return _wait_budget("reproduce", exc)
        outcome, terminal = _engine("reproduce", reservation)
        if terminal is not None:
            return terminal
        report, terminal = _suite("reproduce", reservation)
        if terminal is not None:
            return terminal
        if report.timed_out:
            _settle(reservation, used=tokens["reproduce"], ok=False)
            return _fail("reproduce", "reproduce_timeout", report=report,
                         next_action="提高重现超时上限或拆小重现步骤后重试")
        if report.ok:
            # 检查在未修复副本上通过 = 缺陷未重现：停止，不进入修复。
            _settle(reservation, used=outcome.tokens_used, ok=True)
            return _fail("defect_not_reproduced", "defect_not_reproduced", report=report,
                         next_action="人工确认缺陷触发条件或补充重现环境；无法重现不进入修复")
        red = {"phase": "reproduce", "ok": False, "exit_code": report.exit_code,
               "output": _truncate(report.output)}
        path = _evidence_path("reproduce-red.txt")
        path.write_text(f"exit_code={report.exit_code}\n{_truncate(report.output)}\n",
                        encoding="utf-8")
        _settle(reservation, used=outcome.tokens_used, ok=True)
        done.append("重现：检查在未修复副本上失败（红），失败输出已落盘 evidence/reproduce-red.txt")
        _ledger(_new_checkpoint("reproduce", "done", done=done,
                                todo=[p for p in PHASES if p not in completed and p != "reproduce"],
                                next_action="impact", side_effects=side_effects,
                                extra={"reproduce_evidence": red}))
        session.flush()

    # ── 2. 影响分析 ──
    if "impact" not in completed:
        if _cancelled_pending():
            return _cancelled("impact")
        try:
            reservation = _reserve("impact")
        except budget.BudgetError as exc:
            return _wait_budget("impact", exc)
        outcome, terminal = _engine("impact", reservation)
        if terminal is not None:
            return terminal
        impact_summary = outcome.summary
        path = _evidence_path("impact.md")
        path.write_text(f"# 影响分析\n{impact_summary}\n\n待办：\n"
                        + "".join(f"- {item}\n" for item in outcome.todo), encoding="utf-8")
        _settle(reservation, used=outcome.tokens_used, ok=True)
        done.append(f"影响分析：{impact_summary[:80]}")
        side_effects.extend(f"workspace_file:{rel}" for rel in outcome.paths)
        _ledger(_new_checkpoint("impact", "done", done=done,
                                todo=list(outcome.todo),
                                next_action="regression", side_effects=side_effects,
                                extra={"impact": impact_summary}))
        session.flush()

    # ── 3. 回归测试：新增回归必须在修复前真实失败（先红） ──
    if "regression" not in completed:
        if _cancelled_pending():
            return _cancelled("regression")
        try:
            reservation = _reserve("regression")
        except budget.BudgetError as exc:
            return _wait_budget("regression", exc)
        outcome, terminal = _engine("regression", reservation)
        if terminal is not None:
            return terminal
        report, terminal = _suite("regression", reservation)
        if terminal is not None:
            return terminal
        if report.timed_out:
            _settle(reservation, used=tokens["regression"], ok=False)
            return _fail("regression", "regression_timeout", report=report,
                         next_action="提高回归超时上限或拆小回归用例后重试")
        if report.ok:
            # 回归在修复前就通过 = 用例没有覆盖缺陷：先红不成立，拒绝进入修复。
            _settle(reservation, used=outcome.tokens_used, ok=False)
            return _fail("regression", "regression_not_red", report=report,
                         next_action="回归必须先在未修复代码上失败；修正用例使其真实覆盖缺陷")
        red = {"phase": "regression", "ok": False, "exit_code": report.exit_code,
               "output": _truncate(report.output)}
        path = _evidence_path("regression-red.txt")
        path.write_text(f"exit_code={report.exit_code}\n{_truncate(report.output)}\n",
                        encoding="utf-8")
        _settle(reservation, used=outcome.tokens_used, ok=True)
        done.append("回归（红）：新增回归测试在修复前真实失败，输出已落盘 evidence/regression-red.txt")
        side_effects.extend(f"workspace_file:{rel}" for rel in outcome.paths)
        _ledger(_new_checkpoint("regression", "done", done=done,
                                todo=[p for p in PHASES if p not in completed
                                      and p not in {"reproduce", "impact", "regression"}],
                                next_action="fix", side_effects=side_effects,
                                extra={"red_evidence": red}))
        session.flush()

    # ── 4. 最小修复：diff 不得删除/弱化既有测试断言 ──
    if "fix" not in completed:
        if _cancelled_pending():
            return _cancelled("fix")
        try:
            reservation = _reserve("fix")
        except budget.BudgetError as exc:
            return _wait_budget("fix", exc)
        before = snapshot_texts(workdir)   # 修复前文本快照：diff 以此为基线
        outcome, terminal = _engine("fix", reservation)
        if terminal is not None:
            return terminal
        fix_claimed_done = outcome.claimed_done
        diff_text, diff_files, deleted = compute_diff(workdir, before)
        weakening = find_test_weakening(diff_text) + [
            f"{rel}: test file deleted" for rel in deleted if is_test_path(rel)]
        if weakening:
            _settle(reservation, used=outcome.tokens_used, ok=False)
            return _fail("fix", "test_weakening_rejected",
                         next_action="重出最小修复 diff：不得删除/弱化既有测试断言或删除测试用例",
                         todo=weakening)
        if not diff_files:
            _settle(reservation, used=outcome.tokens_used, ok=False)
            return _fail("fix", "no_fix_applied",
                         next_action="引擎未产生任何代码变更；检查修复是否真的落到工作区")
        artifacts_dir = workdir / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        (artifacts_dir / "fix.patch").write_text(diff_text, encoding="utf-8")
        _settle(reservation, used=outcome.tokens_used, ok=True)
        done.append(f"最小修复：{', '.join(diff_files)}（diff 已落盘 artifacts/fix.patch）")
        side_effects.extend(f"fix_applied:{rel}" for rel in diff_files)
        _ledger(_new_checkpoint("fix", "done", done=done,
                                todo=list(outcome.todo),
                                next_action="gate", side_effects=side_effects,
                                extra={"diff_files": diff_files,
                                       "diff_text": _truncate(diff_text)}))
        session.flush()

    # ── 5. 全量门禁：修复后必须真实转绿；自称修完仍红 = 模型假完成 ──
    if "gate" not in completed:
        if _cancelled_pending():
            return _cancelled("gate")
        try:
            reservation = _reserve("gate")
        except budget.BudgetError as exc:
            return _wait_budget("gate", exc)
        outcome, terminal = _engine("gate", reservation)
        if terminal is not None:
            return terminal
        report, terminal = _suite("gate", reservation)
        if terminal is not None:
            return terminal
        if report.timed_out:
            _settle(reservation, used=tokens["gate"], ok=False)
            return _fail("gate", "gate_timeout", report=report,
                         next_action="提高门禁超时上限或拆小全量检查后重试")
        if not report.ok:
            _settle(reservation, used=tokens["gate"], ok=False)
            # 修复阶段自称完成（或门禁步自称完成）而门禁仍红 = 模型假完成。
            error = ("claimed_done_but_gate_red"
                     if (fix_claimed_done or outcome.claimed_done) else "gate_red")
            return _fail("gate", error, report=report,
                         next_action="按门禁失败输出继续最小修复，或退回返工；不得在门禁红时宣称完成")
        green = {"ok": True, "exit_code": report.exit_code, "output": _truncate(report.output)}
        path = _evidence_path("gate-green.txt")
        path.write_text(f"exit_code={report.exit_code}\n{_truncate(report.output)}\n",
                        encoding="utf-8")
        _settle(reservation, used=outcome.tokens_used, ok=True)
        done.append("全量门禁（绿）：回归与全量检查通过，输出已落盘 evidence/gate-green.txt")
        _ledger(_new_checkpoint("gate", "done", done=done, todo=[],
                                next_action="deliver", side_effects=side_effects,
                                extra={"green_evidence": green}))
        session.flush()

    # ── 6. 差异/风险交付：本地产物 + 远端边界 ──
    if _cancelled_pending():
        return _cancelled("deliver")
    try:
        reservation = _reserve("deliver")
    except budget.BudgetError as exc:
        return _wait_budget("deliver", exc)
    outcome, terminal = _engine("deliver", reservation)
    if terminal is not None:
        return terminal
    # 隔离边界：源树被改动即拒绝交付（修复只允许落在隔离工作区）
    if source_tree is not None and snapshot_tree(source_tree) != source_before:
        _settle(reservation, used=outcome.tokens_used, ok=False)
        return _fail("deliver", "source_tree_mutated",
                     next_action="检查隔离边界：修复必须只落在 Run 工作区内")
    artifacts_dir = workdir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    patch_path = artifacts_dir / "fix.patch"
    if not patch_path.is_file():
        patch_path.write_text(diff_text, encoding="utf-8")
    report_text = "\n".join([
        "# 缺陷修复测试报告",
        f"defect: {task.goal}",
        f"impact: {impact_summary}",
        f"red: exit_code={(red or {}).get('exit_code')} output={(red or {}).get('output', '')[:200]}",
        f"green: exit_code={(green or {}).get('exit_code')} output={(green or {}).get('output', '')[:200]}",
        f"diff_files: {', '.join(diff_files)}",
        "risks:",
        *(f"- {item}" for item in outcome.todo),
    ]) + "\n"
    report_path = artifacts_dir / "test-report.md"
    report_path.write_text(report_text, encoding="utf-8")
    # R07：测试报告登记携带真实检查回执（红/绿门禁证据摘要，绑定报告内容摘要），
    # 验收时按回执核对；报告被改写或回执移植即 receipt_unbound。
    regression_receipt = {
        "checker": "code_fix.regression_gate",
        "checked_at": utcnow().isoformat(),
        "summary_sha256": sha256_text(json.dumps(
            {"red": red or {}, "green": green or {}}, sort_keys=True, ensure_ascii=False)),
    }
    register_artifact(session, run, kind="diff", path=str(patch_path))
    register_artifact(session, run, kind="test_report", path=str(report_path),
                      checker_receipt=regression_receipt)
    side_effects.extend(["local_artifact:artifacts/fix.patch", "local_artifact:artifacts/test-report.md"])
    _settle(reservation, used=outcome.tokens_used, ok=True)
    done.append("差异/风险交付：patch 与测试报告已登记为本地产物")

    diff_sha = sha256_text(diff_text)
    report_sha = sha256_text(report_text)

    # 远端边界（DE-12）：没有端口/批准未绑定具体结果只交付本地；凭据缺失明确 blocked。
    remote_status = "local_only_not_authorized"
    receipt: dict | None = None
    credentials_note = remote_status
    if remote is not None:
        if approval is None:
            remote_status = "blocked_approval_required"
        elif (approval.diff_sha256, approval.report_sha256) != (diff_sha, report_sha):
            remote_status = "blocked_approval_not_bound_to_result"
        else:
            try:
                receipt = remote.publish(RemotePayload(
                    diff_text=diff_text, report_text=report_text, diff_sha256=diff_sha,
                    report_sha256=report_sha, target=approval.target))
            except RemoteCredentialsMissing as exc:
                remote_status = "blocked_credentials_missing"
                receipt = None
                credentials_note = f"remote_credentials_missing: {exc}"
            except Exception as exc:
                failed_cp = _new_checkpoint(
                    "deliver", "failed", done=done, todo=[f"远端发布失败：{exc!r}"],
                    next_action="核对远端配置后重试或转人工", side_effects=side_effects,
                    extra={"error": f"remote_publish_failed: {exc!r}"})
                _ledger(failed_cp)
                _transition_run("failed", failed_cp)
                _audit("failed", f"deliver: remote_publish_failed: {exc}")
                session.flush()
                return {"ok": False, "status": "failed", "error": "remote_publish_failed",
                        "phases_done": list(done), "next": "核对远端配置后重试或转人工"}
            else:
                if isinstance(receipt, dict) and receipt.get("ok"):
                    kind = str(receipt.get("kind", remote.kind))
                    ref = str(receipt.get("ref", ""))
                    remote_status = f"published:{kind}:{ref}"
                    side_effects.append(f"remote_publish:{kind}:{ref}")
                    done.append(f"远端交付：{kind} {ref}")
                else:
                    detail = json.dumps(receipt, ensure_ascii=False)[:200] if receipt else "(empty receipt)"
                    failed_cp = _new_checkpoint(
                        "deliver", "failed", done=done, todo=[f"远端发布被拒：{detail}"],
                        next_action="核对远端回执后重试或转人工", side_effects=side_effects,
                        extra={"error": "remote_publish_rejected"})
                    _ledger(failed_cp)
                    _transition_run("failed", failed_cp)
                    _audit("failed", f"deliver: remote_publish_rejected: {detail}")
                    session.flush()
                    return {"ok": False, "status": "failed", "error": "remote_publish_rejected",
                            "phases_done": list(done), "next": "核对远端回执后重试或转人工"}

    if remote_status.startswith("blocked"):
        # 本地交付完成但远端动作被拒：明确 blocked 语义，不伪造提交/PR/上线成功。
        checkpoint = _new_checkpoint("deliver", "waiting_authorization", done=done,
                                     todo=[f"远端动作未执行：{credentials_note}",
                                           "本地 patch 与测试报告已登记，可审阅"],
                                     next_action="补齐远端写入授权/凭据，并按本结果摘要绑定批准后重试远端动作",
                                     side_effects=side_effects,
                                     extra={"remote_status": remote_status,
                                            "diff_sha256": diff_sha, "report_sha256": report_sha,
                                            "red_evidence": red, "green_evidence": green})
        ledger_path = _ledger(checkpoint)
        _transition_run("waiting_authorization", checkpoint)
        task.waiting_reason = f"远端交付被拒：{credentials_note}"
        task.next_action = checkpoint["next"]
        _audit("waiting_authorization", task.waiting_reason)
        session.flush()
        return {"ok": False, "status": "waiting_authorization", "remote": remote_status,
                "receipt": None, "checkpoint": ledger_path,
                "diff_sha256": diff_sha, "report_sha256": report_sha,
                "artifacts": ["diff", "test_report"], "phases_done": list(done),
                "side_effects": list(side_effects),
                "next": checkpoint["next"]}

    final = _new_checkpoint("deliver", "waiting_acceptance", done=done, todo=[],
                            next_action="提交独立验收（acceptance.request_acceptance / review）",
                            side_effects=side_effects,
                            extra={"remote_status": remote_status, "impact": impact_summary,
                                   "diff_files": diff_files, "diff_sha256": diff_sha,
                                   "report_sha256": report_sha,
                                   "red_evidence": red, "green_evidence": green})
    ledger_path = _ledger(final)
    _transition_run("waiting_acceptance", final)
    _audit("waiting_acceptance", f"diff={diff_sha[:16]} report={report_sha[:16]} remote={remote_status}")
    session.flush()
    return {"ok": True, "status": "waiting_acceptance", "workspace": workdir.as_posix(),
            "phases_done": list(PHASES), "red": red, "green": green,
            "diff_files": diff_files, "diff_sha256": diff_sha, "report_sha256": report_sha,
            "artifacts": ["diff", "test_report"], "remote": remote_status,
            "receipt": receipt, "side_effects": list(side_effects),
            "checkpoint": ledger_path, "next": final["next"]}
