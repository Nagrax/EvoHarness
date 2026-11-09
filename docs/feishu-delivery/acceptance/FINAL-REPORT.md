# 飞书日报与经验回复交付报告

日期：2026-11-07 · 运行根：`D:/claudecode/.zcode/workflows/evoharness-feishu-delivery`

## 1. 结论

离线交付链 F00–F07 已在冻结树上完成并通过聚合门禁；2026-11-07 又用真实飞书应用打通了机器人私聊的最小链路：
消息入库 → 任务/问题 → 日报卡 → owner 批准 → 原消息回复回执 → 任务完成 → 候选经验 → 验证发布 → 重复问题检索。
F01 仍因群聊、话题、OAuth 自然人单聊、撤权与幂等矩阵未验证而 blocked；F08/F09 尚未达到验收完成。
另做了一次受控自动回复冒烟（临时阈值 + `whitelist_auto`），随后已回退到 `single_approval`。
可选的 EH01（抽取窗口持久化）与 EH02（Effects 证据化取舍）已完成并通过独立评审。

| 工单 | 状态 | 证据 |
| --- | --- | --- |
| F00 冻结基线 + 聚合门禁 | done | `runs/F00/gate.json`、`baseline.json`、`acceptance/F00.md` |
| F01 真实读取覆盖/发送身份 | blocked（私聊部分实测） | `acceptance/F01-blocked.md`、`acceptance/F08-F09-live-verification.md` |
| F02 持久接收/历史补采/迁移 | done | `runs/F02/`、`test_intake.py`、`test_migration.py` |
| F03 批示/outbox/回执恢复 | done | `runs/F03/`、`test_outbox.py` |
| F04 任务生命周期/可批示日报 | done | `runs/F04/`、`test_tasks_daily.py` |
| F05 版本化经验/EvoHarness 接缝 | done | `runs/F05/`、`test_experience.py` |
| F06 自动回复硬门/影子/急停 | done | `runs/F06/`、`test_policy.py` |
| F07 黄金回放/评测接线 | done | `runs/F07/`、`feishu-daily-agent/eval/runs/feishu-eval.json` |
| F08 真实只读日报试点 | pending（实机部分完成） | `acceptance/F08-F09-live-verification.md`、`F08-F09-live-evidence.json` |
| F09 受限自动回复灰度 | pending（仅冒烟） | 同上；未完成 7 周期、留出集与 owner 生产放行 |
| EH01 抽取窗口持久化 | done | `runs/EH01/`、`tests/test_pending_window_persistence.py` |
| EH02 Effects 证据化取舍 | done（保留原实现） | `runs/EH02/`、`acceptance/EH02-effects-decision.md` |

## 2. 最终门禁（当前源码树）

- 当前树产物：`runs/F09/gate-20261107-1720.json`；`ok: true`。
- `sourceDigest = endSourceDigest = 5479bd835c9773d50ec4397602096972def5e9a9f43a68fa2e5d270cbce7353b`。
- default / utf8 各 **1275 tests / 1273 passed / 2 skipped / 0 failed**；exit 0；`ast.ok=true`；两环境 nodeIds/skip 一致；baseline 子集校验通过。
- 子套件：根 1197 tests（1195 passed / 2 skipped）；飞书 78 tests（0 failed，含精确匹配校准）。
- 飞书实机链路门禁为 `runs/F08/live-gate-20261107-1342.json`（digest `cc329c54...`）；
  EH01/EH02 证据冻结在 `ebfbdc3f...`；当前树由 `F09/gate-20261107-1720` 整体复核。
- `STATE.json` 为 9 done、1 blocked（F01）、2 pending（F08/F09）。

## 3. 实机链路（2026-11-07，Asia/Shanghai）

应用已发布 `1.0.2`，机器人启用；长连接、`im.message.receive_v1`、`card.action.trigger` 均真实收到。
真实标识与回执在验收文档内脱敏，原始消息与日志只在本机 `data/agent.db`。

| 时间 | 动作 | 结果 |
| --- | --- | --- |
| 11:35 | 用户发送任务与问题 | 入库；Task #1 待确认，Draft #1 pending |
| 12:00 | 日报卡送达 | 回执 `om_x100...1840` |
| 12:04 | 卡片“批准” | 真实回调；Draft #1 → sent，回复回执 `om_x100...f632`，原会话出现回复 |
| 12:08 | 日报修订版 | 回执 `om_x100...ba5e` |
| 12:10 | 卡片“标记完成” | Task #1 → done，token 已消费，review/task 事件落库 |
| 12:15 | 卡片“验证” | Experience #1 → approved_auto（private） |
| 12:16–12:21 | 重复问题 | EvoHarness BM25 命中经验，相似度 0.2901；生成待批影子草稿 |
| 12:19 | 受控自动回复冒烟 | `auto_eligible`，outbox auto=1，回执 `om_x100...a7ec` |
| 12:20 | 冒烟回退 | 恢复 `single_approval`；public v2 回滚为 paused；服务安全模式重启 |
| 13:2x | owner 指令纠正（处理器直调，非飞书传输） | 生成候选 #3；`验证 #3` 后 v3 approved_auto，v1 superseded |

实机发现并修复的产品缺陷：

