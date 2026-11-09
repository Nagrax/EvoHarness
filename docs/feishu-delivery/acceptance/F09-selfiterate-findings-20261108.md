# F09 自迭代轮新发现（2026-11-08 早晨，模拟流量驱动）

## 1. 白名单自动窗口首次真实打通（生产阈值）

2026-11-08 08:05-08:08（CST）演练窗口，配置：`whitelist_auto` + `auto_reply_enabled=1`
+ 生产阈值 `AUTO_REPLY_THRESHOLD=0.85` / `DRAFT_MIN_THRESHOLD=0.55`（**未下调**），
exp#4 公开化（visibility=public + sensitive_level=internal + audience chat_types=[p2p]）：

- 10 条 exact 问题（"请问年假怎么申请？"）：**10/10 `auto_eligible` → outbox auto=1 全部 sent**；
- 决策记录：`retrieval:evoharness-bm25, exact_pattern_match, all_hard_gates_passed`，相似度 1.574；
- 窗口结束全部回退：配置回 `single_approval`/disabled，exp#4 回 private/private_level。

## 2. 首个真实标定发现（自迭代闭环产出）

同窗口 1 条近义变体（"年假要怎么申请呀"）得分 **0.978 ≥ 0.85** 被自动发送（expected 0）：

- 定位：BM25 原始分数量纲随语料变化（exact=1.574、近义=0.978），固定阈值 0.85 无法区分；
- 影响：语义正确（答案本就是年假簇答案），但触发不精确——按 playbook 口径属于"会实际发出
  的候选"中的不精确触发，会计入 precision 分母；
- 结论：**近义评分标定是 F09 灰度的硬前置**（与手册既有判断一致），生产必须保持
  `single_approval` 直到完成标定与 ≥60 裁决候选统计；
- 工单化：标定方案建议——(a) exact 命中直接钳制 0.95、非 exact 一律按归一化分数重标定；
  或 (b) 相似度分数整体归一化到 [0,1] 后重定阈值。两者都需冻结留出集回归验证。

## 3. 演练踩出的实现级约束（已修，均有证据）

1. 公开化必须同时设置 `sensitive_level ∈ {public, internal}`，否则 `_audience_allows`
   的公开分支把经验滤出语料 → 整窗零命中（23:37 与 23:42 两轮空转的根因）；
2. auto 窗口必须保持到「全部问题摄取 + 决策/发送收敛」才能回退——WS 事件分钟级迟到时
   提前回退会让决策跑在回退后的配置上（23:26 轮的根因）；
3. 窗口前预检必须在公开化之后执行（与窗口内服务同状态），预检不过直接放弃窗口；
4. 纠正指令必须选簇匹配的已发送草稿，否则答案会挂到别的意图簇（exp#11 污染案例，
   已用真实「停用 #11」指令修复，验证了停用-暂停路径）。

## 4. 当前口径（不宣称）

- auto 窗口是**演练**：owner 授权的模拟流量 + 临时窗口，窗口后已回退；生产 `.env` 保持
  `single_approval`、`auto_reply_enabled=0`；
- precision 样本 10（非 60），verdict 维持 `insufficient_sample_keep_single_approval`；
- 7 个模拟逻辑日周期已真实投递（outbox #24-30），台账逐条标注 simulated。
