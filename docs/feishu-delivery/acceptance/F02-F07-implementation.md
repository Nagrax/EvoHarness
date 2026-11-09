# F02–F07 实施与验收映射（2026-11-07）

本文件把每项工单的验收条件映射到代码入口与可复跑测试。真实飞书写操作仍由 F01 外部条件阻塞，离线链已可复现。

## F02 持久接收、历史补采与覆盖对账

| 验收 | 代码 | 测试 |
| --- | --- | --- |
| 实时/历史共用准入与持久 inbox | `app/domain/admission.py`、`app/domain/intake.py`、`app/handlers/events.py` | `test_event_dedupe_and_message_idempotent`、`test_realtime_group_no_mention_is_ingested_without_processing` |
| 断进程、重扫重叠分页、事件重放、双入口 | `sync_source_window` 按页落盘 + `SourceSync.cursor`；`Message(tenant,message_id)` 唯一 | `test_history_sync_pages_overlap_and_idempotent`、`test_history_sync_failure_is_partial_and_resumable` |
| 未知身份/空白名单/错误 @/撤权/撤回 | `admit` 返回稳定 reason code；未知 bot id 不做任意 @ 放行 | `test_admission_rejects_unknown_bot_identity`、`test_admission_rejects_revoked_source`、`test_recall_stops_learning_and_pauses_experience` |
| 旧库迁移、备份恢复、迁移重入 | `app/core/migrations.py`：v1 原型回填、v2→v3 只补 category、`schema_version`、`.bak-*` | `test_legacy_database_migrates_and_is_idempotent`、`test_v2_database_gets_category_column` |

## F03 Owner 批示、发送 outbox 与回执恢复

| 验收 | 代码 | 测试 |
| --- | --- | --- |
| 审批与发送意图同事务 | `review._approve_draft` 只写 draft/outbox；`main/worker/scheduler` 在提交后 dispatch | `test_approval_queues_before_send`、`test_transaction_rollback_has_no_external_side_effect` |
| 伪造 operator、旧卡、重复批准、版本变化 | `ActionToken` 一次性 + operator/版本校验；approve/edit 仅 PENDING 原子抢占；reject 取消排队 outbox | `test_forged_operator_is_rejected`、`test_stale_card_version_is_rejected`、`test_duplicate_approval_does_not_duplicate_send`、`test_approved_draft_cannot_be_edited_or_approved_twice` |
| 退避、隔离、对账、暂停/撤权复检 | `outbox.mark_failure/recover_expired_leases/_precheck` | `test_retryable_then_sent`、`test_permanent_error_is_quarantined`、`test_unknown_requires_reconciliation_and_never_blind_retries`、`test_paused_auto_reply_cancels_pending_intent`、`test_revoked_source_cancels_pending_intent` |
| 只有真实回执显示已发送 | `mark_sent` 需要 `receipt_id`；sent 无回执转 `delivery_unknown` | `test_expired_lease_becomes_delivery_unknown_in_offline_mode` |

## F04 任务生命周期与可批示日报

| 验收 | 代码 | 测试 |
| --- | --- | --- |
| 同话题归并、来源证据、待确认 | `app/domain/tasks.py`：稳定 `task_key`、evidence/source ids、`NEEDS_CONFIRMATION` | `test_task_needs_confirmation_when_assignee_missing` |
| 跨天、完成、重开 | `open_tasks` 跨天保留；完成信号/owner 动作写 `TaskEvent` | `test_task_completion_signal_and_manual_reopen` |
| 上海逻辑日、重复执行、迟到补记、停机补跑 | `app/core/clock.py`、`reports.generate_report`（report key + revision + 独立 intent）、`daily.run_daily_pipeline` 补跑缺失日期且重算最近一天 | `test_shanghai_logical_day_boundary`、`test_daily_report_is_idempotent_and_revised_on_late_data`、`test_downtime_catch_up_generates_missing_reports` |
| 日报含待办/待批/已发/经验/缺口；一个日窗口一个主意图 | `app/feishu/cards.py::build_report_card`；`DailyReport.delivery_intent_key` | `test_daily_card_contains_required_sections_and_gaps`、`test_report_card_complete_task_then_approve_draft` |

