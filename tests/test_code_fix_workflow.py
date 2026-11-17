"""DE-20：缺陷修复 SOP 与隔离代码交付——先红后绿、不删测试凑绿、远端边界、取消与预算。

场景对应 tasks.json DE-20 acceptance_scenarios 与 implementation_steps：
- 带重复事件的合成仓库缺陷自动产出修复与先红后绿回归（红→绿两阶段证据落盘）
- 失败/超时/模型假完成：真实失败证据、预算归集（DE-15 语义）与下一动作，不伪装成功
- 修复 diff 删除既有测试断言 → 工作流拒绝该修复（工具层另有写保护钩子，双闸）
- 无远端授权只产可审阅本地产物；凭据缺失明确 blocked 不伪造提交/PR/上线成功；
  批准绑定具体结果（diff/报告摘要）后才允许远端动作（离线模拟端口，DE-12 语义）
- 取消传播：取消后不再执行新副作用（DE-14 语义）
- 预算耗尽 → waiting_budget 等待语义（Later），恢复后续跑且不刷失败 attempts，
  已完成步骤按检查点跳过不重跑

全部离线：合成仓库用 tmp 目录构造；测试运行器经 subprocess -I 真实执行合成检查
脚本（不触网）；不调用付费模型；不依赖 git 远端。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents import subagent, tools
from evoharness.governance import budget
from evoharness.models import Base, Task, TaskStatus
from evoharness.work import execution, service, supplements
from evoharness.work.models import Artifact, ExecutionRun
from evoharness.workflows import code_fix

# ── 合成仓库内容 ──────────────────────────────────────────────────

BUGGY_APP = '''"""合成业务模块：按 event id 去重。"""


def dedup(events):
    # 缺陷：重复事件未去重，重复投递会产生重复待批
    out = []
    for event in events:
        out.append(event)
    return out
'''

FIXED_APP = '''"""合成业务模块：按 event id 去重。"""


def dedup(events):
    # 修复：按 id 去重，同一事件重放只产生一个有效结果
    out = []
    seen = set()
    for event in events:
        if event["id"] in seen:
            continue
        seen.add(event["id"])
        out.append(event)
    return out
'''

PARTIAL_APP = BUGGY_APP.replace("# 缺陷：重复事件未去重，重复投递会产生重复待批",
                                "# 声称已修复（仅改注释，缺陷仍在）")

RUN_CHECKS = '''"""合成仓库检查脚本：核心行为检查 + tests/test_*.py 全量执行（离线 mini runner）。"""
import glob
import importlib.util
import os
import sys

sys.path.insert(0, os.getcwd())
failures = []

from app import dedup

ids = [e["id"] for e in dedup([{"id": "e1"}, {"id": "e1"}, {"id": "e2"}])]
if len(ids) != len(set(ids)):
    failures.append("core: duplicate events remain: %r" % (ids,))

for path in sorted(glob.glob(os.path.join("tests", "test_*.py"))):
    spec = importlib.util.spec_from_file_location(
        path[:-3].replace(os.sep, "_").replace("/", "_"), path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001
        failures.append("%s: import error %r" % (path, exc))
        continue
    for name in sorted(n for n in dir(module) if n.startswith("test_")):
        try:
            getattr(module, name)()
        except Exception as exc:  # noqa: BLE001
            failures.append("%s:%s: %s: %s" % (path, name, type(exc).__name__, exc))

if failures:
    print("FAIL")
    for line in failures:
        print(" -", line)
    sys.exit(1)
print("PASS all checks")
sys.exit(0)
'''

REGRESSION_TEST = '''"""回归：同一事件重复投递只产生一个有效结果。"""
from app import dedup

def test_duplicate_event_dedup():
    result = dedup([{"id": "a"}, {"id": "a"}, {"id": "b"}])
    assert [e["id"] for e in result] == ["a", "b"]
'''


# ── 离线端口 ──────────────────────────────────────────────────────


def _allow(session, context, action):
    return True


def _script_runner() -> code_fix.ScriptTestRunner:
    return code_fix.ScriptTestRunner([sys.executable, "-I", "run_checks.py"],
                                     timeout_seconds=30.0)


class SyntheticFixEngine:
    """合成修复引擎：按阶段真实写文件（经 guard），记录被调用阶段供断言。"""

    kind = "synthetic-fix"

    def __init__(self, *, tokens_used=40, fix_mode="full", weaken_tests=False,
                 regression_path="tests/test_dup_events.py", on_phase=None):
        self.calls = []
        self.tokens_used = tokens_used
        self.fix_mode = fix_mode            # full / partial（假完成）/ none（零变更）
        self.weaken_tests = weaken_tests
        self.regression_path = regression_path
        self.on_phase = on_phase

    def run(self, phase, workdir, context, guard):
        self.calls.append(phase)
        if self.on_phase is not None:
            self.on_phase(phase)
        if phase == "reproduce":
            return code_fix.PhaseOutcome(ok=True, summary="重现脚本就绪", tokens_used=5)
        if phase == "impact":
            return code_fix.PhaseOutcome(
                ok=True, tokens_used=self.tokens_used,
                summary="影响：dedup 是事件去重唯一入口；重复投递会产生重复待批草稿",
                todo=["交付后观察重复事件指标"])
        if phase == "regression":
            target = Path(guard(str(Path(workdir) / self.regression_path)))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(REGRESSION_TEST, encoding="utf-8")
            return code_fix.PhaseOutcome(ok=True, tokens_used=self.tokens_used,
                                         summary="新增回归：重复事件只保留一个",
                                         paths=[self.regression_path])
        if phase == "fix":
            if self.fix_mode == "none":
                return code_fix.PhaseOutcome(ok=True, claimed_done=True,
                                             summary="声称已修复", tokens_used=self.tokens_used)
            app = Path(guard(str(Path(workdir) / "app.py")))
            app.write_text(PARTIAL_APP if self.fix_mode == "partial" else FIXED_APP,
                           encoding="utf-8")
            if self.weaken_tests:
                test_file = Path(guard(str(Path(workdir) / self.regression_path)))
                test_file.write_text(
                    REGRESSION_TEST.replace(
                        'assert [e["id"] for e in result] == ["a", "b"]', "assert result"),
                    encoding="utf-8")
            return code_fix.PhaseOutcome(ok=True, claimed_done=True,
                                         tokens_used=self.tokens_used,
                                         summary="最小修复：dedup 按 id 去重")
        if phase == "gate":
            return code_fix.PhaseOutcome(ok=True, summary="门禁准备完成", tokens_used=5)
        if phase == "deliver":
            return code_fix.PhaseOutcome(ok=True, summary="交付整理完成", tokens_used=5,
                                         todo=["剩余风险：其他聚合入口未审计"])
        return code_fix.PhaseOutcome(ok=False, error=f"unknown_phase:{phase}")


class RecordingPort:
    """离线远端端口：记录 publish 载荷；可注入回执或异常。"""

    kind = "fake-remote"

    def __init__(self, receipt=None, exc=None):
        self.calls = []
        self.receipt = receipt
        self.exc = exc

    def publish(self, payload):
        self.calls.append(payload)
        if self.exc is not None:
            raise self.exc
        return self.receipt or {"ok": True, "kind": "commit", "ref": "local/fix-1",
                                "detail": "offline simulated receipt"}


# ── 环境工厂 ──────────────────────────────────────────────────────

_ENVS: list["Env"] = []


@pytest.fixture(autouse=True)
def _dispose_env_engines():
    """测试结束释放内存库连接：不向进程留下未关闭的 sqlite 连接（防 ResourceWarning 污染）。"""
    yield
    for env in _ENVS:
        try:
            env.s.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            env.engine.dispose()
        except Exception:  # noqa: BLE001
            pass
    _ENVS.clear()


class Env:
    """内存库 + 合成缺陷仓库；new_task_run 可为同一环境派生多个独立 Run。"""

    def __init__(self, tmp_path: Path, name: str):
        _ENVS.append(self)
        self.tmp = tmp_path / name
        self.tmp.mkdir(parents=True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        (self.repo / "app.py").write_text(BUGGY_APP, encoding="utf-8")
        (self.repo / "run_checks.py").write_text(RUN_CHECKS, encoding="utf-8")
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_existing.py").write_text(
            "def test_baseline():\n    assert True\n", encoding="utf-8")
        self.out = self.tmp / "out"
        self.engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.s = self.factory()
        self.counter = 0

    def new_task_run(self, *, space_id: str = ""):
        self.counter += 1
        s = self.s
        task = Task(task_key=f"k-{self.counter}", tenant="t", owner="alice", chat_id="chat",
                    title="修复重复事件", action="fix", status=TaskStatus.OPEN,
                    workflow_kind="delegated_delivery",
                    goal="修复重复事件导致重复待批，保留现有审批行为",
                    acceptance_json='[{"kind": "code", "criteria": "同一事件重放只产生一个有效草稿；完整回归通过"}]',
                    revision=1, assignee_id="bob", org_id="", space_id=space_id)
        s.add(task)
        s.flush()
        run = execution.request_run(s, task_id=task.id,
                                    context={"defect": "duplicate-events"},
                                    actor_id="runner-1", output_root=str(self.out),
                                    idempotency_key=f"i-{self.counter}",
                                    validate_context=_allow)
        s.flush()
        service.transition_to(s, task.id, to_state="queued", actor="bob")
        return task, run

    def run_state(self, run) -> str:
        return self.s.get(ExecutionRun, run.id).state

    def task_state(self, task) -> str:
        return self.s.get(Task, task.id).status


# ── 场景 1：重复事件缺陷 → 自动修复 + 先红后绿 ────────────────────


def test_duplicate_event_defect_red_then_green_delivery(tmp_path):
    env = Env(tmp_path, "happy")
    task, run = env.new_task_run()
    engine = SyntheticFixEngine()
    result = code_fix.run_code_fix(env.s, run.id, engine=engine, runner=_script_runner(),
                                   worker="w1", source_tree=env.repo,
                                   validate_context=_allow)
    assert result["ok"] is True and result["status"] == "waiting_acceptance"
    assert env.task_state(task) == "waiting_acceptance"   # Run 成功 ≠ 业务 done
    assert env.task_state(task) != TaskStatus.DONE
    assert engine.calls == list(code_fix.PHASES)

    # 先红后绿：两阶段真实证据落盘且进结果
    workdir = Path(result["workspace"])
    red_text = (workdir / "evidence" / "regression-red.txt").read_text(encoding="utf-8")
    assert "FAIL" in red_text and "duplicate" in red_text
    green_text = (workdir / "evidence" / "gate-green.txt").read_text(encoding="utf-8")
    assert "PASS all checks" in green_text
    assert result["red"]["ok"] is False and result["red"]["exit_code"] == 1
    assert result["green"]["ok"] is True and result["green"]["exit_code"] == 0

    # 修复落在隔离工作区；源仓库不被触碰（不触碰其他工作区未提交修改）
    assert "seen" in (workdir / "app.py").read_text(encoding="utf-8")
    assert (env.repo / "app.py").read_text(encoding="utf-8") == BUGGY_APP
    assert (env.repo / "tests" / "test_existing.py").is_file()

    # 差异/风险交付：patch + 测试报告登记为本地产物
    assert set(result["artifacts"]) == {"diff", "test_report"}
    patch = (workdir / "artifacts" / "fix.patch").read_text(encoding="utf-8")
    assert "--- a/app.py" in patch and "+++ b/app.py" in patch
    assert "-    # 缺陷：重复事件未去重" in patch
    assert '+        if event["id"] in seen:' in patch
    report = (workdir / "artifacts" / "test-report.md").read_text(encoding="utf-8")
    assert "PASS all checks" in report and "剩余风险" in report
    kinds = {a.kind for a in env.s.query(Artifact).all()}
    assert kinds == {"diff", "test_report"}

    # 每步结构化 checkpoint：已完成证据/未完成项/下一动作/待对账副作用
    ledger = sorted((workdir / "checkpoints").glob("*.json"))
    assert {p.name.split("-", 1)[1][:-5] for p in ledger} >= set(code_fix.PHASES)
    for path in ledger:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert {"phase", "status", "done", "todo", "next", "side_effects"} <= set(data)
    final = json.loads(ledger[-1].read_text(encoding="utf-8"))
    assert final["status"] == "waiting_acceptance" and final["next"]
    assert any(se.startswith("fix_applied:") for se in final["side_effects"])

    # 预算归集（DE-15）：code_fix 类别有真实结算记录
    summary = budget.usage_summary(env.s)
    assert summary["by_category"].get("code_fix", 0) > 0
    assert env.run_state(run) == "waiting_acceptance"


# ── 场景 2：失败/超时/模型假完成 ─────────────────────────────────


def test_reproduce_timeout_records_evidence_budget_and_next_action(tmp_path):
    env = Env(tmp_path, "timeout")
    task, run = env.new_task_run()
    engine = SyntheticFixEngine()
    slow = code_fix.ScriptTestRunner(
        [sys.executable, "-I", "-c", "import time; time.sleep(3); print('PASS all checks')"],
        timeout_seconds=0.5)
    result = code_fix.run_code_fix(env.s, run.id, engine=engine, runner=slow,
                                   worker="w1", source_tree=env.repo,
                                   validate_context=_allow)
    assert result["ok"] is False and result["status"] == "failed"
    assert "timeout" in result["error"]
    # 真实失败证据落盘
    evidence = Path(result["failure_evidence"]).read_text(encoding="utf-8")
    assert "timeout" in evidence and "reproduce" in evidence
    # 下一动作明确
    assert result["next"]
    cp = json.loads(Path(result["checkpoint"]).read_text(encoding="utf-8"))
    assert cp["status"] == "failed" and cp["next"] and cp["todo"]
    # 预算消耗如实归集（未知用量保守全额），失败可见
    summary = budget.usage_summary(env.s)
    assert summary["failed_settled"] >= 1
    assert env.run_state(run) == "failed"
    assert env.task_state(task) == "failed"


def test_claimed_done_with_red_gate_is_fake_completion(tmp_path):
    env = Env(tmp_path, "fake")
    task, run = env.new_task_run()
    engine = SyntheticFixEngine(fix_mode="partial")   # 只改注释仍声称修完
    result = code_fix.run_code_fix(env.s, run.id, engine=engine, runner=_script_runner(),
                                   worker="w1", source_tree=env.repo,
                                   validate_context=_allow)
    assert result["ok"] is False and result["status"] == "failed"
    assert result["error"] == "claimed_done_but_gate_red"
    # 模型假完成被判失败：真实失败输出摘要落盘
    evidence = Path(result["failure_evidence"]).read_text(encoding="utf-8")
    assert "FAIL" in evidence and "duplicate" in evidence
    assert budget.usage_summary(env.s)["failed_settled"] >= 1
    cp = json.loads(Path(result["checkpoint"]).read_text(encoding="utf-8"))
    assert "修复" in cp["next"] or "返工" in cp["next"]
    assert env.run_state(run) == "failed"
    assert env.task_state(task) == "failed"


def test_engine_zero_change_is_rejected_before_gate(tmp_path):
    env = Env(tmp_path, "nofix")
    task, run = env.new_task_run()
    engine = SyntheticFixEngine(fix_mode="none")
    result = code_fix.run_code_fix(env.s, run.id, engine=engine, runner=_script_runner(),
                                   worker="w1", source_tree=env.repo,
                                   validate_context=_allow)
    assert result["ok"] is False and result["error"] == "no_fix_applied"
    assert "gate" not in engine.calls   # 未产生修复不得进入门禁


def test_defect_not_reproduced_stops_before_fix(tmp_path):
    env = Env(tmp_path, "norepro")
    task, run = env.new_task_run()
    (env.repo / "app.py").write_text(FIXED_APP, encoding="utf-8")   # 无缺陷副本
    engine = SyntheticFixEngine()
    result = code_fix.run_code_fix(env.s, run.id, engine=engine, runner=_script_runner(),
                                   worker="w1", source_tree=env.repo,
                                   validate_context=_allow)
    # 无法重现是 SOP 停止/转人工条件：不进入修复，失败可见并带下一动作
    assert result["ok"] is False and result["error"] == "defect_not_reproduced"
    assert engine.calls == ["reproduce"]
    assert "PASS all checks" in Path(result["failure_evidence"]).read_text(encoding="utf-8")
    assert env.task_state(task) == "failed"


# ── 场景 3：删测试凑绿被拒 ────────────────────────────────────────


def test_fix_deleting_test_assertions_is_rejected(tmp_path):
    env = Env(tmp_path, "weaken")
    task, run = env.new_task_run()
    engine = SyntheticFixEngine(weaken_tests=True)
    port = RecordingPort()
    result = code_fix.run_code_fix(env.s, run.id, engine=engine, runner=_script_runner(),
                                   worker="w1", source_tree=env.repo,
                                   validate_context=_allow, remote=port)
    assert result["ok"] is False and result["status"] == "failed"
    assert result["error"] == "test_weakening_rejected"
    # 违规明细进入检查点：被删除断言的测试文件与内容可见
    cp = json.loads(Path(result["checkpoint"]).read_text(encoding="utf-8"))
    assert any("test_dup_events" in item and "assert" in item for item in cp["todo"])
    evidence = Path(result["failure_evidence"]).read_text(encoding="utf-8")
    assert "test_weakening_rejected" in evidence
    # 远端动作从未被尝试；无交付产物登记
    assert port.calls == []
    assert env.s.query(Artifact).count() == 0
    assert env.run_state(run) == "failed"
    assert env.task_state(task) == "failed"


def test_fix_deleting_whole_test_file_is_rejected(tmp_path):
    env = Env(tmp_path, "deltest")
    task, run = env.new_task_run()

    class TestDeleter(SyntheticFixEngine):
        def run(self, phase, workdir, context, guard):
            if phase == "fix":
                self.calls.append(phase)
                target = Path(guard(str(Path(workdir) / self.regression_path)))
                target.unlink()
                app = Path(guard(str(Path(workdir) / "app.py")))
                app.write_text(FIXED_APP, encoding="utf-8")
                return code_fix.PhaseOutcome(ok=True, claimed_done=True,
                                             tokens_used=self.tokens_used,
                                             summary="删除了干扰测试后修复")
            return super().run(phase, workdir, context, guard)

    engine = TestDeleter()
    result = code_fix.run_code_fix(env.s, run.id, engine=engine, runner=_script_runner(),
                                   worker="w1", source_tree=env.repo,
                                   validate_context=_allow)
    assert result["ok"] is False and result["error"] == "test_weakening_rejected"
    cp = json.loads(Path(result["checkpoint"]).read_text(encoding="utf-8"))
    assert any("test file deleted" in item for item in cp["todo"])


# ── 场景 4：远端边界与批准绑定（DE-12） ──────────────────────────


def _bound_approval(first_result) -> code_fix.DeliveryApproval:
    return code_fix.DeliveryApproval(approver="alice", target="review/DE-demo",
                                     diff_sha256=first_result["diff_sha256"],
                                     report_sha256=first_result["report_sha256"])


def test_no_remote_authorization_produces_local_artifacts_only(tmp_path):
    env = Env(tmp_path, "localonly")
    task, run = env.new_task_run()
    result = code_fix.run_code_fix(env.s, run.id, engine=SyntheticFixEngine(),
                                   runner=_script_runner(), worker="w1",
                                   source_tree=env.repo, validate_context=_allow)
    assert result["ok"] is True
    assert result["remote"] == "local_only_not_authorized"
    assert result["receipt"] is None
    assert not any(se.startswith("remote_publish") for se in result["side_effects"])
    # 无任何提交/PR/上线成功宣称
    dumped = json.dumps(result, ensure_ascii=False)
    assert "published:" not in dumped
    workdir = Path(result["workspace"])
    assert (workdir / "artifacts" / "fix.patch").is_file()
    assert (workdir / "artifacts" / "test-report.md").is_file()
    assert env.run_state(run) == "waiting_acceptance"


def test_remote_port_without_bound_approval_never_publishes(tmp_path):
    env = Env(tmp_path, "noapproval")
    task, run = env.new_task_run()
    port = RecordingPort()
    result = code_fix.run_code_fix(env.s, run.id, engine=SyntheticFixEngine(),
                                   runner=_script_runner(), worker="w1",
                                   source_tree=env.repo, validate_context=_allow,
                                   remote=port)
    assert port.calls == []            # 未绑定批准：连发布载荷都不会构造
    assert result["remote"] == "blocked_approval_required"
    assert result["status"] == "waiting_authorization"
    assert env.run_state(run) == "waiting_authorization"
    assert env.task_state(task) == "waiting_authorization"
    # 本地产物仍可审阅
    assert set(result["artifacts"]) == {"diff", "test_report"}


def test_missing_remote_credentials_blocked_not_faked(tmp_path):
    env = Env(tmp_path, "creds")
    task, run = env.new_task_run()
    first = code_fix.run_code_fix(env.s, run.id, engine=SyntheticFixEngine(),
                                  runner=_script_runner(), worker="w1",
                                  source_tree=env.repo, validate_context=_allow)
    approval = _bound_approval(first)
    task2, run2 = env.new_task_run()
    port = RecordingPort(exc=code_fix.RemoteCredentialsMissing("no stored token for remote"))
    result = code_fix.run_code_fix(env.s, run2.id, engine=SyntheticFixEngine(),
                                   runner=_script_runner(), worker="w2",
                                   source_tree=env.repo, validate_context=_allow,
                                   remote=port, approval=approval)
    assert len(port.calls) == 1        # 绑定批准后确实尝试了发布
    assert result["remote"] == "blocked_credentials_missing"
    assert result["receipt"] is None   # 不伪造提交/PR/上线成功
    assert result["status"] == "waiting_authorization"
    assert env.run_state(run2) == "waiting_authorization"
    assert env.task_state(task2) == "waiting_authorization"
    assert env.s.get(Task, task2.id).waiting_reason
    assert "published:" not in json.dumps(result, ensure_ascii=False)


def test_bound_approval_authorizes_publish_with_exact_payload(tmp_path):
    env = Env(tmp_path, "publish")
    task, run = env.new_task_run()
    first = code_fix.run_code_fix(env.s, run.id, engine=SyntheticFixEngine(),
                                  runner=_script_runner(), worker="w1",
                                  source_tree=env.repo, validate_context=_allow)
    approval = _bound_approval(first)
    task2, run2 = env.new_task_run()
    port = RecordingPort()
    result = code_fix.run_code_fix(env.s, run2.id, engine=SyntheticFixEngine(),
                                   runner=_script_runner(), worker="w2",
                                   source_tree=env.repo, validate_context=_allow,
                                   remote=port, approval=approval)
    assert result["ok"] is True and result["remote"].startswith("published:")
    assert result["receipt"]["ref"] == "local/fix-1"
    assert len(port.calls) == 1
    payload = port.calls[0]
    # 批准绑定具体结果：发布载荷的摘要与批准一致，目标即批准目标
    assert payload.diff_sha256 == approval.diff_sha256
    assert payload.report_sha256 == approval.report_sha256
    assert payload.target == "review/DE-demo"
    assert any(se.startswith("remote_publish:commit:") for se in result["side_effects"])
    assert env.run_state(run2) == "waiting_acceptance"


def test_approval_not_bound_to_actual_result_is_rejected(tmp_path):
    env = Env(tmp_path, "mismatch")
    task, run = env.new_task_run()
    port = RecordingPort()
    approval = code_fix.DeliveryApproval(approver="alice", target="review/other",
                                         diff_sha256="0" * 64, report_sha256="0" * 64)
    result = code_fix.run_code_fix(env.s, run.id, engine=SyntheticFixEngine(),
                                   runner=_script_runner(), worker="w1",
                                   source_tree=env.repo, validate_context=_allow,
                                   remote=port, approval=approval)
    assert port.calls == []            # 批准与本次 diff/报告不匹配：不得发布
    assert result["remote"] == "blocked_approval_not_bound_to_result"
    assert result["status"] == "waiting_authorization"
    assert env.run_state(run) == "waiting_authorization"


# ── 场景 5：取消传播（DE-14） ────────────────────────────────────


def test_cancellation_stops_new_side_effects(tmp_path):
    env = Env(tmp_path, "cancel")
    task, run = env.new_task_run()

    def enqueue_cancel(phase):
        if phase == "regression":
            supplements.enqueue(env.s, task.id, event_id="cancel-1",
                                event_kind="cancellation", payload={"reason": "需求撤回"})

    engine = SyntheticFixEngine(on_phase=enqueue_cancel)
    result = code_fix.run_code_fix(env.s, run.id, engine=engine, runner=_script_runner(),
                                   worker="w1", source_tree=env.repo,
                                   validate_context=_allow)
    assert result["ok"] is False and result["status"] == "cancelled"
    # 取消后不再执行新副作用：fix/gate/deliver 均未运行
    assert engine.calls == ["reproduce", "impact", "regression"]
    workdir = Path(result["workspace"])
    assert (workdir / "app.py").read_text(encoding="utf-8") == BUGGY_APP   # 修复未应用
    assert not (workdir / "artifacts").exists()                            # 未产生交付产物
    assert not any(se.startswith("fix_applied") for se in result["side_effects"])
    assert env.task_state(task) == "cancelled"
    assert env.run_state(run) == "paused"
    assert not supplements.has_pending_cancellation(env.s, task.id)        # 取消事件消费留痕
    cp = json.loads(Path(result["checkpoint"]).read_text(encoding="utf-8"))
    assert cp["status"] == "cancelled" and cp["phase"] == "fix"
    assert cp["cancelled_events"] == ["cancel-1"]
    # 红证据保留（已发生步骤的产物不回滚，供对账）
    assert (workdir / "evidence" / "regression-red.txt").is_file()


# ── 场景 6：预算耗尽 → 等待语义（DE-15 Later） ───────────────────


def test_budget_exhausted_waits_then_resumes_after_topup(tmp_path):
    env = Env(tmp_path, "budget")
    budget.set_limit(env.s, scope_kind="space", scope_id="s1", limit_tokens=120)
    task, run = env.new_task_run(space_id="s1")
    engine = SyntheticFixEngine(tokens_used=40)
    result = code_fix.run_code_fix(env.s, run.id, engine=engine, runner=_script_runner(),
                                   worker="w1", source_tree=env.repo,
                                   validate_context=_allow)
    # 等待语义：不计失败、不刷 attempts，引擎一步都没执行
    assert result["ok"] is False and result["status"] == "waiting_budget"
    assert env.run_state(run) == "waiting_budget"
    assert env.task_state(task) == "waiting_budget"
    assert env.run_state(run) != "failed"
    assert env.s.get(ExecutionRun, run.id).attempts == 1
    assert engine.calls == []
    cp = json.loads(Path(result["checkpoint"]).read_text(encoding="utf-8"))
    assert cp["budget_error"].startswith("budget_exhausted")
    assert "唤醒" in cp["next"]

    # 人工补额度后等待 Run 可继续
    budget.set_limit(env.s, scope_kind="space", scope_id="s1", limit_tokens=100000)
    result2 = code_fix.run_code_fix(env.s, run.id, engine=engine, runner=_script_runner(),
                                    worker="w1", source_tree=env.repo,
                                    validate_context=_allow)
    assert result2["ok"] is True and result2["status"] == "waiting_acceptance"
    assert result2["red"]["ok"] is False and result2["green"]["ok"] is True
    assert env.s.get(ExecutionRun, run.id).attempts == 2   # 仅恢复续跑一次，无失败刷 Attempts
    assert env.task_state(task) == "waiting_acceptance"


def test_resume_after_budget_wait_skips_completed_phases(tmp_path):
    env = Env(tmp_path, "resumeskip")
    budget.set_limit(env.s, scope_kind="space", scope_id="s1", limit_tokens=3600)
    task, run = env.new_task_run(space_id="s1")
    engine = SyntheticFixEngine(tokens_used=900)
    result = code_fix.run_code_fix(env.s, run.id, engine=engine, runner=_script_runner(),
                                   worker="w1", source_tree=env.repo,
                                   validate_context=_allow)
    assert result["status"] == "waiting_budget"
    assert engine.calls == ["reproduce", "impact", "regression", "fix"]

    budget.set_limit(env.s, scope_kind="space", scope_id="s1", limit_tokens=100000)
    engine2 = SyntheticFixEngine(tokens_used=900)
    result2 = code_fix.run_code_fix(env.s, run.id, engine=engine2, runner=_script_runner(),
                                    worker="w1", source_tree=env.repo,
                                    validate_context=_allow)
    # 已完成步骤按检查点跳过不重跑：恢复的引擎只跑剩余两步
    assert engine2.calls == ["gate", "deliver"]
    assert result2["ok"] is True
    assert result2["red"]["ok"] is False and result2["green"]["ok"] is True
    assert result2["diff_files"] == ["app.py"]   # 回归测试在 regression 阶段新增，fix diff 只含修复文件
    assert result2["diff_sha256"]
    assert env.run_state(run) == "waiting_acceptance"


# ── 接线回归：工具层测试写保护钩子 + SOP 引擎受限配置 ─────────────


def test_test_guard_blocks_editing_existing_test_file(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    tests_dir = repo / "tests"
    tests_dir.mkdir(parents=True)
    existing = tests_dir / "test_existing.py"
    existing.write_text("def test_baseline():\n    assert True\n", encoding="utf-8")
    monkeypatch.chdir(repo)

    tools.set_active_test_guard([tests_dir])
    try:
        # 既有测试文件：edit/覆盖一律 deny，且 bypassPermissions 也不可绕过
        assert tools.check_permission("edit_file", {"file_path": str(existing)},
                                      mode="bypassPermissions")["action"] == "deny"
        assert tools.check_permission("write_file", {"file_path": str(existing)},
                                      mode="acceptEdits")["action"] == "deny"
        # 新增回归测试文件（尚不存在）：放行——先红后绿要求引擎能写新测试
        assert tools.check_permission("write_file",
                                      {"file_path": str(tests_dir / "test_regression_new.py")},
                                      mode="acceptEdits")["action"] == "allow"
        # 非测试文件不受影响
        assert tools.check_permission("edit_file", {"file_path": str(repo / "app.py")},
                                      mode="acceptEdits")["action"] == "allow"
    finally:
        tools.clear_active_test_guard()
    # 未激活时零行为变化
    assert tools.check_permission("edit_file", {"file_path": str(existing)},
                                  mode="acceptEdits")["action"] == "allow"


def test_code_fix_engine_config_is_restricted():
    config = subagent.get_code_fix_engine_config("修复 dedup 缺陷：先写回归，再最小修复")
    names = {t["name"] for t in config["tools"]}
    assert names == set(subagent.CODE_FIX_ENGINE_TOOLS)
    assert "agent" not in names            # 禁递归创建子代理
    assert "skill_evolve" not in names and "skill_create" not in names
    assert {"read_file", "write_file", "edit_file", "run_shell", "grep_search"} <= names
    assert config["system_prompt"].startswith("修复 dedup")
    default = subagent.get_code_fix_engine_config()
    assert default["system_prompt"] == subagent.GENERAL_PROMPT
