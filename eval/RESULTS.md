# EvoHarness 评测结果（真实自测数据，定稿 2026-09-18）

> 本文替换原"评测部分.md"中的论文引用数字。原数字（GAIA 53.3% / HLE 20.2% / 关折叠 44.7%）来自 deepAgents 论文，非本项目实测，已废弃。
> 所有数字由 `eval/run_benchmark.py` 产生，逐题预测文件在 `eval/runs/`，可复现。

## 总览

| 基准 | 口径 | Pass@1 | 消耗 |
|---|---|---|---|
| GAIA | 165 题全量 | **51.5%** | 89.7M |
| GAIA 折叠消融（阈值 0.30） | 165 题配对 | 40.0%（p=0.011） | ~90M |
| HLE | n=75 有效 | 9.3% | 14.1M |
| LoCoMo（记忆子系统） | 1,540 题第三方 harness | 详见下文四 | ~60M |

统一条件：DeepSeek-V3.2（paratera）、temperature 默认、bypassPermissions、每题独立会话、看门狗超时计错。

## 一、GAIA（165 题，DeepSeek-V3.2，4 workers，60 turns / 1200s 看门狗）

**Pass@1 = 51.5%（85/165）**

| 维度 | 通过率 |
|---|---|
| file 类（38 题） | 63.2% |
| text 类（103 题） | 48.5% |
| mm 类（24 题） | 45.8%（注：Chromium 未装，浏览器题全部失败，此为下界） |
| Level 1 / 2 / 3 | 60.4% / 44.2% / 57.7% |

失败构成：19 题看门狗超时、12 题上下文超限（413）、若干无 Final Answer 标记。总消耗 89.7M tokens。

## 二、记忆折叠消融（同 165 题，McNemar 配对）

| 组 | Pass@1 | 折叠触发 |
|---|---|---|
| 默认阈值 0.70 | **51.5%** | 0 次（机制名存实亡） |
| 强制阈值 0.30 | 40.0% | 27 题共 35 次 |

- McNemar 精确检验：51 个不一致对（35 默认胜 / 16 折叠胜），**p = 0.011**
- 发生折叠的 27 题：折叠组通过率 29.6% vs 同批题默认组 44.4%；折叠组超时 39 题 vs 默认 19 题
- **结论：当前折叠机制在两个 regime 均无可测量正收益**——0.70 阈值从不触发（12 题死于本可由折叠避免的上下文超限），0.30 阈值显著伤害完成率。根因假设：阈值 dead-zone + 折叠状态保真度不足（工具经验/文件路径丢失导致返工）。这是机制重构的量化依据。

## 三、HLE（定稿：n=75 有效样本）

**Pass@1 = 9.3%（7/75）**。分类别：multipleChoice 20.0%（4/20）、exactMatch 5.5%（3/55）、mm 15.0%（3/20）、text 7.3%（4/55）。35 题看门狗超时（600s 口径）计错。

口径说明：原 100 题子集中 25 题因 API 平台收回 DeepSeek-V3.2 team 权限（403 team_model_access_denied）未能运行——属"未跑"而非"跑错"，已从定稿数据剔除并归档至 `eval/runs/archive-smoke/hle-excluded-api403.jsonl`。HLE 为 frontier-killer 基准（顶级前沿模型公开成绩约 10-25%），9.3% 为 DeepSeek-V3.2 + 600s 超时口径下的真实量级。API 恢复后可 `python -m eval.run_benchmark run --dataset hle --limit 100` 断点续跑补齐 100 题全口径（约 10M tokens）。

## 四、评测暴露并修复的生产 bug（证明链）

| # | bug | 症状 | 修复 |
|---|---|---|---|
| 1 | `memory.py` 召回精确文件名匹配 | 选择器照抄 manifest 行时召回全部静默丢失（LoCoMo F1 13.96→33.01） | 规范化宽松匹配，单测 6 项入 CI |
| 2 | `tools.py` run_shell 默认 UTF-8 解码 | Windows GBK 命令输出导致进程崩溃（两个评测批次死于它） | `errors="replace"` |
| 3 | 评测 runner MCP 子进程泄漏 | 每题 3 个 MCP server 进程不清理，累积数百进程耗尽系统资源 | runner 每题 finally 清理（`tools.py` 侧亦可考虑复用连接） |
| 4（已修，**待验证**） | 折叠阈值 0.70 永不触发 | 165 题 0 次折叠 + 12 题 413 | 两项修复已提交：①折叠 prompt 强制保留文件路径/已否定结果/下一步动作 + 折叠存档可恢复提示（`agent.py` `_append_fold_archive_hint`）；②`MODEL_CONTEXT` 补 DeepSeek-V3.2=128k。**验证评测待跑**（20-30 题快速消融，~15M tokens），结论以复测为准 |

## 五、其他评测资产

- **LoCoMo 记忆评测**（1,540 题，MiniMem 第三方 harness）：文件型记忆与 embedding 基线总分持平（judge 50.8% vs 52.7%），检索条数 1/5、上下文 -62%，Temporal/Multi-hop 反超。报告：MiniMem 仓库 `benchmarks/locomo_bearcode/RESULTS.md`
- **Skills 在线评测**：数据闭环已验证（replay→judge→promotion 门控正确拒绝 50% 通过率候选）

## 六、复现

```bash
python -m eval.run_benchmark run --dataset gaia --workers 4 --max-turns 60 --timeout 1200          # 主跑
python -m eval.run_benchmark run --dataset gaia --workers 4 --max-turns 60 --timeout 1200 --fold-threshold 0.30  # 消融
python -m eval.run_benchmark score --dataset gaia    # 判分（gaia + gaia-nofold tag）
```
