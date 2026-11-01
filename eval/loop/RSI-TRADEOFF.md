# EvoHarness 作为 Agent 架构试验场：RSI 权衡与 Harness² 落地

> 2026-10-26。本文回答三个问题：**（1）只迭代 skill 的收益能否对冲 token 成本；
> （2）Harness² 能否复现；（3）记忆体系有没有被真正评测。**
> 所有数字都有落盘文件或可复跑脚本，标"未执行"的地方没有做。

---

## 一、先把问题定义清楚

用户的核心质疑是准确的：

> 这个项目最大的问题是只迭代 skill。skill 迭代的收益能不能对冲多消耗的 token？
> 而 RSI 论文真正要回答的是：**是否值得花这么多 token 去优化 harness。**

这不是一个"分数越高越好"的工程问题，而是一个**成本-收益核算问题**。
因此本仓库新增了专门做核算的评测器，而不是继续加功能。

### 1.1 判据（写进代码，不靠自觉）

| 量 | 定义 | 文件 |
| --- | --- | --- |
| `tokensPerCorrect` | 总 tokens ÷ 正确题数 = 每答对一题烧多少 | `eval/roi.py` |
| `ROI` | Δcorrect ÷ ΔtokensM = 每多花 1M token 换来几道对的题 | `eval/roi.py` |
| `verdict` | profitable / loss / not-claimable / neutral | `eval/roi.py` |
| 红线 | Δcorrect<0 时省 token **不算收益**；CI 跨 0 一律 inconclusive | `eval/roi.py: redLines` |

---

## 二、问题 1：skill 迭代的 ROI —— 现在能算，但还不能下结论

### 2.1 已跑通的真机事实（W13 pilot）

| 项 | 实测 | 来源 |
| --- | --- | --- |
| 规模 | 2 轮 × 3 臂 × 2 题 | `eval/runs/evolution/curve.json` |
| 总 tokens | 2.52M（其中 side_query 0.174M） | 同上 |
| 进化触发 | no_evo=False / ungated=True / gated=True | 同上 |
| 新沉淀 skill | 0 / 1 / 1 | 同上 |
| holdout 只读 | 三臂全部通过 | 同上 |
| 进化自身开销占比 | **6.9%** | `eval/roi.py evolution` |

**关键发现**：进化本身的额外开销只占 6.9%（side_query 是抽取与判定），
真正的大头一直是**主线推理**。所以"进化太贵"这个直觉，在 token 结构上不成立；
贵的不是进化，是"每题都要跑完整 agent 循环"。

### 2.2 但 ROI 判 not-claimable，原因是样本

`eval/roi.py` 输出：

```
ungated_evolution  Δpass=+100.0  ΔtokM=+0.2  roi=500.0  verdict=not-claimable:undersampled
gated_evolution    Δpass=+50.0   ΔtokM=+0.1  roi=500.0  verdict=not-claimable:undersampled
```

n=2 时任何"profitable"都必须降级。这是**有意的防自欺**：小样本上的 500.0 ROI 是噪声，
不是发现。要把它变成结论，必须走 W14 的正式规模（10-15 轮、独立未见族）。

**所以问题 1 的诚实答案是**：机制已能核算 ROI，真机 pilot 证明管线与隔离成立，
但"skill 迭代是否划算"在现有样本量下**无法回答**，不能宣称。

---

## 三、问题 2：Harness² —— 已复现"优化 harness"这一步，且已有负结果

### 3.1 复现了什么

不是复现论文数字（没读原文，不能编），而是复现**机制**：让 harness 优化 harness 自己。

