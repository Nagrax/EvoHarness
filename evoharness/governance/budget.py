"""统一调用包装器的预算侧：预估预留 → 调用 → 真实 usage 结算。

没有可靠价格或 usage 时按 token 保守预留并标记 estimated；
失败、重试、取消同样结算归集，不留免费副作用。

并发原子性（R04）：限额校验与预留是**数据库内的一次原子判定**——
``INSERT ... SELECT ... WHERE 各已配置层级余量足够``，以 rowcount 判定成败；
任意两个并发事务（两个端口实例、两个进程）不可能同时通过校验。
Python 层不承担任何"先读后判再写"的决策路径（``_consumed`` 只用于报告/摘要）。
"""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import and_, exists, func, insert, literal, or_, select, update
from sqlalchemy.orm import Session

from evoharness.clock import utcnow
from evoharness.governance.models import BudgetLimit, BudgetReservation


class BudgetError(ValueError):
    def __init__(self, code: str, scope: str = ""):
        self.code = code
        self.scope = scope
        super().__init__(f"{code}:{scope}" if scope else code)


_SCOPE_COLUMNS = {
    "org": "org_id",
    "space": "space_id",
    "principal": "principal_id",
}


def set_limit(session: Session, *, scope_kind: str, scope_id: str, limit_tokens: int) -> BudgetLimit:
    """配置/调整限额：原子 upsert（先条件 INSERT，冲突即改既有行，不产生重复行）。"""
    if scope_kind not in {"org", "space", "principal"}:
        raise BudgetError("invalid_budget_scope")
    if limit_tokens < 0:
        raise BudgetError("invalid_budget_limit")
    limit_t = BudgetLimit.__table__
    already_configured = exists(select(limit_t.c.id).where(
        limit_t.c.scope_kind == scope_kind, limit_t.c.scope_id == scope_id))
    inserted = session.execute(insert(limit_t).from_select(
        [limit_t.c.scope_kind, limit_t.c.scope_id, limit_t.c.limit_tokens, limit_t.c.revision],
        select(literal(scope_kind), literal(scope_id), literal(int(limit_tokens)), literal(1)).where(
            ~already_configured),
    ))
    if not inserted.rowcount:
        row = session.scalar(select(BudgetLimit).where(
            BudgetLimit.scope_kind == scope_kind, BudgetLimit.scope_id == scope_id))
        row.limit_tokens = int(limit_tokens)
        row.revision += 1
    session.flush()
    return session.scalar(select(BudgetLimit).where(
        BudgetLimit.scope_kind == scope_kind, BudgetLimit.scope_id == scope_id))


def _consumed(session: Session, scope_kind: str, scope_id: str) -> int:
    """该 scope 层级下的已消耗（报告/摘要口径）：活跃预留全额 + 已结算真实用量。

    判定路径不得调用本函数——限额判定在数据库原子语句内完成（见 ``reserve``）。
    """
    column = {"org": BudgetReservation.org_id,
              "space": BudgetReservation.space_id,
              "principal": BudgetReservation.principal_id}[scope_kind]
    reserved = session.scalar(
        select(func.coalesce(func.sum(BudgetReservation.reserved_tokens), 0)).where(
            column == scope_id, BudgetReservation.status == "reserved"))
    used = session.scalar(
        select(func.coalesce(func.sum(BudgetReservation.used_tokens), 0)).where(
            column == scope_id, BudgetReservation.status == "settled"))
    return int(reserved or 0) + int(used or 0)


def _scope_consumption_sql(reservation_t, scope_kind: str, scope_id: str):
    """SQL 端已消耗表达式：活跃预留全额 + 已结算真实用量（判定在数据库内求值）。"""
    column = reservation_t.c[_SCOPE_COLUMNS[scope_kind]]
    reserved = select(func.coalesce(func.sum(reservation_t.c.reserved_tokens), 0)).where(
        column == scope_id, reservation_t.c.status == "reserved").scalar_subquery()
    used = select(func.coalesce(func.sum(reservation_t.c.used_tokens), 0)).where(
        column == scope_id, reservation_t.c.status == "settled").scalar_subquery()
    return reserved + used


