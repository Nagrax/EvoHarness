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

### 冻结口径复算（2026-10-19，离线重评分，零模型）

上表为历史报告口径（`pass@1 = 正确 / 可判分`，把超时/执行错误/无最终答案排除出分母）。现按
**冻结分母政策**对同一批逐题预测离线复算，主指标把系统未完成的代价计入失败：

| 基准 | 计划 | 平台不可用 | 可判分 | 正确 | 超时 | 执行错误 | 无答案 | **pass@1** | 95% CI | 有效作答准确率 | 唯一产物 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| GAIA | 165 | 0 | 127 | 81 | 19 | 17 | 2 | **49.1%** | 41.2–57.0 | 63.8% | `eval/runs/gaia/scores/20261019T083454Z-c24288.json` |
| HLE | 75 | 0 | 32 | 8 | 35 | 3 | 5 | **10.7%** | 4.0–17.3 | 25.0% | `eval/runs/hle/scores/20261019T083458Z-f93a86.json` |

- 分母政策：`pass_at_1 = 正确 /（计划 − 平台不可用）`；超时/执行错误/无最终答案均为失败；平台不可用为预先声明、与系统能力无关，单列并从分母剔除。
- 判分器版本固定 `matcher:gaia_match+hle_match@2026-09-18`；bootstrap 95% CI 固定种子 `20260119`、`2000` 次重采样，可离线复算。
- 复算命令（零模型）：`python -m eval.run_benchmark score --dataset gaia` / `--dataset hle`。
- 历史 51.5% / 9.3% 与本次冻结口径**不可直接比较**（分母不同，且逐题预测快照经 `predictions_sha256` 绑定；历史 summary 已留档为 `summary.json.octo-baseline`）。跨版本比较前须核对 `judge_version` 与预测哈希。

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
- **证据版根因分析（同题对照 + 38 个折叠存档审计）**：同 27 道折叠题，默认组 577k input / 5 题 413 / 3 题超时 / 通过 44.4%；折叠组 1017k input（+76%）/ **0 题 413 / 0 题超时** / 通过 29.6%。折叠存档审计：79% 保留路径信息、next_actions 几乎不缺——**"状态丢失返工"假设被推翻**
- **修正后的机制评价**：折叠在"保活"上完全有效（消灭同批题全部 413 与超时），在"保质"上为负——任务存活做完但答案质量下降，token 成本 +76%。真实根因指向：①摘要式延续造成的推理链断裂（多步推理被压缩后继续，末段答案劣化）②成本结构（每次折叠 = 80k 全量 transcript 输入的 LLM 调用）
- **修复方向（待验证，按证据排序）**：①折叠输入只取旧消息段、保留最近 N 轮原文不折（同时降本与保推理连续性）②异步预折叠摊平延迟 ③质量修复优先于触发时机调整

## 三、HLE（定稿：n=75 有效样本）

**Pass@1 = 9.3%（7/75）**。分类别：multipleChoice 20.0%（4/20）、exactMatch 5.5%（3/55）、mm 15.0%（3/20）、text 7.3%（4/55）。35 题看门狗超时（600s 口径）计错。

口径说明：原 100 题子集中 25 题因 API 平台收回 DeepSeek-V3.2 team 权限（403 team_model_access_denied）未能运行——属"未跑"而非"跑错"，已从定稿数据剔除并归档至 `eval/runs/archive-smoke/hle-excluded-api403.jsonl`。HLE 为 frontier-killer 基准（顶级前沿模型公开成绩约 10-25%），9.3% 为 DeepSeek-V3.2 + 600s 超时口径下的真实量级。API 恢复后可 `python -m eval.run_benchmark run --dataset hle --limit 100` 断点续跑补齐 100 题全口径（约 10M tokens）。

## 四、评测暴露并修复的生产 bug（证明链）

| # | bug | 症状 | 修复 |
|---|---|---|---|
| 1 | `memory.py` 召回精确文件名匹配 | 选择器照抄 manifest 行时召回全部静默丢失（LoCoMo F1 13.96→33.01） | 规范化宽松匹配，单测 6 项入 CI |
| 2 | `tools.py` run_shell 默认 UTF-8 解码 | Windows GBK 命令输出导致进程崩溃（两个评测批次死于它） | `errors="replace"` |
| 3 | 评测 runner MCP 子进程泄漏 | 每题 3 个 MCP server 进程不清理，累积数百进程耗尽系统资源 | runner 每题 finally 清理（`tools.py` 侧亦可考虑复用连接） |
| 4（部分修复，**待验证**） | 未知模型窗口 fallback=200k 高估 | 165 题 0 次折叠 + 12 题 413 | ①fallback 改保守值 128k（不对称错误：高估→413 不可恢复，低估→早折叠可恢复）+ `EVOHARNESS_CONTEXT_WINDOW` 覆盖，换 API 不再依赖字典条目；②折叠 prompt 路径保留强化（存档审计显示 21% 折叠丢路径，属安全措施而非主修复）+ 折叠存档可恢复提示。**主修复方向（保推理连续性：只折旧消息段）待实现与验证** |

## 五、其他评测资产

- **LoCoMo 记忆评测**（1,540 题，MiniMem 第三方 harness）：文件型记忆与 embedding 基线总分持平（judge 50.8% vs 52.7%），检索条数 1/5、上下文 -62%，Temporal/Multi-hop 反超。报告：MiniMem 仓库 `benchmarks/locomo_bearcode/RESULTS.md`。**证据等级：C（外部 harness + 数据集不在本仓库，无法在此复跑）**
- **Skills 在线评测**：数据闭环已验证（replay→judge→promotion 门控正确拒绝 50% 通过率候选）

