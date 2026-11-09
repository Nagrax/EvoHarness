# F08/F09 验收状态（2026-11-07）

真实凭据已具备，机器人私聊最小链路已打通；两项仍未达到验收完成。
实机证据见 `F08-F09-live-verification.md` 与 `F08-F09-live-evidence.json`。

## F08 已具备

- 真实 owner 收到日报卡（含修订版），可查看待办与待批回复；
- 真实卡片批准后回复到原会话，outbox 有真实回执；
- 真实卡片“标记完成”消费一次性 token 并更新任务；
- 真实卡片“验证”把候选经验发布为 `approved_auto`；
- 重复问题经 EvoHarness BM25 命中经验并生成待批草稿；
- 健康探针实际识别 `daily_heartbeat_missing`，执行 `_daily_job()` 后恢复 `ok:true`；
- SQLite backup API 备份后，隔离副本行数一致且 health `ok:true`；
- 服务多次重启后 WS 重连、历史补采与 outbox 恢复；outbox 恢复与日志有离线测试。

## F08 未完成

1. 至少 **7 个实际日报周期**与真实人工裁决记录；
2. 经飞书传输的“纠正 #id 内容”真实链路（本轮只做了处理器直调）；
3. 账号撤权 / 升级迁移的实机演练；
4. 群聊 @ / 群无 @ / 同群话题与 OAuth 自然人单聊覆盖（同时阻塞 F01）；
5. `main.py daily` 不写 daily heartbeat，手工日报的健康覆盖仍缺失。

## F09 已具备

- `whitelist_auto`、每日限额、类别/敏感/注入/时效/白名单硬门；
- 一次受控自动回复冒烟：决策 `auto_eligible`、outbox `auto=1`、真实回执；
- 冒烟后已回退 `single_approval`，public 版本回滚为 `paused`；
- 精确模式匹配校准：去空白/标点后完全一致提升到 0.95，近义不抬高，负例冲突优先；
  `test_exact_pattern_is_auto_eligible` 在 public + whitelist_auto + 0.85 下得到 `auto_eligible`；
- 黄金回放与 Wilson 下界报告（仅回归用途）。

## F09 未完成

1. 冻结留出集与影子统计，候选精度/覆盖率/严重错误达到门槛；
2. 近义问题的评分仍未正式标定；精确重复不再是 0.2901，但只有单测与合成冒烟，没有真实留出集；
3. owner 对自动发送的显式生产放行与最小白名单；
4. 停发、回退、更正演练，以及样本不足时维持 `single_approval` 的结论。

## 当前不得宣称

- 不得宣称 F08/F09 已完成或自动回复已灰度上线。
- 冒烟阈值（0.2/0.1）不是生产配置，不能作为精度证据。
- 真实语料、凭据与日志不入仓库；启动试点时按 `docs/feishu-delivery/probes/README.md` 脱敏。