1. 事件模型字段实际为 `message_type`，旧代码读 `msg_type`；已兼容并加回归测试。
2. 飞书网页版 `3.9.32` 不渲染卡片 2.0；日报、草稿、经验卡和回调结果卡统一切换为 1.0。
3. 卡片 1.0 不允许 `action` 嵌在 `column`（`230099/200410`）；改为独立按钮行。
4. 生产阈值（自动 0.85 / 草稿 0.55）下，完全重复问题原 BM25 仅 0.2901。
   已新增精确模式匹配校准：去空白/标点时分数提升到 `0.95`，并记录 `exact_pattern_match`；
   近义问题不抬高，负例冲突仍优先。`test_exact_pattern_is_auto_eligible` 证明在
   public + whitelist_auto + 0.85 下可 `auto_eligible`。生产仍关闭自动发送，需 F09 留出集与放行。
5. `纠正` 指令向 `correct_sent_reply` 传入未声明的 `reason`，运行即崩溃；已修复并补两条回归测试。
   无关联旧经验时，回执曾错误声称“旧经验已暂停”，现已按实际状态输出。

## 4. 已实现的关键机制

- 实时/历史共用一个准入与 inbox；未知 bot 身份、空白名单、撤权、非允许内容不进入；编辑/撤回清决策并重判。
- 来源水位按页持久化，失败可续跑；旧库 v1→v3 迁移，v2 只补 category，备份与重入测试覆盖。
- 审批与 outbox 同事务；一次性 token、可信 operator、版本绑定；approve/edit 仅 PENDING 原子抢占；reject 取消未领取意图；`delivery_unknown` 对账、429/永久错误分离、租约恢复。
- 日报逻辑日 Asia/Shanghai，跨天任务保留，重复执行幂等，迟到数据重算最近一天并产生补记意图；已发回复按真实回执时间归属。
- 版本化经验、受众/负例/有效期/来源；审批只沉淀候选，发布与回滚独立；卡片编辑/纠正带必填输入；回滚绑定 head/version。
- 自动回复硬门：授权、影子、类别、内容安全、注入、回声、过期、每日限额原子预留、暂停/撤权预检；模型只产出证据/草稿。
- 经验检索复用 EvoHarness `agents.skills.rank_corpus_for_query` 的受控语料接口，实测引擎为 `evoharness-bm25`。
- 黄金回放直接调用生产 `policy.evaluate`；12 case（dev 10 / holdout 2），报告 chat/category/time 分组、四策略逐 case 证据、Wilson 95% 下界与样本限制。

## 5. 复现命令

```powershell
cd D:\claudecode\EvoHarness\feishu-daily-agent
# 离线测试（76 tests）
.venv\Scripts\python.exe -m pytest tests -q
# 真实服务（需要本机 .env，凭据不入仓库）
.venv\Scripts\python.exe main.py
# 手动日报 / 初始化
.venv\Scripts\python.exe main.py daily
.venv\Scripts\python.exe main.py init-db
# 黄金回放
.venv\Scripts\python.exe eval\replay.py --run
.venv\Scripts\python.exe eval\replay.py --check
# 聚合门禁（约 20-25 分钟；运行期间不要改源码）
cd D:\claudecode\EvoHarness
python .zcode\workflows\evoharness-feishu-delivery\gate.py run `
  --repo D:/claudecode/EvoHarness `
  --out .zcode\workflows\evoharness-feishu-delivery\runs\F08\live-gate-<ts>.json `
  --task F08 --baseline .zcode\workflows\evoharness-feishu-delivery\baseline.json
# 状态
node .zcode\workflows\evoharness-feishu-delivery\state.mjs --root .zcode\workflows\evoharness-feishu-delivery list
```

## 6. 未完成/已知限制

1. F01 仍需验证群聊 @ / 群无 @、同群话题、OAuth 自然人单聊、撤权与写探针幂等矩阵。
2. F08 仍需至少 7 个真实日报周期、经飞书传输的真实纠正链路、撤权/升级演练；
   进程重启、SQLite 备份恢复、健康探针识别 `daily_heartbeat_missing` 并恢复已演练。
   另：`main.py daily` 不写 daily heartbeat，手工日报健康覆盖仍是缺口。
3. F09 仍需冻结留出集、影子统计、阈值标定、owner 显式生产放行和停发/回退/更正演练；样本不足时维持 `single_approval`。
4. 精确重复问题已校准到 0.95，但近义问题仍低分；未做冻结留出集的精度/覆盖率统计，
   不得用单测或冒烟阈值对外宣称自动回复精度或灰度。
5. EH01 已完成：抽取窗口按会话落盘、CLI/Web 恢复、领取去重、失败重试、holdout 只读；
   EH02 以故障注入证据选择保留原实现，未引入通用 Effects 队列。EH02 明确未完成项：
   无预算/Later 状态（无 side_query 会消耗 attempts）、后台 outcome 可能丢弃、usage 失败无持久队列；
   见 `acceptance/EH02-effects-decision.md`。
6. `ModelLedger` 与预算配置已就绪，但运行路径未强制逐次记账，不能宣称预算硬门完成。
7. failure-stability 最新产物仅 1 题×2 遍，只用于恢复健康检查；10 题×2 遍因 provider 配额污染已归档，不能作为稳定性结论。
8. 黄金集 12 case 只用于回归与硬门，不足以支撑对外精度宣称；F09 的留出集/影子统计未执行。

## 7. SuperAgent 对标

2026-11-07 复查 `git ls-remote`：upstream HEAD/main 仍为 `8c32a9d`，无更新。已落地事务语义（outbox、提交后发送、回执恢复、两级准入、reason code、prompt/model/snapshot 记录）；Ticket/Mandate/多 Session 编排仍不引入。详见 `docs/design/对标笔记_dsh-superagent_2026-11-06.md`。