## 六、证据强度分级（2026-10-28 追加）

本节回答一个必须回答的问题：**上表每个数字能撑多重的结论**。
分级规则来自 `eval/FROZEN_PROTOCOL.json` 的 statistics 与 `eval/lifecycle_noise_gate.py` 的跨批次判读。

| 等级 | 判据 | 可用于 |
| --- | --- | --- |
| **A** | 本仓库可复跑 + 冻结口径 + 唯一产物绑定 + 95% CI + 跨批次方向一致 | 对外声明 |
| **B** | 本仓库可复跑 + 冻结口径 + CI，但只有单批次（未做跨批次复核） | 内部结论，对外须标注"单批次" |
| **C** | 依赖外部 harness / 数据集不在仓库 / 无法本地复跑 | 只能引用，不得作为本项目能力证据 |
| **D** | 样本不足（n<30）、或差异落在配置噪声内、或方向跨批次反号 | **不可宣称**，只能列为待验证 |

逐项标注：

| 结论 | 等级 | 依据 |
| --- | --- | --- |
| GAIA 165 题 pass@1 49.1%（CI 41.2–57.0） | **A** | 全量、冻结口径、唯一产物 `scores/20261019T083454Z-*`、CI 可离线复算 |
| HLE n=75 pass@1 10.7%（CI 4.0–17.3） | **A** | 同上（有效样本 32，CI 已计入） |
| 折叠消融（阈值 0.30 降 11.5pp，McNemar p=0.011） | **C** | 配对检验显著，但**产物 eval/runs/gaia-nofold/ 已不存在**，只剩文档记录（documentOnly）→ 不可核对 |
| LoCoMo 持平 + token −62% | **C** | 外部 harness，数据集不在本仓库 |
| 生命周期 soft 臂**消除污染**（−2 事件，两批极差 0） | **A** | `memory_lifecycle_ab` 两批独立复现，噪声门判 claimable |
| 生命周期 soft 臂**提升召回** | **D** | 跨批次方向反号（+3.3 / −6.7pp）→ `not-claimable:direction-flips` |
| 生命周期 filter 臂**省 ~10% tokens** | **A** | 两批 −1955 / −1586，噪声 687 → claimable |
| 生命周期 filter 臂**损失 8.4pp 召回** | **A** | 两批 −3.4 / −13.3pp，噪声 0.066 → claimable |
| **进化三臂**（两批 12 轮 × 3 臂，每臂 66–72 次评估） | **A** | **两批**跨批次方向一致且超噪声：ungated +5.6/+2.8（噪声 2.8pp）、gated +5.6/+3.3（噪声 2.3pp）。**限定**：效应仅 4pp 量级、命中 3/72 极稀疏、且**未随技能累积增长** |
| 进化的**自身开销**（side_query / 总消耗） | **A** | 6.4–7.4%，两个进化臂一致；基线臂恒 0%（防串数据） |
| **增益不随技能累积增长**（定量） | **B** | 技能 12→19 期间命中率与技能数 **Spearman ρ = −0.213**；前 1/3 轮 2.1% → 后 1/3 轮 0.0%。**口径**：只能宣称「未观察到增长」，**不能**宣称「下降」（n=24、总命中 4 次） |
| Harness² 配置轴可配可回退（机制层） | **A** | 16 条回归；但**论文数值未复现**（检索工具不可用），不得引用论文数字 |
| 反思教训**可被召回**（3/3 命中、0 误召） | **A** | `reflection-recall-probe.json`，真实模型 |
| 反思教训**降低同类失败率** | **D** | 两批一致**零效应**（4 题×2 臂：1/4 vs 1/4；0/4 vs 0/4）→ 不可宣称 |
| 反思教训注入**增加 token 消耗** | **A** | 两批 +90k / +356k，噪声 222k → `claimable`（唯一可宣称的效果是成本） |

> **反思实验的历史两批已作废**：2026-10-29 发现 worker 子进程读 `os.environ["MODEL"]`
> 而脚本设的是 `EVOHARNESS_MODEL`，导致那两批（−66.7pp / −25pp）实际用的是 `.env`
> 里的另一个模型。修正后重跑两批均为**零效应**。结论不是"反思有害"，而是
> "**反思在最终标记产出率这个指标上无效果，且成本上升**"。

**纪律**：任何对外文档出现 D 级结论即为不合格；B/C 级必须带标注。

## 七、复现

```bash
python -m eval.run_benchmark run --dataset gaia --workers 4 --max-turns 60 --timeout 1200          # 主跑
python -m eval.run_benchmark run --dataset gaia --workers 4 --max-turns 60 --timeout 1200 --fold-threshold 0.30  # 消融
python -m eval.run_benchmark score --dataset gaia    # 判分（gaia + gaia-nofold tag）

# 生命周期三臂对照 + 跨批次噪声门（真实模型，两批独立）
python eval/memory_lifecycle_ab.py --repeats 2 --out eval/runs/memory-lifecycle-ab-v3.json
python eval/memory_lifecycle_ab.py --repeats 2 --out eval/runs/memory-lifecycle-ab-v3b.json
python eval/lifecycle_noise_gate.py eval/runs/memory-lifecycle-ab-v3.json eval/runs/memory-lifecycle-ab-v3b.json
```
