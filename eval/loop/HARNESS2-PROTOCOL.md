# Harness² 实验设计约束（从实测失败里总结）

> 2026-10-26。本文件记录**为什么多次对照实验得到"全 0 分"**，以及以后怎么做才不会白烧 token。
> 每一条都来自本仓库实测，不是推测。

## 一、已发生的三次"无信号"实验（诚实记账）

| # | 规模 | 结果 | 判定 | 消耗 |
| --- | --- | --- | --- | --- |
| 1 | 1 题 1 轮 | baseline 100 / tight 0 | negative（但 n=1） | 0.15M |
| 2 | 2 轮 × 2 题 | baseline 50 / tight 0 | negative:quality-loss-at-same-cost | 0.52M |
| 3 | 3 轮 × 3 题（holdout 4） | baseline 0 / tight 0 | **uninformative:both-zero-baseline** | 1.38M |
| 4 | 2 轮 × 1 题（discriminating 子集） | baseline 0 / tight 0 | **uninformative:both-zero-baseline** | 0.44M |

合计约 2.5M tokens。**其中 #3、#4 是无效消耗**——不是没跑成，而是跑了但没有信息量。

## 二、根因分析（不是"模型不行"这么含糊）

1. **题池难度不匹配**：GAIA 165 题里 68 题对所有配置都失败、48 题都通过，
   只有 49 题（29.7%）有区分度（`eval/runs/holdout-calibration.json`）。
2. **样本太小**：即便从 49 题池里抽，n=1/n=2 时抽到"当前配置做不出"的概率仍然很高。
3. **我的第一次校准是错的**：`predictions.jsonl` 里**没有** `correct` 字段，
   初版校准脚本读不存在的字段 → 165 题全判失败 → 得出"GAIA 全 0 分"的荒谬结论。
   修正方式：调用仓库真实判分函数 `eval.run_benchmark.classify_record`，
   并加了回归断言"逐题判对必须等于 summary.correct（81）"。

## 三、以后的硬约束（写进流程）

在 `eval/harness2_run.py` 与 `eval/loop.py` 里已落实：

1. **先校准，再跑**：任何配置对照前必须先有 `holdout-calibration.json`，
   且 `verdict != no-signal`。
2. **只跑有区分度的题**：用 `--task-ids-file eval/runs/discriminating-ids.json`。
3. **n ≥ 30 才允许宣称**：低于此门槛一律 `not-claimable`（`eval/roi.py` 已有该判据）。
4. **两种配置都 0 分时判 `uninformative`**，不得写成"效果相同"或"省 token 更好"。
5. **失败 run 不得复用旧 curve**（`harness2_run.py` 已修：只在 exitCode==0 时读结果）。

## 四、正确的下一步（要花 token 的话该怎么花）

按性价比排序：

| 方案 | 做法 | 预估成本 | 信息量 |
| --- | --- | --- | --- |
| A. 换更简单的题池 | 从 always-pass 里挑"当前能做出"的题做配置对照（看**成本**差异） | ~1M | 高：能测出 token/延迟差异 |
| B. 分层统计 | 不追求"谁更好"，报告 always-fail/pass/discriminating 三层分布 | ~0 | 高：直接回答"为什么测不出" |
| C. 只在 49 题池上放大到 n≥30 | 需 30×2×2 次调用，约 25-40M | 25-40M | 中：可能得到有 CI 的结论 |

**推荐先做 A + B**：
- A 能证明"配置对成本的影响"（这是 RSI 权衡的另一半，且不依赖难度）；
- B 零成本，且能解释为什么之前的实验无信号。

C 放在 W14 正式阶段，且必须先在 W12 冻结协议。

## 四之二、方案 A 的实际结果（已执行，有信号）

用 always-pass 题池（当前配置做得出的题）跑 baseline vs tight（2 轮 × 1 题）：

| 变体 | pass@1 | tokens | 判定 |
| --- | ---: | ---: | --- |
| baseline | **50.0** | 0.40M | — |
| tight | **0.0** | 0.41M | **negative:quality-loss-and-more-cost** |

**关键**：tight 不但掉了 50pp 正确率，**token 还多了 2.5%**。

这条结果直接推翻"激进截断能省钱"的直觉。可能机制（未单独验证）：
截断后模型信息不足 → 重试/多轮探索 → 反而更贵。这是下一步值得做的假设检验。

同时也说明：**换对题池后，同一轴立刻有信号**——问题从来不是"配置无差异"，
而是"之前的题池测不出差异"（#3、#4 的 0 对 0）。

## 五、这件事对"试验场"叙事的价值

用户要的是把 EvoHarness 当试验场，探究哪种 harness 设计更好。本轮的教训说明：

> **试验场的第一性能力不是"跑实验"，而是"判断一个实验是否有信息量"。**

一个只会跑实验的试验场，会把 2.5M token 烧在 0 对 0 上还得出"没有差异"的结论；
能识别"无信号"的试验场，才知道什么时候该换题、什么时候该停、什么时候才值得扩大样本。
这正是本项目新增 `holdout_calibrate.py` 与三种 verdict 分类的原因。