def _admission_conditions(reservation_t, limit_t, scopes, tokens: int) -> list:
    """单条原子语句内的准入条件：每个非空 scope 层级必须放行。

    - org/space：必须已配置限额，且 已消耗 + tokens <= 限额（未配置的生产空间不自动付费）；
    - principal：未配置不约束；已配置则同样校验。
    """
    conditions = []
    for scope_kind, scope_id in scopes:
        if not scope_id:
            continue
        configured = exists(select(limit_t.c.id).where(
            limit_t.c.scope_kind == scope_kind, limit_t.c.scope_id == scope_id))
        limit_row = select(limit_t.c.limit_tokens).where(
            limit_t.c.scope_kind == scope_kind, limit_t.c.scope_id == scope_id).scalar_subquery()
        fits = literal(tokens) + _scope_consumption_sql(reservation_t, scope_kind, scope_id) <= limit_row
        if scope_kind in {"org", "space"}:
            conditions.append(and_(configured, fits))
        else:
            conditions.append(or_(~configured, fits))
    return conditions


def _rejection_reason(session: Session, scopes, tokens: int) -> BudgetError:
    """原子判定拒绝后的原因归类（诊断路径，只解释 rowcount=0，不参与判定）。"""
    for scope_kind, scope_id in scopes:
        if not scope_id:
            continue
        limit_row = session.scalar(select(BudgetLimit).where(
            BudgetLimit.scope_kind == scope_kind, BudgetLimit.scope_id == scope_id))
        if limit_row is None:
            if scope_kind in {"org", "space"}:
                return BudgetError("budget_not_configured", scope_id)
            continue
        column = getattr(BudgetReservation, _SCOPE_COLUMNS[scope_kind])
        reserved = session.scalar(
            select(func.coalesce(func.sum(BudgetReservation.reserved_tokens), 0)).where(
                column == scope_id, BudgetReservation.status == "reserved")) or 0
        used = session.scalar(
            select(func.coalesce(func.sum(BudgetReservation.used_tokens), 0)).where(
                column == scope_id, BudgetReservation.status == "settled")) or 0
        if int(reserved) + int(used) + tokens > int(limit_row.limit_tokens):
            return BudgetError("budget_exhausted", f"{scope_kind}:{scope_id}")
    for scope_kind, scope_id in scopes:
        if scope_id:
            return BudgetError("budget_exhausted", f"{scope_kind}:{scope_id}")
    return BudgetError("budget_exhausted")


def _open_envelope(session: Session, run_id: str) -> BudgetReservation | None:
    """该 Run 当前开放的父预留（托管额度）；没有则 None（调用走自身准入）。"""
    return session.scalar(select(BudgetReservation).where(
        BudgetReservation.run_id == str(run_id),
        BudgetReservation.kind == "envelope",
        BudgetReservation.status == "reserved"))