| 能力 | 状态 | 证据 |
| --- | --- | --- |
| 把 harness 配置暴露成可优化轴 | 已实现 | `eval/harness_squared.py` 3 轴 9 变体 |
| 轴真的可配置（非纸面） | 已实现 + 回归 | `tests/test_harness_axes_configurable.py`（16 条） |
| 每轴生成可执行实验计划 | 已实现 | `eval/harness2/plan.json` |
| 跑变体对照并量化 Δcorrect/Δtokens | 已实现 | `eval/harness2_run.py` |
| ROI 分析与负收益判定 | 已实现 | `eval/harness2_analyze.py` |

**这一步之前是不成立的**：`MAX_RESULT_CHARS` 与 `AUTO_COMPACT_THRESHOLD` 都是硬编码常量，
`harness_squared.py` 却把它们列为"可配置轴"。那等于在测空气。现已改为真实 env 覆盖，
非法值安全回退（0/负数/NaN/越界都测过）。

### 3.2 已有真实结果（含负结果，这是好结果）

三轴各跑 baseline vs 单个变体。汇总见 `eval/runs/harness2/summary.json`。

**先看 1 题规模（早期探索）**：

| 轴 | 变体 | pass | tokens | 省 token | 判定 |
| --- | --- | --- | ---: | ---: | --- |
| tool_policy | tight（截断 20k） | 100 → **0** | 0.15 → 0.13M | 13.3% | negative:quality-loss-outweighs-savings |
| memory_policy | recall15 | 0 → 0 | 0.15 → 0.13M | 13.3% | uninformative:both-zero-baseline |
| context_policy | early-fold（0.55） | 0 → 0 | 0.16 → 0.16M | 0% | uninformative:both-zero-baseline |

**再看 2 轮 × 2 题（同一 tool_policy 轴放大后，结论变了）**：

| 轴 | 变体 | pass | tokens | 省 token | 判定 |
| --- | --- | --- | ---: | ---: | --- |
| tool_policy | tight | 50 → **0** | 0.52 → 0.52M | **0%** | **negative:quality-loss-at-same-cost** |

**这是本轮最重要的一条发现**：放大样本后，tight 变体**根本不省 token**（0.52M 对 0.52M），
却把正确率从 50% 打到 0%。

也就是说，1 题规模下"省 13.3% token"的观感，在更大样本上消失了——
token 节省是单题噪声，而质量下降是稳定方向。这正好演示了为什么
**小样本 ROI 必须判不可宣称**：否则会把噪声当成收益来源。

三条判定各自说明一件事：

1. **negative（同成本掉质量）** —— 最纯粹的负结果：没省到钱，还掉了质量。
2. **uninformative** —— 两种配置都做不出这道题时，判"不可判"而不是"同质量更便宜"。
   后者会把"省下了失败的成本"包装成效率提升，是本项目刻意拒绝的话术。
3. **claimable = 0** —— 没有任何一条能宣称优劣。这是**正确**的：n≤2 远低于 n≥30 门槛。

这套判别器的价值不在"给出结论"，而在**阻止错误结论**。

### 3.3 还不能复现的部分（必须写清）

- **论文数字**：未读原文（本机 web_search 无凭据），不复现、不引用任何论文数值。
- **任意代码改写**：当前只支持"配置轴"，不支持 harness 自动改写自身源码。
  后者需要 mutation 生成 + 全量回归 + 回滚三段，W11/W19 才有相应基建。
- **规模**：每轴每变体仅 1 题 1 轮。扩到 n≥30 后 `eval/roi.py pair` 才能给 CI。

---

## 四、问题 3：记忆体系是否被评测过 —— 之前是"被间接评测"，现在是"被直接压测"

### 4.1 之前的状态（诚实说法）

LoCoMo（1,540 题）证明**召回质量**有改善（F1 13.96→33.01），
但它测的是"答得对不对"，**没有**把记忆机制本身当被测对象。
所以"记忆体系经过检验"这句话，之前只对"结果"成立，对"机制"不成立。

### 4.2 现在的状态（`eval/memory_eval.py`，7/7 通过）

把设计文档里每条约束变成可复跑断言：

