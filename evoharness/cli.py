"""统一 CLI 入口（DE-28）：与 api.py 共用同一应用服务与判定。

- 稳定 ID、``--json`` 结构化输出、可读错误码 + 下一步动作；
- 不做业务判定：全部经 evoharness.api.WorkApi → 各领域服务；
- 数据库来源：--db 参数或 EVOHARNESS_DATABASE_URL，缺失时给可读错误而非 traceback。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
from pathlib import Path

from evoharness.api import WorkApi, open_api
from evoharness.config import resolve_config
from evoharness.storage import sqlite_database_path


def _build_api(args) -> WorkApi:
    """统一配置解析（DE-02）：--db > EVOHARNESS_DATABASE_URL；产物目录按库位置派生。"""
    config = resolve_config(db=getattr(args, "db", "") or "",
                            artifacts=getattr(args, "artifacts", None))
    return open_api(config.database_url, artifacts_dir=config.artifacts_dir)


def _add_item_commands(sub):
    p = sub.add_parser("item", help="工作项操作")
    item_sub = p.add_subparsers(dest="item_cmd", required=True)

    get = item_sub.add_parser("get", help="查看工作项")
    get.add_argument("--id", required=True)

    create = item_sub.add_parser("create", help="创建工作项")
    create.add_argument("--tenant", required=True)
    create.add_argument("--owner", required=True)
    create.add_argument("--title", required=True)
    create.add_argument("--action", default="")
    create.add_argument("--chat-id", default="")
    create.add_argument("--goal", default="")
    create.add_argument("--acceptance-json", default="[]")
    create.add_argument("--assignee-id", default="")
    create.add_argument("--assignee-name", default="")
    create.add_argument("--kind", default="legacy_todo",
                        choices=["legacy_todo", "delegated_delivery"])
    create.add_argument("--org", default="")
    create.add_argument("--space", default="")
    create.add_argument("--due", default="")
    create.add_argument("--source-refs", default="", help="逗号分隔来源引用")
    create.add_argument("--idempotency-key", default="")

    for name, helptext in (("transition", "状态推进"), ("pause", "暂停"),
                           ("cancel", "取消")):
        cmd = item_sub.add_parser(name, help=helptext)
        cmd.add_argument("--id", required=True, type=int)
        cmd.add_argument("--actor", required=True)
        cmd.add_argument("--reason", default="")
        if name == "transition":
            cmd.add_argument("--to", required=True)
            cmd.add_argument("--expected-revision", default=None)

    complete = item_sub.add_parser("complete", help="完成普通待办（委派交付不适用）")
    complete.add_argument("--id", required=True, type=int)
    complete.add_argument("--actor", required=True)
    complete.add_argument("--evidence", default="", help="逗号分隔证据消息 ID")

    clarify = item_sub.add_parser("clarify", help="提出澄清")
    clarify.add_argument("--id", required=True, type=int)
    clarify.add_argument("--actor", required=True)
    clarify.add_argument("--question", required=True)

    resolve = item_sub.add_parser("resolve", help="补齐澄清")
    resolve.add_argument("--id", required=True, type=int)
    resolve.add_argument("--actor", required=True)
    resolve.add_argument("--answer-event-id", required=True)

    note = item_sub.add_parser("note", help="提交追加说明")
    note.add_argument("--id", required=True, type=int)
    note.add_argument("--event-id", required=True)
    note.add_argument("--note", required=True)
    note.add_argument("--actor", required=True)


def _add_view_commands(sub):
    view = sub.add_parser("view", help="工作视图（待处理/进行中/待我处理/已交付）")
    view.add_argument("--tenant", required=True)
    view.add_argument("--owner", required=True)
    view.add_argument("--org", default="", help="组织空间 ID；留空为个人范围")


def _add_run_commands(sub):
    p = sub.add_parser("run", help="Run 操作")
    run_sub = p.add_subparsers(dest="run_cmd", required=True)

    plan = run_sub.add_parser("plan", help="生成待批准载荷（payload_hash）")
    plan.add_argument("--task", required=True, type=int)
    plan.add_argument("--brief-json", default="")

    start = run_sub.add_parser("start", help="启动 Run（需先批准 work.execute）")
    start.add_argument("--task", required=True, type=int)
    start.add_argument("--actor", required=True)
    start.add_argument("--brief-json", default="")
    start.add_argument("--mode", default="live", choices=["live", "dryrun"])
    start.add_argument("--output-root", default="")
    start.add_argument("--idempotency-key", default="")

    pause = run_sub.add_parser("pause", help="暂停未认领的 Run")
    pause.add_argument("--run-id", required=True)

    req = run_sub.add_parser("request-acceptance", help="执行结束请求验收")
    req.add_argument("--task", required=True, type=int)
    req.add_argument("--run-id", required=True)
    req.add_argument("--requester", required=True)

    review = run_sub.add_parser("review-acceptance", help="独立验收（pass/fail）")
    review.add_argument("--task", required=True, type=int)
    review.add_argument("--run-id", required=True)
    review.add_argument("--reviewer", required=True)
    review.add_argument("--verdict", required=True, choices=["pass", "fail"])
    review.add_argument("--notes", default="")


def _add_approval_commands(sub):
    p = sub.add_parser("approval", help="批准 / 撤回")
    ap_sub = p.add_subparsers(dest="approval_cmd", required=True)

    submit = ap_sub.add_parser("submit", help="提交批准（绑定目标版本与载荷）")
    submit.add_argument("--org", required=True)
    submit.add_argument("--actor", required=True)
    submit.add_argument("--target-type", required=True)
    submit.add_argument("--target-id", required=True)
    submit.add_argument("--revision", required=True, type=int)
    submit.add_argument("--payload-json", required=True)
    submit.add_argument("--actions", required=True, help="逗号分隔，如 work.execute")
    submit.add_argument("--grantee", default="")
    submit.add_argument("--expires-at", default="")

    get = ap_sub.add_parser("get", help="查看批准（含正文差异/身份/过期）")
    get.add_argument("--decision-id", required=True)

    listing = ap_sub.add_parser("list", help="列出活跃批准")
    listing.add_argument("--org", required=True)

    revoke = ap_sub.add_parser("revoke", help="撤回（未执行发送意图一并取消）")
    revoke.add_argument("--org", required=True)
    revoke.add_argument("--actor", required=True)
    revoke.add_argument("--target-type", required=True)
    revoke.add_argument("--target-id", required=True)


def _add_info_commands(sub):
    kn = sub.add_parser("knowledge", help="知识检索")
    kn_sub = kn.add_subparsers(dest="knowledge_cmd", required=True)
    search = kn_sub.add_parser("search", help="权限裁剪后的检索")
    search.add_argument("--space", required=True)
    search.add_argument("--subject", required=True)
    search.add_argument("--owner-scope", default="")
    search.add_argument("--include-private", action="store_true")

    ev = sub.add_parser("evidence", help="查看工作项证据")
    ev.add_argument("--task", required=True, type=int)

    cost = sub.add_parser("cost", help="成本归集分项")
    cost.add_argument("--org", default="")
    cost.add_argument("--space", default="")
    cost.add_argument("--principal", default="")

    health = sub.add_parser("health", help="健康分项（含未知项）")
    health.add_argument("--org", default="")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evoharness.cli",
        description="EvoHarness 统一 CLI（与 API/工作台共用同一应用服务）")
    parser.add_argument("--db", default="", help="业务库 URL；缺省读 EVOHARNESS_DATABASE_URL")
    parser.add_argument("--artifacts", default="", help="长输出产物目录")
    parser.add_argument("--json", action="store_true", help="输出统一信封 JSON")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("schema", help="打印 API 契约 schema")
    _add_view_commands(sub)
    _add_item_commands(sub)
    _add_run_commands(sub)
    _add_approval_commands(sub)
    _add_info_commands(sub)
    return parser


def _dispatch(api: WorkApi, args) -> dict:
    command, item_cmd, run_cmd, approval_cmd, knowledge_cmd = (
        args.command, getattr(args, "item_cmd", ""), getattr(args, "run_cmd", ""),
        getattr(args, "approval_cmd", ""), getattr(args, "knowledge_cmd", ""))
    if command == "schema":
        return api.schema()
    if command == "view":
        return api.work_view(tenant=args.tenant, owner=args.owner, org_id=args.org)
    if command == "item":
        if item_cmd == "get":
            return api.get_work_item(args.id)
        if item_cmd == "create":
            return api.create_work_item(
                tenant=args.tenant, owner=args.owner, title=args.title, action=args.action,
                chat_id=args.chat_id, goal=args.goal, acceptance=args.acceptance_json,
                assignee_id=args.assignee_id, assignee_name=args.assignee_name,
                workflow_kind=args.kind, org_id=args.org, space_id=args.space,
                due_text=args.due,
                source_refs=[r for r in args.source_refs.split(",") if r.strip()],
                idempotency_key=args.idempotency_key)
        if item_cmd == "transition":
            return api.transition_work_item(
                args.id, to_state=args.to, actor=args.actor, reason=args.reason,
                expected_revision=args.expected_revision)
        if item_cmd == "pause":
            return api.pause_work_item(args.id, actor=args.actor, reason=args.reason)
        if item_cmd == "complete":
            return api.complete_work_item(
                args.id, actor=args.actor,
                evidence_message_ids=[e for e in args.evidence.split(",") if e.strip()])
        if item_cmd == "cancel":
            return api.cancel_work_item(args.id, actor=args.actor, reason=args.reason)
        if item_cmd == "clarify":
            return api.request_clarification(args.id, actor=args.actor, question=args.question)
        if item_cmd == "resolve":
            return api.resolve_clarification(args.id, actor=args.actor,
                                             answer_message_id=args.answer_event_id)
        if item_cmd == "note":
            return api.submit_supplement(args.id, event_id=args.event_id,
                                         note=args.note, actor=args.actor)
    if command == "run":
        brief = None
        if getattr(args, "brief_json", ""):
            try:
                brief = json.loads(args.brief_json)
            except ValueError:
                return WorkApi._fail("invalid_brief_json")
        if run_cmd == "plan":
            return api.run_request_payload(args.task, brief)
        if run_cmd == "start":
            return api.start_run(args.task, actor_id=args.actor, brief=brief, mode=args.mode,
                                 output_root=args.output_root or None,
                                 idempotency_key=args.idempotency_key or None)
        if run_cmd == "pause":
            return api.pause_run(args.run_id)
        if run_cmd == "request-acceptance":
            return api.request_acceptance(args.task, run_id=args.run_id,
                                          requester=args.requester)
        if run_cmd == "review-acceptance":
            return api.review_acceptance(args.task, run_id=args.run_id, reviewer=args.reviewer,
                                         verdict=args.verdict, notes=args.notes)
    if command == "approval":
        if approval_cmd == "submit":
            try:
                payload = json.loads(args.payload_json)
            except ValueError:
                return api._fail("decision_payload_required", {"detail": "payload-json 解析失败"})
            return api.submit_approval(
                org_id=args.org, actor_id=args.actor, target_type=args.target_type,
                target_id=args.target_id, target_revision=args.revision, payload=payload,
                actions=[a for a in args.actions.split(",") if a.strip()],
                grantee_id=args.grantee, expires_at=args.expires_at or None)
        if approval_cmd == "get":
            return api.get_approval(args.decision_id)
        if approval_cmd == "list":
            return api.approvals_view(args.org)
        if approval_cmd == "revoke":
            return api.revoke_approval(org_id=args.org, actor_id=args.actor,
                                       target_type=args.target_type,
                                       target_id=args.target_id)
    if command == "knowledge":
        return api.search_knowledge(space_id=args.space, subject=args.subject,
                                    owner_scope=args.owner_scope,
                                    include_private=args.include_private)
    if command == "evidence":
        return api.view_evidence(args.task)
    if command == "cost":
        return api.view_cost(org_id=args.org, space_id=args.space, principal_id=args.principal)
    if command == "health":
        return api.view_health(args.org)
    return WorkApi._envelope(False, None, "unknown_command")


def _readable(result: dict) -> str:
    """非 --json 的可读输出：只报事实与下一步，不加标语。"""
    data = result.get("data") or {}
    if not result.get("ok"):
        lines = [f"错误 {result.get('reason_code')}",
                 f"可重试: {'是' if result.get('retryable') else '否'}"]
        if result.get("next_action"):
            lines.append(f"下一步: {result.get('next_action')}")
        if isinstance(data.get("detail"), dict) and data["detail"]:
            lines.append(f"明细: {json.dumps(data['detail'], ensure_ascii=False)}")
        return "\n".join(lines)
    if result.get("reason_code") != "ok":
        return f"状态 {result.get('reason_code')}（下一步: {result.get('next_action') or '—'}）"
    if "buckets" in data:
        counts = data.get("counts", {})
        return "待处理 {pending} · 进行中 {in_progress} · 待我处理 {awaiting_me} · 已交付 {delivered} · 已取消 {cancelled}".format(**counts)
    if "approvals" in data:
        return f"活跃批准 {len(data['approvals'])} 项"
    if "claims" in data:
        return f"检索到 {len(data['claims'])} 条主张（冲突 {len(data.get('conflicts', []))}）"
    if "operation_id" in data:
        return f"Run {data['operation_id']} 状态 {data['state']}（任务 {data.get('task_state')}）"
    if "decision_id" in data:
        return f"批准 {data['decision_id']} 已绑定目标版本 r{data.get('target_revision')}"
    if "id" in data and "revision" in data:
        display = data.get("display") or {}
        return f"工作项 {data['id']} r{data['revision']} 状态 {display.get('label', data['state'])}"
    return json.dumps(data, ensure_ascii=False)[:600]


def main(argv=None, *, stdout=None) -> int:
    out = stdout if stdout is not None else sys.stdout
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        api = _build_api(args)
    except ValueError as exc:
        result = WorkApi._envelope(False, None, str(exc) or "database_not_configured")
    else:
        try:
            result = _dispatch(api, args)
        except ValueError as exc:
            result = WorkApi._convert(exc)
    if args.json:
        print(json.dumps(result, ensure_ascii=False), file=out)
    else:
        print(_readable(result), file=out)
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
