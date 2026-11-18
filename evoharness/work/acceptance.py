"""交付物登记与独立验收：done 的唯一合法入口（DE-13）。

验收是确定性核对：必需证据齐、绑定当前 revision、产物在授权输出根内、
审阅者与执行者分离；模型自述/exit 0 不构成验收。

R07（验收不复核已登记产物）修复口径：
- review 在判定前重新核对每个已登记产物：文件存在、当前 sha256 与登记
  hash 一致、登记 generation 与 Run 当前 generation 一致、Run 处于允许
  验收的状态（未被取消/失败/暂停）、Task revision 匹配；任一不符即拒绝
  pass（acceptance_blocked 事件携带结构化 reason），Task 保持未 done。
- 同 ID 重复登记不得直接复用旧记录：复用前复核文件 hash/generation/revision，
  内容或代际已变时原位更新为当前事实（重登记可见）。
- 测试报告类产物要求真实检查回执（checker id + 时间 + 结果摘要 hash，
  与产物内容摘要绑定）：已带回执的始终校验绑定；无回执的纯文本报告是否
  作为通过依据由 EVOHARNESS_ACCEPTANCE_RECEIPTS 策略控制（grace 兼容既有
  调用方，enforce 严格拒绝——显式升级路径，不做静默放行）。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from evoharness.clock import utcnow
from evoharness.ids import sha256_text
from evoharness.knowledge.retrieval import validate_run_snapshot, RetrievalError
from evoharness.models import Task, TaskEvent, TaskStatus
from evoharness.work import integrity, service
from evoharness.work.models import Artifact, ArtifactReceipt, ExecutionRun
from evoharness.work.state import is_delegated

# 验收模板：按交付声明的 kind 要求必需产物；未声明时委派交付默认要有测试报告。
TEMPLATES: dict[str, set[str]] = {
    "code": {"diff", "test_report"},
    "qa": {"answer", "evidence"},
    "doc": {"document"},
}
DEFAULT_REQUIRED = {"test_report"}

# 允许验收的 Run 状态：与 request_acceptance 的准入一致；failed/paused/
# waiting_recovery 等一律不可作为验收依据。
VERIFY_ALLOWED_RUN_STATES = frozenset({"waiting_acceptance", "running"})

# 要求真实检查回执的产物 kind（R07：纯文本报告不冒充真实检查）。
RECEIPT_REQUIRED_KINDS = frozenset({"test_report"})

# DE-13：release_gate 结果作为显式验收证据（eval 报告 JSON）。
GATE_REPORT_KIND = "gate_report"

# 回执强制策略：grace（默认，兼容既有未带回执调用方）/ enforce（严格拒绝）。
RECEIPT_ENFORCE_ENV = "EVOHARNESS_ACCEPTANCE_RECEIPTS"


class AcceptanceError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def required_artifacts(task: Task) -> set[str]:
    required: set[str] = set()
    try:
        items = json.loads(task.acceptance_json or "[]")
    except ValueError:
        items = []
    for item in items:
        if isinstance(item, dict):
            required |= set(item.get("required_artifacts") or [])
            kind = item.get("kind")
            if kind in TEMPLATES:
                required |= TEMPLATES[kind]
        elif isinstance(item, str) and item in TEMPLATES:
            required |= TEMPLATES[item]
    return required or set(DEFAULT_REQUIRED)


def receipts_enforced(policy: str | None = None) -> bool:
    """enforce 模式下无回执的 test_report 不作为通过依据（显式升级开关）。

    policy 参数供服务装配按部署模式覆盖环境变量（hosted=strict），
    不在模块导入时改全局环境，避免同进程测试被部署策略污染。
    """
    value = (policy if policy is not None
             else os.environ.get(RECEIPT_ENFORCE_ENV, "")).strip().lower()
    return value in {"1", "enforce", "strict", "true", "on", "yes"}


def receipts_strict(policy: str | None = None) -> bool:
    """strict：在 enforce 之上核对检查执行事实（exit/执行数/超时/被测 bundle）。"""
    value = (policy if policy is not None
             else os.environ.get(RECEIPT_ENFORCE_ENV, "")).strip().lower()
    return value == "strict"


def _hex64(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _bind_checker_receipt(session: Session, run: ExecutionRun, kind: str,
                          artifact: Artifact, checker_receipt: dict | None) -> None:
    """登记时绑定检查回执：checker id + 时间 + 结果摘要 hash + 检查执行事实。

    DE-13：回执记录 exit_code/timed_out/executions/ran/node_ids 与被测 bundle
    摘要；legacy 回执允许为空（默认 0/None），严格策略下这些缺失会被拒绝，
    不会把「没统计」伪装成「通过」。
    """
    if checker_receipt is None:
        return
    if not isinstance(checker_receipt, dict):
        raise AcceptanceError("invalid_checker_receipt")
    checker = str(checker_receipt.get("checker") or "").strip()
    summary = str(checker_receipt.get("summary_sha256") or "").strip().lower()
    if not checker or not _hex64(summary):
        raise AcceptanceError("invalid_checker_receipt")
    exit_code = checker_receipt.get("exit_code")
    if exit_code is not None and not isinstance(exit_code, int):
        raise AcceptanceError("invalid_checker_receipt")
    try:
        executions = max(0, int(checker_receipt.get("executions") or 0))
        ran = max(0, int(checker_receipt.get("ran") or 0))
    except (TypeError, ValueError):
        raise AcceptanceError("invalid_checker_receipt") from None
    bundle = str(checker_receipt.get("bundle_sha256") or "").strip().lower()
    if bundle and not _hex64(bundle):
        raise AcceptanceError("invalid_checker_receipt")
    node_ids = checker_receipt.get("node_ids") or []
    if not isinstance(node_ids, list):
        raise AcceptanceError("invalid_checker_receipt")
    node_ids = [str(v) for v in node_ids][:200]
    # DE-13：测试集差集事实——被删 node、基线/现状计数（严格策略据此拒绝）。
    removed_node_ids = checker_receipt.get("removed_node_ids") or []
    if not isinstance(removed_node_ids, list):
        raise AcceptanceError("invalid_checker_receipt")
    removed_node_ids = [str(v) for v in removed_node_ids][:200]
    modified_node_ids = checker_receipt.get("modified_node_ids") or []
    if not isinstance(modified_node_ids, list):
        raise AcceptanceError("invalid_checker_receipt")
    modified_node_ids = [str(v) for v in modified_node_ids][:200]
    try:
        baseline_tests = max(0, int(checker_receipt.get("baseline_tests") or 0))
        current_tests = max(0, int(checker_receipt.get("current_tests") or 0))
    except (TypeError, ValueError):
        raise AcceptanceError("invalid_checker_receipt") from None
    timed_out = bool(checker_receipt.get("timed_out") or False)
    checked_at = str(checker_receipt.get("checked_at") or utcnow().isoformat())
    body = json.dumps({"artifact_id": artifact.id, "artifact_sha256": artifact.sha256,
                       "checker": checker, "checked_at": checked_at,
                       "summary_sha256": summary, "exit_code": exit_code,
                       "timed_out": timed_out, "executions": executions,
                       "ran": ran, "bundle_sha256": bundle, "node_ids": node_ids,
                       "baseline_tests": baseline_tests,
                       "current_tests": current_tests,
                       "removed_node_ids": removed_node_ids,
                       "modified_node_ids": modified_node_ids},
                      sort_keys=True)
    receipt_id = sha256_text(body)[:32]
    if session.get(ArtifactReceipt, receipt_id) is None:
        session.add(ArtifactReceipt(
            id=receipt_id, artifact_id=artifact.id, run_id=run.id,
            kind=kind, checker=checker, checked_at=checked_at,
            summary_sha256=summary, artifact_sha256=artifact.sha256,
            exit_code=exit_code, timed_out=timed_out, executions=executions,
            ran=ran, bundle_sha256=bundle,
            node_ids_json=json.dumps(node_ids, ensure_ascii=False),
            baseline_tests=baseline_tests, current_tests=current_tests,
            removed_node_ids_json=json.dumps(removed_node_ids, ensure_ascii=False),
            modified_node_ids_json=json.dumps(modified_node_ids, ensure_ascii=False)))
        session.flush()


def register_artifact(session: Session, run: ExecutionRun, *, kind: str, path: str,
                      checker_receipt: dict | None = None) -> Artifact:
    """登记交付产物：路径必须落在该 Run 的授权输出根内，绑定当前任务 revision。

    R07：同 ID 重复登记不得直接复用旧记录——复用前复核文件 hash、generation
    与 task revision；内容或代际已变时原位更新为当前事实（新 hash/新代际
    落库可见），旧 hash 不再冒充当前产物。
    """
    resolved = Path(path).resolve()
    root = Path(run.output_root).resolve()
    if root not in resolved.parents and resolved != root:
        raise AcceptanceError("artifact_path_outside_output_root")
    if not resolved.is_file():
        raise AcceptanceError("artifact_file_missing")
    digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
    artifact_id = sha256_text(f"{run.id}:{kind}:{resolved.as_posix()}")[:32]
    existing = session.get(Artifact, artifact_id)
    if existing is not None:
        if (existing.generation == run.generation
                and existing.task_revision == run.task_revision
                and existing.sha256 == digest):
            _bind_checker_receipt(session, run, kind, existing, checker_receipt)
            return existing
        # 复核不一致：登记为当前事实（hash 复核后的真实内容与代际），重登记可见。
        existing.sha256 = digest
        existing.generation = run.generation
        existing.task_revision = run.task_revision
        existing.created_at = utcnow()
        session.add(existing)
        session.flush()
        _bind_checker_receipt(session, run, kind, existing, checker_receipt)
        return existing
    artifact = Artifact(id=artifact_id,
                        run_id=run.id, generation=run.generation,
                        task_revision=run.task_revision, kind=kind,
                        path=resolved.as_posix(), sha256=digest,
                        scope_hash=sha256_text(resolved.as_posix()))
    session.add(artifact)
    session.flush()
    _bind_checker_receipt(session, run, kind, artifact, checker_receipt)
    return artifact


def request_acceptance(session: Session, task_id: int, *, run_id: str, requester: str) -> Task:
    """执行完成后请求验收：waiting_acceptance → in_review。"""
    task = session.get(Task, task_id)
    if task is None or not is_delegated(task):
        raise AcceptanceError("delegated_task_required")
    run = session.get(ExecutionRun, run_id)
    if run is None or run.task_id != task_id:
        raise AcceptanceError("run_not_for_task")
    if run.state not in {"waiting_acceptance", "running"}:
        raise AcceptanceError("run_not_awaiting_acceptance")
    if run.task_revision != task.revision:
        raise AcceptanceError("stale_run_revision")
    try:
        validate_run_snapshot(session, run)
    except RetrievalError as exc:
        raise AcceptanceError(exc.code) from exc
    if task.status == "waiting_acceptance":
        return service.transition_to(session, task_id, to_state="in_review", actor=requester)
    if task.status in {"running", "verifying"}:
        if task.status == "running":
            service.transition_to(session, task_id, to_state="verifying", actor=requester)
        return service.transition_to(session, task_id, to_state="in_review", actor=requester)
    if task.status == "in_review":
        return task
    raise AcceptanceError("task_not_accepting_review")


def _receipt_problem(session: Session, row: Artifact, run: ExecutionRun,
                     *, policy: str | None = None) -> str | None:
    """回执复核：缺失按策略、绑定不符一律拒绝（回执不可移植冒充）。

    strict 策略额外核对检查执行事实：exit=0、非超时、至少执行一次、
    被测 bundle 摘要与当前工作区源码一致（检查后源码被改写则旧结论失效）。
    """
    label = f"{row.kind}:{Path(row.path).name}"
    receipt = session.scalar(select(ArtifactReceipt).where(
        ArtifactReceipt.artifact_id == row.id))
    if receipt is None:
        if receipts_enforced(policy):
            return f"receipt_missing:{label}"
        return None
    if (receipt.artifact_sha256 != row.sha256 or receipt.run_id != run.id
            or receipt.kind != row.kind):
        return f"receipt_unbound:{label}"
    if not receipts_strict(policy):
        return None
    if receipt.exit_code is None:
        return f"checker_exit_unknown:{label}"
    if receipt.exit_code != 0:
        return f"checker_exit_nonzero:{label}:{receipt.exit_code}"
    if receipt.timed_out:
        return f"checker_timed_out:{label}"
    if int(receipt.executions or 0) < 1:
        return f"checker_not_executed:{label}"
    if int(receipt.ran or 0) < 1:
        return f"checker_no_tests_ran:{label}"
    removed = json.loads(receipt.removed_node_ids_json or "[]")
    if removed:
        # 检查改动了测试集：整条测试被删/改名后，检查通过不能代表原测试集通过。
        return f"checker_tests_removed:{label}:{len(removed)}"
    modified = json.loads(receipt.modified_node_ids_json or "[]")
    if modified:
        # 原测试被改写（弱化断言/换实现）：原测试集只读，改写后的通过不算数。
        return f"checker_tests_modified:{label}:{len(modified)}"
    if not receipt.bundle_sha256:
        return f"checker_bundle_unbound:{label}"
    workspace = integrity.run_workspace(run.checkpoint_json or "{}")
    if workspace is None:
        return f"checker_bundle_unavailable:{label}"
    if integrity.workspace_bundle_digest(workspace) != receipt.bundle_sha256:
        return f"checker_bundle_changed:{label}"
    return None


def _gate_report_problem(row: Artifact) -> str | None:
    """release_gate 证据：JSON 必须显式 passed 且绑定冻结门禁摘要。

    契约（DE-13）：``{"ok": true, "gatesSha256": "<64 hex>"}``；
    ``gates_sha256`` 视作同义字段。缺字段/解析失败/未通过都拒绝，
    不做「没写结论=通过」的静默放行。
    """
    label = f"{row.kind}:{Path(row.path).name}"
    try:
        payload = json.loads(Path(row.path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return f"gate_report_invalid:{label}"
    if not isinstance(payload, dict):
        return f"gate_report_invalid:{label}"
    gates = str(payload.get("gatesSha256") or payload.get("gates_sha256") or "")
    if not _hex64(gates.strip().lower()):
        return f"gate_report_invalid:{label}"
    if payload.get("ok") is not True:
        return f"gate_not_passed:{label}"
    return None


def _artifact_integrity_problems(session: Session, run: ExecutionRun,
                                 *, policy: str | None = None) -> list[str]:
    """R07：判定前重新核对 Run 可验收性与每个已登记产物（确定性，无模型自述）。"""
    if run.state not in VERIFY_ALLOWED_RUN_STATES:
        return [f"run_not_verifiable:{run.state}"]
    problems: list[str] = []
    try:
        validate_run_snapshot(session, run)
    except RetrievalError as exc:
        problems.append(exc.code)
    rows = list(session.scalars(select(Artifact).where(
        Artifact.run_id == run.id).order_by(Artifact.id)))
    for row in rows:
        label = f"{row.kind}:{Path(row.path).name}"
        if row.generation != run.generation:
            problems.append(f"generation_stale:{label}")
            continue
        file_path = Path(row.path)
        if not file_path.is_file():
            problems.append(f"artifact_missing:{label}")
            continue
        digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
        if digest != row.sha256:
            problems.append(f"hash_mismatch:{label}")
            continue
        if row.kind == GATE_REPORT_KIND:
            problem = _gate_report_problem(row)
            if problem:
                problems.append(problem)
            continue
        if row.kind in RECEIPT_REQUIRED_KINDS:
            problem = _receipt_problem(session, row, run, policy=policy)
            if problem:
                problems.append(problem)
    return problems


def review(session: Session, task_id: int, *, run_id: str, reviewer: str,
           verdict: str, notes: str = "", policy: str | None = None) -> Task:
    """独立验收：证据核对通过才 done；失败回到 running 返工并留审阅记录。

    R07：pass 判定前重新核对每个已登记产物（存在性/hash/generation）、Run
    可验收状态与检查回执；任一不符即拒绝通过并落 acceptance_blocked 事件
    （结构化 reason：artifact_missing/hash_mismatch/generation_stale/
    run_not_verifiable/receipt_missing/receipt_unbound），Task 保持未 done。
    拒绝路径与事件在同一事务内，不产生任何完成态写入。
    """
    if verdict not in {"pass", "fail"}:
        raise AcceptanceError("invalid_verdict")
    task = session.get(Task, task_id)
    if task is None or not is_delegated(task):
        raise AcceptanceError("delegated_task_required")
    if reviewer not in {task.owner, task.assignee_id}:
        raise AcceptanceError("reviewer_not_authorized")
    run = session.get(ExecutionRun, run_id)
    if run is None or run.task_id != task_id:
        raise AcceptanceError("run_not_for_task")
    if reviewer == run.actor_id:
        raise AcceptanceError("reviewer_must_differ_from_executor")
    if task.status != "in_review":
        raise AcceptanceError("task_not_in_review")

    # 确定性证据核对：exit 0 / 模型自述不算数。
    artifacts = {row.kind: row for row in session.scalars(
        select(Artifact).where(Artifact.run_id == run_id))}
    missing = sorted(required_artifacts(task) - set(artifacts))
    stale = sorted(kind for kind, row in artifacts.items()
                   if row.task_revision != task.revision
                   or row.task_revision != run.task_revision)
    if verdict == "pass":
        if missing:
            raise AcceptanceError("acceptance_evidence_missing:" + ",".join(missing))
        if stale:
            raise AcceptanceError("acceptance_evidence_stale_revision:" + ",".join(stale))
        problems = _artifact_integrity_problems(session, run, policy=policy)
        if problems:
            # 拒绝通过：不标 done、不进入完成转换；理由落审阅事件（同一事务）。
            session.add(TaskEvent(task_id=task_id, event_type="acceptance_blocked",
                                  actor=reviewer,
                                  detail=("artifact_integrity: " + "; ".join(problems)
                                          + (f" | {notes}" if notes else ""))[:500]))
            session.flush()
            return task
        session.add(TaskEvent(task_id=task_id, event_type="acceptance_passed",
                              actor=reviewer, detail=notes[:500],
                              evidence_message_ids=json.dumps([f"run:{run.id}"])))
        from evoharness.knowledge.task_learning import capture_feedback
        capture_feedback(session,task,run,reviewer=reviewer,verdict=verdict,notes=notes)
        session.flush()
        return service.transition_to(session, task_id, to_state=TaskStatus.DONE,
                                     actor=reviewer, reason=notes,
                                     _accepted_delivery=True)
    session.add(TaskEvent(task_id=task_id, event_type="acceptance_rejected",
                          actor=reviewer,
                          detail=(notes or "rework")[:500] + (
                              f" | missing={missing}" if missing else "")))
    from evoharness.knowledge.task_learning import capture_feedback
    capture_feedback(session,task,run,reviewer=reviewer,verdict=verdict,notes=notes)
    session.flush()
    return service.transition_to(session, task_id, to_state="running", actor=reviewer,
                                 reason=f"验收退回：{notes or '未满足验收'}")