def reserve(session: Session, *, org_id: str = "", space_id: str = "",
            principal_id: str = "", work_item_id: int = 0, run_id: str = "",
            category: str, tokens: int, estimated: bool = False,
            kind: str = "call", call_id: str = "", generation: int = 0,
            grant_revision: int = 0, request_hash: str = "",
            intent_status: str = "committed") -> BudgetReservation:
    """并发安全预留：单条原子条件 INSERT 判定，任一已配置层级超出即 budget_exhausted。

    校验与写入是同一条 SQL（``INSERT ... SELECT ... WHERE``）内的数据库级原子操作，
    以 rowcount 判定成败；``_consumed`` 何时返回、返回什么都无法影响判定结果。

    N04 费用语义（两层账、互斥合计）：
    - kind="envelope"：Run 级托管额度（escrow），照旧按 scope 原子准入；
    - kind="call" 且该 Run 有开放 envelope：调用优先从托管额度划拨
      （escrow_tokens），划不动（escrow 耗尽）的余量才按 scope 原子准入——
      预留期 envelope 与调用不叠加占额，可用额度不被提前拒绝；
    - kind="call" 且无 envelope：原有语义（独立按 scope 准入，兼容 code_fix
      引擎阶段与一次性网关调用）。

    动作意图字段（call_id/generation/grant_revision/request_hash/intent_status）
    随预留一并提交——外部调用执行前意图已持久可对账（N02）。
    """
    tokens = int(tokens)
    if tokens <= 0:
        raise BudgetError("invalid_reservation_tokens")
    if kind not in {"envelope", "call"}:
        raise BudgetError("invalid_reservation_kind")
    scopes = (("org", str(org_id or "")), ("space", str(space_id or "")),
              ("principal", str(principal_id or "")))
    reservation_t = BudgetReservation.__table__
    limit_t = BudgetLimit.__table__
    reservation_id = uuid4().hex
    resolved_call_id = str(call_id or "") or reservation_id
    columns = [
        reservation_t.c.reservation_id, reservation_t.c.org_id, reservation_t.c.space_id,
        reservation_t.c.principal_id, reservation_t.c.work_item_id, reservation_t.c.run_id,
        reservation_t.c.category, reservation_t.c.reserved_tokens, reservation_t.c.used_tokens,
        reservation_t.c.status, reservation_t.c.estimated, reservation_t.c.ok,
        reservation_t.c.created_at, reservation_t.c.kind, reservation_t.c.call_id,
        reservation_t.c.generation, reservation_t.c.grant_revision,
        reservation_t.c.request_hash, reservation_t.c.intent_status,
        reservation_t.c.escrow_tokens, reservation_t.c.provider_request_id,
        reservation_t.c.receipt_ref, reservation_t.c.checkpoint_ref,
    ]

    def _values(escrow_tokens: int, admission_tokens: int):
        return select(
            literal(reservation_id).label("reservation_id"),
            literal(scopes[0][1]).label("org_id"),
            literal(scopes[1][1]).label("space_id"),
            literal(scopes[2][1]).label("principal_id"),
            literal(int(work_item_id or 0)).label("work_item_id"),
            literal(str(run_id or "")).label("run_id"),
            literal(str(category)).label("category"),
            literal(tokens).label("reserved_tokens"),
            literal(0).label("used_tokens"),
            literal("reserved").label("status"),
            literal(bool(estimated)).label("estimated"),
            literal(True).label("ok"),
            literal(utcnow()).label("created_at"),
            literal(str(kind)).label("kind"),
            literal(resolved_call_id).label("call_id"),
            literal(int(generation or 0)).label("generation"),
            literal(int(grant_revision or 0)).label("grant_revision"),
            literal(str(request_hash or "")).label("request_hash"),
            literal(str(intent_status or "committed")).label("intent_status"),
            literal(int(escrow_tokens)).label("escrow_tokens"),
            literal("").label("provider_request_id"),
            literal("").label("receipt_ref"),
            literal("").label("checkpoint_ref"),
        ).where(and_(True, *_admission_conditions(
            reservation_t, limit_t, scopes, admission_tokens)))

    envelope = (_open_envelope(session, str(run_id))
                if kind == "call" and str(run_id or "") else None)
    if envelope is not None:
        return _reserve_from_envelope(
            session, envelope, scopes=scopes, columns=columns, values=_values,
            tokens=tokens, run_id=str(run_id), reservation_id=reservation_id,
            call_id=resolved_call_id, category=str(category),
            estimated=bool(estimated), grant_revision=int(grant_revision or 0),
            request_hash=str(request_hash or ""))
    result = session.execute(insert(reservation_t).from_select(columns, _values(0, tokens)))
    if result.rowcount != 1:
        raise _rejection_reason(session, scopes, tokens)
    return session.get(BudgetReservation, reservation_id)


