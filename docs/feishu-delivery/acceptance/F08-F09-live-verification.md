# F08/F09 实机链路验证（2026-11-07）

状态：**F08/F09 均未达到验收完成**；本文记录已打通的最小真实链路与一次受控自动回复冒烟。
原始消息、凭据与日志只在本机，本文对真实标识脱敏，不写入真实语料。

## 1. 已验证链路

| 环节 | 结果 | 证据 |
|---|---|---|
| 真实私聊消息入库 | 通过 | 任务消息与问题消息均入库；`msg_type=text`、来源 `p2p` |
| 任务抽取 | 通过 | Task #1 `needs_confirmation`，证据指向原消息 |
| 问题拟稿 | 通过 | Draft #1 pending，来源问题、拟回复、`no_experience` 原因码 |
| 日报卡送达 | 通过 | 首次回执 `om_x100...1840`；修订版 `om_x100...a5e` |
| 卡片批准 | 通过 | 真实回调；Draft #1 → `sent`，回执 `om_x100...f632`，原消息会话出现回复 |
| 任务完成 | 通过 | 真实回调；Task #1 → `done`，一次性 token 已消费，写入 review/task 事件 |
| 经验沉淀 | 通过 | 批准后生成候选 Experience #1（private，意图哈希稳定） |
| 经验验证 | 通过 | token 验证后 `approved_auto`，写入 publish 事件 |
| 重复问题命中 | 通过 | EvoHarness BM25 命中经验 #1，相似度 0.2901；生成 Draft #2（影子，待批） |
| 受控自动回复冒烟 | 通过（仅冒烟） | public v2 + `whitelist_auto` 下决策 `auto_eligible`，outbox `auto=1`，回执 `om_x100...7ec` |

### 1.1 纠正与反馈迭代

浏览器面板关闭后，用同一 owner 指令处理器补测了纠正分支（**未经过飞书传输**）：

- `纠正 #1 新答案` 首次调用直接抛 `TypeError: correct_sent_reply() got an unexpected keyword argument 'reason'`；
  真实缺陷，已修复函数签名并补回归测试。
- 修复后生成候选 Experience #3，answer 为纠正内容；由于 Draft #1 本身没有关联经验，
  旧经验不会被暂停，指令回执也不再谎称“旧经验已暂停”。
- 随后的 `验证 #3` 将 v3 发布为 `approved_auto`（private），v1 变为 `superseded`。

真实飞书消息触发该指令、以及卡片的 `纠正` 交互仍未验证，计入 F08 未完成项。

### 1.2 F08 运维演练（2026-11-07）

- **健康漏跑识别**：服务在日报时点后启动，`main.py health` 返回
  `ok:false`、`issues=["daily_heartbeat_missing"]`。
- **恢复**：执行调度器的 `_daily_job()` 后，`dailyHeartbeatAge` 约 1.7s、`workerHeartbeatAge` 约 7.2s，
  health 转为 `ok:true`、`issues=[]`。
- **已知缺口**：`main.py daily` 直接跑流水线但不写 daily heartbeat；本轮用 `_daily_job()` 恢复。
  这属于 F08 未完成项，不能宣称手工日报也已被健康探针覆盖。
- **备份/恢复**：SQLite backup API 复制运行库到隔离副本；两库行数一致
  （messages 6、tasks 1、reply_drafts 5、outbox 7、experiences 3、review_logs 3）；
  以副本和独立 heartbeat 路径运行 health，返回 `ok:true`。
- **进程重启**：服务多次重启后 WS 重连、历史补采与 outbox 恢复；已发送回执保持，
  重复事件未重复入库（见 intake 回归测试）。
- **仍缺**：真实飞书传输的“纠正”链路、撤权/群聊演练（浏览器会话不可用、无测试群）。

### 1.3 F09 阈值标定（准备态，未灰度）

- 生产阈值仍为自动 `0.85`、草稿 `0.55`；之前完全重复问题 BM25 仅 `0.2901`。
- 新增精确模式匹配：问题去掉空白/标点/大小写后与 `question_pattern` 或 pattern 完全一致时，
  分数提升到 `EXACT_MATCH_SCORE`（默认 `0.95`），并记录 `exact_pattern_match`；
  负例冲突检查在此之前，仍优先拦截。
