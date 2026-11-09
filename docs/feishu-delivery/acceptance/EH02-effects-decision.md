# EH02：Effects 与抽取规则的证据化取舍

日期：2026-11-07 · 结论：**保留现有抽取/记账实现，不引入通用 Effects 队列；不启用影子草稿预生成。**

## 1. 测量方法

- 故障注入脚本：本机临时 `D:/claudecode/scratch/eh02_metrics.py`（不入仓库）。
- 清理后的证据副本：`runs/EH02/metrics.json`（运行根，脱敏、无用户文本；已剔除控制台面板，
  纯 JSON；原始故障注入输出留在本机 scratch）。
- 场景覆盖：重复领取/完成去重、恢复时重领、失败重试上限、供应商不可用、供应商失败、
  后台结果被下一窗口丢弃、使用记账失败。

## 2. 实测结果

| 场景 | 结果 | 含义 |
|---|---|---|
| 重复领取 | `duplicate_claimed=false` | 同租约内第二次领取被拒绝（不是“重放返回已记录结果”） |
| 完成去重 | `after_completed_claimed=false` | 已完成窗口不再领取 |
| 恢复重领 | 领取进程变更 → `failed`，再次领取成功，attempts=2 | store 级验证；`restore_session` 使用同参数，普通 `_pop` 仍等待 300s 租约 |
| 失败重试 | claims=`[true,true,true,false]`，最终 `max_attempts` | 最多 3 次，之后显式失败 |
| 供应商不可用 | `side_query_unavailable`，attempts 1 → 重试 attempts 2 | **代码中没有预算/Later 状态**；这里只证明“无 side_query 时会消耗尝试” |
| 供应商失败 | 持久为 `failed`，可再次领取 | 失败可见、可重试 |
| 后台结果 | 新 pending 写入后旧窗口 outcome `lost=true` | 旧窗口 completed/failed 被丢弃（不是覆盖同一窗口） |
| 使用记账失败 | 仅控制台 `print_error`；无持久队列 | `durable_extraction_records=0` 只证明未写 extraction 状态；usage 无队列由代码检查确认 |

## 3. 取舍

### 采用
- EH01 的按会话窗口持久化、原子写、领取去重、失败重试与崩溃恢复。
- 权限边界保持不变：仍走 `_online_evolution_enabled`、permission_mode 与 holdout 只读；
  skill 写入仍使用原 namespace / target / version 语义。

### 不采用（本轮）
1. **通用 Effects 队列**：当前只有两个后台效果（extraction、usage tracking）。EH01 已为
   extraction 提供单窗口持久状态；再建通用 `kind+key` 队列会引入第二套编排与两套状态机，
   与设计笔记“不能是第二套编排模型”的约束冲突。测量显示的缺口（供应商不可用会计入
   attempts、后台 outcome 丢弃、usage 无持久记录）是否造成主链路失败尚无生产日志；先作为
   条件化后续项记录，不据此宣称已修复。
2. **影子草稿预生成**：飞书侧已在审批前同步生成草稿，通用 Agent 侧没有只读预生成的
   稳定消费者；收益无法覆盖失效、成本上限与撤销逻辑的复杂度。
3. **抽取纯函数化**：`online_skill_eval` 的门槛/判定与观测台耦合仍高；本轮没有足够独立的
   调用方证据，强行抽取会把评测常量与业务状态拆散，保留原实现。

## 4. 未采用项的触发条件

- 供应商不可用若连续出现（≥3 个窗口因 `side_query_unavailable` 消耗 attempts），应新增
  `deferred` 状态并禁止计数，而不是放宽 `MAX_ATTEMPTS`；当前代码没有预算/Later 路径，
  不能把它宣称为预算暂缓。
- 后台 outcome 丢弃若在真实日志中出现，应改为按窗口 id 的 outcome journal（只读审计），
  不改变主窗口内容。
- 使用记账失败若影响晋升统计的完整性，再为 `usage` 增加同键幂等记录；当前无重试，故不宣称
  “使用记账去重”已覆盖。

## 5. 验收映射

- 采用/保留证据：本文 §2 与 `runs/EH02/metrics.json`；复核门禁 `runs/EH02/gate.json`
  （复用 EH01 同源码摘要 `ebfbdc3f...`，1273 tests / 1271 passed / 2 skipped）。
- 命名空间、版本语义、权限边界：§3 采用项，未改 skill 命名空间与发布版本逻辑。
- 重试条件项：EH02 **没有新接通自动重试**；EH01 的领取/恢复/失败重试只作为既有能力证据。
  重复领取拒绝、完成去重、恢复重领、失败上限已测量；预算/Later 语义与使用记账去重
  **未实现、未宣称完成**，见 §2/§4。
- 预生成条件项：未启用，故不需要失效/成本/撤销回归。