| 编号 | 机制 | 断言 |
| --- | --- | --- |
| M1 | 四类 type | user/feedback/project/reference 都能正确解析 |
| M2 | 索引自动重建 | MEMORY.md 重建且行数不超上限 |
| M3 | 召回三道门槛 | 中文整句触发、英文多词触发、短命令拒绝、空输入拒绝 |
| M4 | 有界头部读取 | 读字节数 < 候选总量（不整读正文） |
| M5 | 预算 | 单文件 4096B、单会话 60KB |
| M6 | 回指宽松匹配 | 整行清单/代码围栏/路径前缀/裸文件名**四种写法都能命中** |
| M7 | 注入格式 | 带 Memory 标签且非空 |

**成本量级已量化**：候选扫描只读 320 字节，注入 199 字符（约 60-130 tokens）。
这给出一个可对线面试的数字：记忆注入的成本是**百 token 量级**，不是千 token 量级。

### 4.3 前沿设计对比（可移植性判断）

> 说明：以下对比基于本项目已有实现与公开常识，**未做外部论文检索**（本机搜索不可用），
> 因此不引用任何具体论文数值；标"待核实"的需读原文后再下结论。

| 设计 | 核心机制 | 与本项目差异 | 可移植性 |
| --- | --- | --- | --- |
| 本项目（文件 + 索引） | markdown + MEMORY.md + 两段式召回 | 零依赖、可人读可 diff | 基线 |
| Mem0 类（记忆服务） | 抽取→结构化→向量检索→生命周期 | 多一层服务与向量库 | 中：可作为**可选加速层**，保留文件兜底 |
| Letta/MemGPT 类 | 分页记忆 + 自管理上下文 | 核心是 agent 自管内存分页 | 低：需改主循环语义 |
| 结构化记忆表 + 向量 | 原始事件层/结构化层/索引层/生命周期 | 本项目缺"生命周期"与"冲突消解" | 高：可先补生命周期，不引向量 |

**判断**：最值得先移植的不是向量，而是**生命周期管理**（合并重复、冲突消解、
过期降权）。理由：本项目已有 `[type]` 与 provenance，但**没有**过期与冲突策略；
而向量只影响"找得到"，生命周期影响"存得对"——后者才是当前瓶颈。

---

## 五、把 EvoHarness 当试验场的架构

现在的闭环有**五个信号源**，全部可复跑：

```
backend 契约   ─┐
frontend 逐按钮 ─┤
module 扫描     ─┼→ eval/loop.py BACKLOG → 判读 → 修复+回归 → 复测 → done
memory 逐机制   ─┤
harness² 配置   ─┘
                ↓
        eval/roi.py（成本-收益核算）
```

对应的架构对比问题（"Claude Code 重封装 vs pi-agent 轻量 vs DSH 插件式"）：
**本仓库现在能做的不是站队，而是把它们都变成同一批任务下的可测变体**——
因为优化轴已经可配置、成本已可核算。这才是"试验场"的实际含义。

---

## 六、下一步（不需要用户确认）

1. **W14 正式规模**：把 pilot 扩到 10-15 轮 + 独立未见族，才能对"skill ROI"下结论。
2. **Harness² 全轴对照**：9 个变体跑完（约 15 分钟/轮），拿到每个轴的 Δcorrect/Δtoken 曲线。
3. **补记忆生命周期**：合并/冲突/过期三件事，做完再用 `memory_eval.py` 加 M8/M9 断言。
4. **W17**：无真实人标就 blocked，不得用模型生成替代。

## 七、当前硬事实

- 门禁：`626 tests / 624 passed / 0 failed / 2 skipped`，双编码一致。
- 300M 台账：已用 2.77M，剩 297.2M（`eval/runs/live-w13/LIVE_BUDGET.json`）。
- 端点模型：`aggregate/glm-5-3-flash`（原 `zcode/GLM-5.3-Flash` 遇 429 并发限流）。