## F05 版本化 FAQ 与 EvoHarness 接缝

| 验收 | 代码 | 测试 |
| --- | --- | --- |
| 问题簇、受众、负例、来源、版本 | `app/memory/experience.py`：`Experience` 不可变版本 + `ExperienceHead` + `audience`/`negative_examples` | `test_versioned_publish_supersede_and_rollback`、`test_private_experience_not_used_in_group`、`test_public_experience_audience_match`、`test_negative_example_blocks_high_similarity_wrong_intent` |
| 回复审批与经验发布分开 | `review._sediment_approved_answer` 只生成 candidate；`publish` 单独动作 | `test_approve_sends_and_sedates_new_experience`、`test_verify_card_is_version_bound` |
| 纠正已发回复、回执保留 | `correct_sent_reply` 暂停旧版、完整继承受众/步骤/可见性/类别、生成候选；卡片增加必填纠正输入；二次回滚 no-op | `test_correction_creates_candidate_and_keeps_receipt`、`test_second_rollback_is_noop_and_does_not_pause_head` |
| 检索接缝只传受控语料 | `app/memory/retrieval.py` 调用 `agents.skills.rank_corpus_for_query` | `test_evoharness_retrieval_engine_is_used`（断言 `evoharness-bm25`） |

## F06 自动回复硬门、影子与急停

| 验收 | 代码 | 测试 |
| --- | --- | --- |
| 纯策略 reason code | `app/domain/policy.py::evaluate` | `test_policy.py` 全部 |
| 许可/内容安全/注入/回声/过期/高分异意图负例 | `admit` + `policy.answer_safety` + `negative_example_conflict` | `test_shadow_mode_generates_no_send_intent`、`test_sensitive_answer_requires_manual_review`、`test_promise_answer_requires_manual_review`、`test_prompt_injection_in_question_requires_manual_review`、`test_stale_trigger_is_manual` |
| 影子请求传输层零业务发送 | `process_incoming` 对 shadow 只建 pending draft；`dispatch` 无 intent | `test_shadow_mode_generates_no_send_intent` |
| 每日每会话上限与暂停 | `policy._reserve_auto_slot` 原子预留、`Experience.category` 逐条类别门、`outbox._precheck` | `test_daily_chat_limit_is_manual`、`test_category_not_allowed_requires_review`、`test_paused_auto_reply_cancels_pending_intent` |

## F07 黄金回放与评测接线

| 验收 | 代码/产物 | 复跑 |
| --- | --- | --- |
| 冻结 case/阈值与 case 级证据 | `feishu-daily-agent/eval/golden_cases.json`、`eval/replay.py` | `cd feishu-daily-agent && .venv/Scripts/python.exe eval/replay.py --run` |
| 比较原型/无经验/候选/已批准 | approved/candidate/no_experience 直接调生产 `policy.evaluate`；`prototype` 仅作缺陷对照 | 同上；产物含 dev/holdout 分组、Wilson 95% 下界、golden/replay 哈希 |
| 缺产物/旧产物/严重错误触发闭环 | `eval/feishu_signal.py`、`eval/loop.py::_collect_feishu` | `python eval/feishu_signal.py --check`；`pytest tests/test_feishu_signal.py` |
| 注入缺陷 → finding → 修复复测 | 测试用临时 artifact 覆盖 missing/stale/severe；`_collect_feishu` 追加 finding | `test_severe_errors_produce_high_finding`、`test_loop_collect_feishu_appends_findings` |

## 未覆盖/诚实边界

- 真实飞书读权限、用户授权自然人单聊、真实发送回执：F01 外部阻塞。
- 模型每日预算尚未在执行路径强制记账（`ModelLedger` 表与配置已就绪，离线模式不调用真实模型）；这是后续硬化项，不在本轮离线验收内宣称完成。
- 最新 failure-stability artifact 仅 1 题×2 遍，仅用于健康检查；不得作为稳定性结论。
- golden 仅 12 case（dev 10 / holdout 2），用于回归与硬门，不足以对外宣称精度；真实灰度仍需 F09 的留出集与影子统计。