def _reserve_from_envelope(session: Session, envelope: BudgetReservation, *, scopes,
                           columns, values, tokens: int, run_id: str,
                           reservation_id: str, call_id: str, category: str,
                           estimated: bool, grant_revision: int,
                           request_hash: str) -> BudgetReservation:
    """从父预留划拨的调用预留：托管内代付，划不动余量才按 scope 准入。

    - 划拨是条件 UPDATE（envelope 仍 reserved 且余量足够），并发划拨原子；
    - 全部划拨（excess=0）时不再向 scope 二次计额——envelope 创建时已准入，
      避免「Run 额度 + 每次调用」同时占额把可用额度提前拒掉（N04）；
    - 失败经 SAVEPOINT 回滚，只回滚本次划拨，不污染调用方事务。
    """
    reservation_t = BudgetReservation.__table__
    with session.begin_nested():
        escrow = min(tokens, int(envelope.reserved_tokens or 0))
        if escrow > 0:
            drawn = session.execute(update(reservation_t).where(
                reservation_t.c.reservation_id == envelope.reservation_id,
                reservation_t.c.status == "reserved",
                reservation_t.c.reserved_tokens >= escrow).values(
                reserved_tokens=reservation_t.c.reserved_tokens - escrow))
            if drawn.rowcount != 1:
                raise BudgetError("budget_exhausted", f"run:{run_id}")
        excess = tokens - escrow
        if excess > 0:
            result = session.execute(insert(reservation_t).from_select(
                columns, values(escrow, excess)))
            if result.rowcount != 1:
                raise _rejection_reason(session, scopes, excess)
        else:
            session.execute(insert(reservation_t).values(
                reservation_id=reservation_id, org_id=scopes[0][1], space_id=scopes[1][1],
                principal_id=scopes[2][1], run_id=str(run_id), category=str(category),
                reserved_tokens=tokens, used_tokens=0, status="reserved",
                estimated=bool(estimated), kind="call", call_id=str(call_id),
                grant_revision=int(grant_revision or 0),
                request_hash=str(request_hash or ""),
                intent_status="committed", escrow_tokens=escrow))
        session.flush()
    return session.get(BudgetReservation, reservation_id)


def _return_escrow(session: Session, *, run_id: str, amount: int) -> None:
    """子调用结算后把托管份额回补/追加扣减到父预留（互斥合计的关键一步）。"""
    if str(run_id or "") == "":
        return
    envelope = _open_envelope(session, str(run_id))
    if envelope is None:
        return
    current = int(envelope.reserved_tokens or 0)
    new_value = max(0, current + int(amount))
    session.execute(update(BudgetReservation).where(
        BudgetReservation.reservation_id == envelope.reservation_id,
        BudgetReservation.status == "reserved",
        BudgetReservation.reserved_tokens == current).values(reserved_tokens=new_value))