- 测试：`test_exact_pattern_match_gets_calibrated_score`（精确 0.95，近义不抬高）与
  `test_exact_pattern_is_auto_eligible`（public + whitelist_auto + 阈值 0.85 下决策 `auto_eligible`）。
- **仍不构成 F09 验收**：未跑冻结留出集的精度/覆盖率统计，未获 owner 生产放行；
  生产 `.env` 仍为 `single_approval`、`auto_reply_enabled=0`。

## 2. 当前源码树门禁

- 当前树：`runs/F09/gate-20261107-1720.json`：`ok: true`；
- `sourceDigest = endSourceDigest = 5479bd835c9773d50ec4397602096972def5e9a9f43a68fa2e5d270cbce7353b`；
- default / utf8：1275 tests / 1273 passed / 2 skipped / 0 failed；飞书 78 tests；
- 历史：`live-gate-20261107-1233` 因基线测试节点被改名失败；恢复后 `1257` 通过；
  修复 `纠正` 指令后 `1342` 通过；EH01/EH02 后 `EH01/gate-1625` 通过；精确匹配校准后
  以 `F09/gate-1720` 为当前最终门禁；
- 实机缺陷修复带回归测试：`message_type` 兼容、卡片 1.0 结构、回调结果卡 1.0、纠正指令参数与回执、
  精确模式匹配校准。

## 3. 自动回复冒烟配置（已回退）

冒烟时临时设置：

```
FEISHU_DEPLOYMENT_MODE=whitelist_auto
FEISHU_AUTO_REPLY_ENABLED=1
FEISHU_SHADOW_MODE=0
AUTO_REPLY_THRESHOLD=0.2
DRAFT_MIN_THRESHOLD=0.1
```

同时把经验 #1 的 public v2 版本发布，受众 `chat_types=[p2p, group]`。随后：

- 决策记录：`auto_eligible`，原因 `retrieval:evoharness-bm25, all_hard_gates_passed`；
- `ReplyDraft #3`：`mode=auto`、`status=sent`；
- `Outbox #4`：`auto=1`、`status=sent`、回执 `om_x100...7ec`；
- 经验计数：`auto_reply_count=1`。

冒烟结束后已立即回退：

- `.env` 恢复 `single_approval`、`auto_reply_enabled=0`、`shadow_mode=0`，阈值恢复默认；
- 经验回滚到 v1（private），public v2 置 `paused`；
- 服务以安全模式重启。

**该冒烟不构成 F09 验收**：阈值是临时下调的，未使用冻结留出集，也没有 owner 的正式生产放行。

## 4. 关键缺陷与修复

1. 真实事件字段是 `message_type`，旧代码读 `msg_type`；已兼容并加回归测试。
2. 飞书网页版 3.9.32 不渲染卡片 2.0，日报退回占位图；卡片构建器与回调结果卡已切换为 1.0，
   实测网页端可渲染、按钮可回调。
3. 卡片 1.0 不允许把 `action` 放在 `column` 中（`230099/200410`）；已改为独立按钮行。
4. 生产阈值（`auto_reply_threshold=0.85`、`draft_min_threshold=0.55`）下，完全重复的问题
   BM25 得分仅 0.2901，会被降为影子草稿。**阈值与评分标定是 F09 的前置条件**，不能用冒烟值上线。
5. `纠正` 指令向 `correct_sent_reply` 传入未声明的 `reason`，运行即崩溃；已修复并补两条回归测试。
   无关联旧经验时，回执曾错误声称“旧经验已暂停”，现已按实际状态输出。

## 5. F08/F09 仍未完成

F08：

- 至少 7 个实际日报周期与真实人工裁决；
- 纠正已发回复（`纠正 #id 内容`）的真实回调链路；
- 进程重启、备份恢复、账号撤权、升级迁移演练；
- 健康探针识别漏跑/积压/授权失败的实机证据。

F09：

- 冻结留出集与影子统计，候选精度/覆盖率/严重错误达到门槛；
- owner 对自动发送的显式生产放行；
- 最小白名单灰度、停发、回退与更正演练；
- 阈值与 BM25 评分的正式标定，以及样本不足时维持 `single_approval` 的结论。

在上述条件满足前，生产配置保持 `single_approval`，不宣称受限自动回复已验收。
