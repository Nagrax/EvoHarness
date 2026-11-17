"""统一调用包装器的预算网关（R03）：预估 → 原子预留 → 受保护调用 → 真实 usage 结算。

审核定位：组织路径的模型调用此前没有统一预算入口——付费路径绕过预算直接调
模型，排队成功不等于"花出去的每一笔都可对账"。本模块是缺失的装配层：

- **预估**：``estimate``（注入式估算端口；缺省按保守固定值）；
- **原子预留**：``reserve`` 直通 governance.budget.reserve（R04：单条原子条件
  INSERT，限额校验与预留同语句完成，org/space/principal 各层级放行才落账）；
- **受保护调用**：``invoke`` 在预算记录上下文内执行真实调用，并按真实 usage
  ``settle`` 结算；异常/崩溃按保守口径全额归集（``ok=False``），不留免费副作用；
- **契约门禁（fail-closed）**：付费端口发起调用前必须 ``require_record``——
  当前上下文没有网关签发的预算记录即 ``NoBudgetRecord`` 拒绝执行。没有预算
  记录的付费调用不存在"先调用后补账"的通道。

装配点（本轮已接）：
- DE-20 code_fix 引擎端口：``evoharness/workflows/code_fix.py`` 每阶段预留后
  在 ``record_context`` 内调用引擎/测试运行器；
- 生产 Worker + 受限 Agent 适配器：``evoharness/work/worker.py`` /
  ``evoharness/work/agent_adapter.py``（每次模型调用一笔预留+结算）；
- integration/iterate.py 主循环入口层（显式开启时，见 R03 实施记录剩余接线清单）。

离线确定性：网关只依赖 Session 工厂与注入端口，测试用内存 SQLite + 脚本化
端口即可覆盖全部契约（无预算记录拒绝 / 有预算正常 / 预留不足失败 / 结算后
账目一致），不触网、不消耗费用。
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import update

from evoharness.governance import budget
from evoharness.governance.budget import BudgetError
from evoharness.governance.models import BudgetReservation

# 单次模型调用的保守预估（真实装配注入 estimator 覆盖）
DEFAULT_CALL_TOKENS = 1000


class NoBudgetRecord(PermissionError):
    """契约门禁拒绝：付费调用没有预算记录（fail-closed，不可用 try/except 绕过）。"""

    def __init__(self, detail: str = ""):
        self.code = "no_budget_record"
        self.detail = detail
        super().__init__(f"no_budget_record: {detail}" if detail else "no_budget_record")


@dataclass(frozen=True)
class BudgetRecord:
    """网关签发的活跃预算记录：调用期间经 ContextVar 传播（付费端口凭此放行）。"""

    reservation_id: str
    org_id: str = ""
    space_id: str = ""
    principal_id: str = ""
    work_item_id: int = 0
    run_id: str = ""
    category: str = ""
    reserved_tokens: int = 0


_current_record: ContextVar = ContextVar("budget_gateway_record", default=None)


def current_record() -> BudgetRecord | None:
    """当前上下文的预算记录（观测口径；判定一律走 require_record）。"""
    return _current_record.get()


def require_record(*, category: str | None = None) -> BudgetRecord:
    """付费端口调用前的契约门禁：无预算记录即 NoBudgetRecord（fail-closed）。

    - category 给定时还要求记录类别一致（模型调用的记录不能挪给 embedding 用）；
    - 记录由网关 reserve 签发，签名不可伪造（构造 BudgetRecord 不经 ContextVar
      传播到付费端口上下文之外）。
    """
    record = _current_record.get()
    if record is None:
        raise NoBudgetRecord(detail=f"paid call without budget reservation "
                                    f"(category={category or '?'})")
    if category is not None and record.category != category:
        raise NoBudgetRecord(
            detail=f"budget record category mismatch: got {record.category!r}, need {category!r}")
    return record


@contextmanager
def record_context(record: BudgetRecord | None):
    """把预算记录绑定到当前调用上下文（网关 invoke 内部使用；装配层复用同一语义）。"""
    token = _current_record.set(record)
    try:
        yield record
    finally:
        _current_record.reset(token)


def _default_usage_of(result: Any) -> int:
    """默认 usage 提取：``result.usage_tokens`` 属性或 ``result["usage_tokens"]`` 键。"""
    if result is None:
        return 0
    value = getattr(result, "usage_tokens", None)
    if value is None and isinstance(result, dict):
        value = result.get("usage_tokens")
    return int(value) if value is not None else 0


@dataclass
class GatewayResult:
    """一次受保护调用的完整账目：结果、真实 usage、预留 id 与结算状态。"""

    ok: bool
    result: Any = None
    used_tokens: int = 0
    reserved_tokens: int = 0
    reservation_id: str = ""
    error: str = ""
    error_code: str = ""
    settled: bool = False


class BudgetGateway:
    """组织路径付费调用的统一包装器：预估→原子预留→调用→真实 usage 结算。

    - 每次调用一笔预留（并发原子，R4 语义直通 budget.reserve），结算恰好一次
      （budget.settle 条件 UPDATE）；失败/异常按保守口径全额归集（ok=False），
      不留免费副作用；
    - 调用执行在 ``record_context`` 内：网关感知的付费端口凭 require_record 放行，
      绕过网关的直接调用 fail-closed 拒绝；
    - session_factory 注入：预留/结算各走独立短事务，调用期间不占会话
      （长模型调用不阻塞其他写入）。
    """

    def __init__(self, session_factory, *, estimator: Callable[..., int] | None = None,
                 usage_of: Callable[[Any], int] | None = None,
                 default_category: str = "model_call",
                 default_tokens: int = DEFAULT_CALL_TOKENS):
        self._session_factory = session_factory
        self._estimator = estimator
        self._usage_of = usage_of
        self.default_category = str(default_category)
        self.default_tokens = int(default_tokens)
        # 观测口径：本网关实例累计调用次数与结算用量（账目真源在 budget_reservations）
        self.calls = 0
        self.last_usage_tokens = 0

    @property
    def session_factory(self):
        """注入的会话工厂（装配层/适配器复用同一来源，不另造连接）。"""
        return self._session_factory

    # ── 预估 ────────────────────────────────────────────────────────────────

    def estimate(self, *, category: str = "", context: Any = None) -> int:
        if self._estimator is None:
            return self.default_tokens
        value = int(self._estimator(category=category or self.default_category,
                                    context=context))
        return max(1, value)

    # ── 原子预留（R04 语义直通） ─────────────────────────────────────────────

    def reserve(self, *, org_id: str = "", space_id: str = "", principal_id: str = "",
                work_item_id: int = 0, run_id: str = "", category: str | None = None,
                tokens: int | None = None, session=None, call_id: str = "",
                generation: int = 0, grant_revision: int = 0,
                request_hash: str = "") -> BudgetRecord:
        """提交预算预留意图；任一已配置层级余量不足/未配置即 BudgetError（不落账）。

        session 给定时预留并入调用方事务（DE-23/§4 既有调用方兼容）；
        否则走独立短事务——Run 执行期的调用预留/回执各占一个短事务并立即
        提交（N02）：外部调用开始前动作意图已持久可对账，崩溃不留空账。
        """
        resolved_category = str(category) if category else self.default_category
        resolved_tokens = int(tokens) if tokens is not None else self.estimate(
            category=resolved_category)
        if session is not None:
            row = budget.reserve(session, org_id=str(org_id or ""),
                                 space_id=str(space_id or ""),
                                 principal_id=str(principal_id or ""),
                                 work_item_id=int(work_item_id or 0),
                                 run_id=str(run_id or ""),
                                 category=resolved_category,
                                 tokens=max(1, resolved_tokens), estimated=True,
                                 kind="call", call_id=str(call_id or ""),
                                 generation=int(generation or 0),
                                 grant_revision=int(grant_revision or 0),
                                 request_hash=str(request_hash or ""))
            session.flush()
            return self._record_of(row)
        with self._session_factory.begin() as scoped:
            row = budget.reserve(scoped, org_id=str(org_id or ""),
                                 space_id=str(space_id or ""),
                                 principal_id=str(principal_id or ""),
                                 work_item_id=int(work_item_id or 0),
                                 run_id=str(run_id or ""),
                                 category=resolved_category,
                                 tokens=max(1, resolved_tokens), estimated=True,
                                 kind="call", call_id=str(call_id or ""),
                                 generation=int(generation or 0),
                                 grant_revision=int(grant_revision or 0),
                                 request_hash=str(request_hash or ""))
            return self._record_of(row)

    @staticmethod
    def _record_of(row) -> BudgetRecord:
        return BudgetRecord(reservation_id=row.reservation_id,
                            org_id=row.org_id, space_id=row.space_id,
                            principal_id=row.principal_id,
                            work_item_id=int(row.work_item_id or 0),
                            run_id=row.run_id, category=row.category,
                            reserved_tokens=int(row.reserved_tokens))

    # ── 结算 / 释放 ─────────────────────────────────────────────────────────

    def settle(self, record: BudgetRecord, *, used_tokens: int, ok: bool = True,
               session=None, usage_status: str | None = None,
               intent_status: str | None = None, outcome: str | None = None) -> None:
        """按真实 usage 结算（恰好一次；重复结算被 budget.settle 条件更新拒绝）。"""
        if session is not None:
            budget.settle(session, record.reservation_id,
                          used_tokens=max(0, int(used_tokens)), ok=bool(ok),
                          usage_status=usage_status, intent_status=intent_status,
                          outcome=outcome)
            session.flush()
            return
        with self._session_factory.begin() as scoped:
            budget.settle(scoped, record.reservation_id,
                          used_tokens=max(0, int(used_tokens)), ok=bool(ok),
                          usage_status=usage_status, intent_status=intent_status,
                          outcome=outcome)

    def release(self, record: BudgetRecord, session=None) -> None:
        """取消未执行调用：释放不泄漏、不可重复。"""
        if session is not None:
            budget.release(session, record.reservation_id)
            session.flush()
            return
        with self._session_factory.begin() as scoped:
            budget.release(scoped, record.reservation_id)

    # ── 受保护调用 ──────────────────────────────────────────────────────────

    def _mark_executing(self, reservation_id: str, session=None) -> None:
        """动作意图推进到 executing：外部调用开始前持久留痕（独立短事务提交）。

        崩溃对账据此区分「未执行（committed→可释放）」与「已开始、结果未知
        （executing→保守归集）」，不回滚成未发生、不盲重试（N02）。
        """
        stmt = update(BudgetReservation.__table__).where(
            BudgetReservation.__table__.c.reservation_id == str(reservation_id),
            BudgetReservation.__table__.c.status == "reserved").values(
            intent_status="executing")
        if session is not None:
            session.execute(stmt)
            session.flush()
            return
        with self._session_factory.begin() as scoped:
            scoped.execute(stmt)

    def mark_executing(self, reservation_id: str, session=None) -> None:
        """公开薄别名（N05，向后兼容）：网关感知 SDK 端口在真实调用开始前推进
        动作意图（与 invoke 内部同一语义，供异步调用路径复用）。"""
        self._mark_executing(reservation_id, session=session)

    def invoke(self, record: BudgetRecord, fn: Callable[[], Any], *,
               usage_of: Callable[[Any], int] | None = None,
               session=None) -> GatewayResult:
        """在预算记录上下文内执行调用并结算；异常按保守口径全额归集。

        - 调用前先把动作意图推进到 executing（独立短事务，N02）；
        - 调用期间付费端口可经 require_record 取得本记录（fail-closed 门禁放行）；
        - 结算发生在调用返回之后：usage_of(result) 为真实 usage（缺省读
          result.usage_tokens），提取失败按预留全额保守归集（usage_status=
          unknown，与真实零用量 zero 区分）；
        - fn 异常不向外静默：返回 ok=False 的 GatewayResult（错误已记账），
          调用方据此决定失败/等待语义。
        """
        extractor = usage_of if usage_of is not None else (
            self._usage_of if self._usage_of is not None else _default_usage_of)
        with record_context(record):
            self._mark_executing(record.reservation_id, session)
            try:
                result = fn()
            except Exception as exc:  # 未知用量保守全额归集（与 code_fix 同口径）
                self.settle(record, used_tokens=record.reserved_tokens, ok=False,
                            session=session, usage_status="unknown",
                            intent_status="unknown", outcome="failed")
                self.last_usage_tokens += record.reserved_tokens
                return GatewayResult(ok=False, error=repr(exc),
                                     error_code=type(exc).__name__,
                                     used_tokens=record.reserved_tokens,
                                     reserved_tokens=record.reserved_tokens,
                                     reservation_id=record.reservation_id,
                                     settled=True)
        try:
            used = max(0, int(extractor(result)))
        except Exception:  # usage 提取不可信：保守全额，不产生未记账用量
            self.settle(record, used_tokens=record.reserved_tokens, ok=True,
                        session=session, usage_status="unknown",
                        intent_status="unknown", outcome="ok")
            self.last_usage_tokens += record.reserved_tokens
            return GatewayResult(ok=True, result=result, used_tokens=record.reserved_tokens,
                                 reserved_tokens=record.reserved_tokens,
                                 reservation_id=record.reservation_id, settled=True)
        usage_status = "zero" if used == 0 else "recorded"
        self.settle(record, used_tokens=used, ok=True, session=session,
                    usage_status=usage_status)
        self.last_usage_tokens += used
        return GatewayResult(ok=True, result=result, used_tokens=used,
                             reserved_tokens=record.reserved_tokens,
                             reservation_id=record.reservation_id, settled=True)

    def call(self, fn: Callable[[], Any], *, org_id: str = "", space_id: str = "",
             principal_id: str = "", work_item_id: int = 0, run_id: str = "",
             category: str | None = None, tokens: int | None = None,
             usage_of: Callable[[Any], int] | None = None,
             session=None, pre_check: Callable[[], Any] | None = None,
             call_id: str = "", grant_revision: int = 0,
             request_hash: str = "") -> GatewayResult:
        """预估→原子预留→调用→结算 的一次性入口（最常用装配形式）。

        pre_check：外部调用前的重验回调（N02：下一工具/模型调用前重验取消与
        撤权）；返回 dict 时其 generation/grant_revision 键并入意图留痕。
        """
        observed = pre_check() if pre_check is not None else None
        extra = observed if isinstance(observed, dict) else {}
        record = self.reserve(org_id=org_id, space_id=space_id, principal_id=principal_id,
                              work_item_id=work_item_id, run_id=run_id,
                              category=category, tokens=tokens, session=session,
                              call_id=str(call_id or ""),
                              generation=int(extra.get("generation") or 0),
                              grant_revision=int(extra.get("grant_revision")
                                                 or grant_revision),
                              request_hash=str(request_hash or ""))
        self.calls += 1
        result = self.invoke(record, fn, usage_of=usage_of, session=session)
        return result


def usage_summary(session, *, org_id: str = "", space_id: str = "",
                  principal_id: str = "") -> dict:
    """网关账目透传（budget.usage_summary）：对账用，不另造第二份账本。"""
    return budget.usage_summary(session, org_id=org_id, space_id=space_id,
                                principal_id=principal_id)
