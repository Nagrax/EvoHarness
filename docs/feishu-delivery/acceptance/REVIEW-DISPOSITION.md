# 独立评审处置记录（2026-11-07）

两轮独立评审（不同会话，只读代码/产物）提出的问题与处置。评审意见按原编号保留。

## 核心链（F02–F06）

| # | 严重度 | 问题 | 处置 |
| --- | --- | --- | --- |
| 1 | blocking | 历史补采只入库，处理时因 `created/edited=False` 不分类，问题永不生成草稿 | 已修：`process_incoming` 对 `category=unknown 且未处理` 的已存消息重新分类；新增 `test_history_message_is_classified_when_processed` |
| 2 | blocking | 空白名单返回放行，任意已登记会话可接收/自动回复 | 已修：`is_whitelisted` 空白名单返回 False；admission/auto gate 均按关闭处理；测试显式设置白名单 |
| 3 | blocking | 日报已发回复用宿主本地 `fromtimestamp` 混用 naive UTC | 已修：统一 `from_epoch_ms`；新增 `test_sent_reply_is_attributed_by_receipt_time_in_utc` |
| 4 | major | `Source.reply_allowed` 与配置移除不同步 | 已修：bootstrap 同步权限；outbox 发送前同时检查 Source 与 `FEISHU_REPLY_ALLOWED_CHATS` |
| 5 | major | 编辑只作废旧草稿，不清 decision/handled，不重判 | 已修：编辑/撤回清 `ReplyDecision`、`handled=False`；新增 `test_edit_reprocesses_message_instead_of_staying_handled` |
| 6 | major | `in_time_window` 把 naive UTC 当宿主本地时间 | 已修：先 `replace(tzinfo=UTC)` 再转目标时区 |
| 7 | major | `AUTO_REPLY_ALLOWED_CATEGORIES` 未生效 | 已修：policy 增加 `category_not_allowed`；新增 `test_category_not_allowed_requires_review` |
| 8 | major | 新候选无条件改 head，把已发布版本挤出检索 | 已修：候选不改 head；检索覆盖所有 active 版本，publish/rollback 才切换 head；新增 `test_candidate_does_not_displace_published_head` |

## 评测与门禁（F00/F07）

| # | 严重度 | 问题 | 处置 |
| --- | --- | --- | --- |
| 1 | blocking | F00 门禁产物/STATE 尚未落盘 | 见 §门禁结果（重跑后回填） |
| 2 | blocking | feishu finding 被 triage 当信息型自动闭合，`loop_autorun` 退出码失效 | 已修：`feishu` 移出自动闭合 source 集合；缺失/失败保持 pending，需显式复测关闭 |
| 3 | major | baseline 可选，未冻结节点集 | F00 首次成功门禁产物作为 baseline；F02 起使用 `--baseline`；F00 报告如实标注首轮 `provided:false` |
| 4 | major | replay 自写规则，未调用生产 policy；阈值不一致 | 已重建：approved/candidate/no_experience 直接调 `policy.evaluate` + `find_best_match`；阈值来自 golden 冻结配置并在产物标注 |
| 5 | major | golden 样本少、无分组/留出/区间 | 已加 dev/holdout、Wilson 95% 下界、`sampleNote` 明确不足以对外宣称精度；不冒充大样本结论 |
| 6 | major | 产物只按时间判新旧，未绑定 golden/代码；非原子写 | 已修：产物写入 goldenSha256/replaySha256，原子 replace；signal 校验 hash；`check` 对 golden/replay 变更报错 |
| 7 | major | gate 把 `eval/loop/BACKLOG.json` 算进 sourceDigest | 已修：gate 排除 `eval/loop/BACKLOG*`，写台账不再触发 stale |
| 8 | minor | 1 题×2 遍的 stability 产物仍显示 all-sources-runnable | 已在 F00 报告披露低置信；source-health 增加 artifactValid/infraFailures 校验，不把 provider 故障当能力失败 |

## 终审 B 轮（6 条 BLOCK）处置

| # | 问题 | 处置 |
| --- | --- | --- |
| 1 | 修改/纠正卡片无输入控件；空 form 回落原稿 | 卡片增加 form + input；`edit` 空输入拒绝；新增表单结构与空输入测试 |
| 2 | 已批准草稿可再 edit；并发 approve+edit 二次发信；reject 可能已发 | approve/edit 只允许 PENDING 且原子抢占；reject 只允许 PENDING 并取消排队 outbox；新增 `test_approved_draft_cannot_be_edited_or_approved_twice` |
| 3 | 生产流水线 last==report_date 时不重算最近一天 | `start` 回落到 report_date 强制重算；新增 `test_daily_pipeline_regenerates_latest_report_for_late_data` |
| 4 | 二次 rollback 暂停 head；旧卡可回滚新 head | 无上一版 no-op；rollback 校验 experience 版本与 head；新增 `test_second_rollback_is_noop_and_does_not_pause_head` |
| 5 | 日限额先读后写；类别只查全局配置 | `SystemState` 原子预留名额；Experience 增加 category 列并逐条校验；迁移补列 |
| 6 | replay 无 chat/category/time 分组、仅 approved case 证据 | 增加 dev/holdout、chatType/category/timeBlock 分组、四策略 `caseEvidence`；样本限制披露 |

## 终审 C 轮（剩余 4 条）处置

| # | 问题 | 处置 |
| --- | --- | --- |
| 1 | rollback 未接受 expected 参数（上一轮补丁未落地） | 已补齐签名与 head/version 守卫；review 传入；二次 rollback 测试覆盖 |
| 2 | v2 库仍走 v1 回填，同 intent 多版本可撞 UNIQUE | `migrate` 按 `current_version` 跳过 v1 回填，v2→v3 只补 category；新增双版本迁移测试 |
| 3 | 缺并发 reject、quota 原子、纠正 category 断言 | 新增 reject-after-approve、`_reserve_auto_slot` 原子限额、纠正继承 public/category 断言 |
| 4 | 纠正候选丢失 visibility/steps 等 | `correct_sent_reply` 完整继承 previous 的 preconditions/steps/allowed_links/sensitive/visibility/category |

## 门禁结果

- F00 聚合门禁：`runs/F00/gate-20261107-0500.json`，`ok: true`；default/utf8 各 1255 tests / 1253 passed / 2 skipped / 0 failed；`sourceDigest c4a78e82…`。
- F02–F07：由同一 `sourceDigest` 复用该次完整门禁运行（`reuse_gate.py` 强制核对快照），分别生成独立 taskId 的 gate.json；没有重复消耗相同测试运行，也没有在树变化后复用。
- F07 飞书信号：`feishu-eval.json` 产物绑定 golden/replay 哈希；缺产物/旧产物/严重错误会产出 finding，且不再被 triage 自动闭合。