def settle(session: Session, reservation_id: str, *, used_tokens: int,
           ok: bool = True, usage_status: str | None = None,
           intent_status: str | None = None, outcome: str | None = None) -> BudgetReservation:
    """按真实 usage 结算：条件 UPDATE（status='reserved' 才可结算）保证并发下恰好结算一次。

    N04 费用语义：
    - 调用行（kind=call）：真实 usage 以唯一 call_id 记一次；结算即把该调用
      消耗的托管份额从父预留互斥释放（escrow 余量回补，负值=追加扣减钳 0）；
    - envelope 行（kind=envelope）：托管额度不是第二份消费账——只结算未被
      子调用回执覆盖的残差（reported - Σ子调用已结算），其余释放；
    - usage_status：recorded（真实用量）/ zero（真实零用量）/ unknown（结果
      未知，按预留保守归集）/ not_applicable（envelope 残差为零）——真实零与
      未知不用同一个值表示。
    """
    reservation_id = str(reservation_id)
    row = session.get(BudgetReservation, reservation_id)
    reported = max(0, int(used_tokens))
    if row is not None and (row.kind or "call") == "envelope":
        call_used = session.scalar(
            select(func.coalesce(func.sum(BudgetReservation.used_tokens), 0)).where(
                BudgetReservation.run_id == row.run_id,
                BudgetReservation.kind == "call",
                BudgetReservation.status == "settled")) or 0
        used = max(0, reported - int(call_used))
        resolved_usage = usage_status if usage_status is not None else (
            "recorded" if used > 0 else "not_applicable")
        escrow_delta = None
    else:
        used = reported
        resolved_usage = usage_status if usage_status is not None else "recorded"
        escrow_delta = (int(row.escrow_tokens or 0) - used) if row is not None else None
    resolved_intent = intent_status if intent_status is not None else "recorded"
    resolved_outcome = str(outcome) if outcome is not None else ("ok" if ok else "failed")
    result = session.execute(
        update(BudgetReservation)
        .where(BudgetReservation.reservation_id == reservation_id,
               BudgetReservation.status == "reserved")
        .values(status="settled", used_tokens=used, ok=bool(ok), settled_at=utcnow(),
                usage_status=str(resolved_usage), intent_status=str(resolved_intent),
                outcome=str(resolved_outcome)))
    if result.rowcount == 0:
        row = session.get(BudgetReservation, reservation_id)
        if row is None:
            raise BudgetError("reservation_not_found")
        raise BudgetError("reservation_already_closed")
    if escrow_delta is not None and row is not None:
        _return_escrow(session, run_id=str(row.run_id or ""), amount=int(escrow_delta))
    session.flush()
    return session.get(BudgetReservation, reservation_id)


def release(session: Session, reservation_id: str) -> BudgetReservation:
    """取消未执行调用：条件 UPDATE（status='reserved' 才可释放），释放不泄漏、不可重复。"""
    result = session.execute(
        update(BudgetReservation)
        .where(BudgetReservation.reservation_id == str(reservation_id),
               BudgetReservation.status == "reserved")
        .values(status="released", settled_at=utcnow(),
                intent_status="cancelled", outcome="cancelled",
                usage_status="zero"))
    if result.rowcount == 0:
        row = session.get(BudgetReservation, str(reservation_id))
        if row is None:
            raise BudgetError("reservation_not_found")
        raise BudgetError("reservation_already_closed")
    return session.get(BudgetReservation, str(reservation_id))


def call_usage_total(session: Session, run_id: str) -> int:
    """Run 维度已结算真实用量（查询聚合，不是第二份账）：Σ子调用 used_tokens。"""
    value = session.scalar(
        select(func.coalesce(func.sum(BudgetReservation.used_tokens), 0)).where(
            BudgetReservation.run_id == str(run_id),
            BudgetReservation.kind == "call",
            BudgetReservation.status == "settled"))
    return int(value or 0)


def usage_summary(session: Session, *, org_id: str = "", space_id: str = "",
                  principal_id: str = "") -> dict:
    """分项事实与未知项：不能只给 ok:true。"""
    query = select(BudgetReservation)
    if org_id:
        query = query.where(BudgetReservation.org_id == org_id)
    if space_id:
        query = query.where(BudgetReservation.space_id == space_id)
    if principal_id:
        query = query.where(BudgetReservation.principal_id == principal_id)
    rows = list(session.scalars(query))
    return {
        "reserved_open": sum(r.reserved_tokens for r in rows if r.status == "reserved"),
        "used_total": sum(r.used_tokens for r in rows if r.status == "settled"),
        "released": sum(1 for r in rows if r.status == "released"),
        "estimated_settled": sum(1 for r in rows if r.status == "settled" and r.estimated),
        "failed_settled": sum(1 for r in rows if r.status == "settled" and not r.ok),
        "by_category": {category: sum(r.used_tokens for r in rows
                                      if r.status == "settled" and r.category == category)
                        for category in sorted({r.category for r in rows})},
    }
