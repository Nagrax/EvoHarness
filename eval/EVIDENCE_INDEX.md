# 证据索引（EvoHarness）

本文件由 `eval/evidence_index.py` 生成，**不要手改**。
评审者可用它逐条核对：主张 → 证据文件 → sha256 → 复跑方式。

- 主张总数：**13**（A 9 / B 1 / C 2 / D 1）
- 有独立复跑命令：**10/13**
- 等级定义见 [`eval/RESULTS.md` §六](RESULTS.md)

| # | 主张 | 等级 | 证据 | sha256(前 12) |
| --- | --- | --- | --- | --- |
| 1 | GAIA 165 题 pass@1 49.1%（CI 41.2–57.0） | **A** | `eval/runs/gaia/summary.json` | `84f518bf2899` |
| 2 | HLE n=75 pass@1 10.7%（CI 4.0–17.3） | **A** | `eval/runs/hle/summary.json` | `c9d34d709c7b` |
| 3 | 折叠消融阈值 0.30 降 11.5pp（McNemar p=0.011） | **C** | `eval/RESULTS.md` | `03d4426ce61c` |
| 4 | LoCoMo 持平 + token −62% | **C** | `eval/RESULTS.md` | `03d4426ce61c` |
| 5 | 软标注消除污染（−2 事件，两批极差 0） | **A** | `eval/runs/memory-lifecycle-ab-v3.json`<br>`eval/runs/memory-lifecycle-ab-v3b.json`<br>`eval/runs/lifecycle-noise-gate.json` | `533a655bce3b`<br>`f2bd9e77653b`<br>`3acb93fb8c47` |
| 6 | 硬过滤省 ~10% tokens 但损失 8.4pp 召回 | **A** | `eval/runs/lifecycle-noise-gate.json` | `3acb93fb8c47` |
| 7 | 反思教训可召回（3/3 命中，0 误召） | **A** | `eval/runs/reflection-recall-probe.json` | `04de71857ca2` |
| 8 | 反思降低同类失败率 | **D** | **（无：不可宣称）** | — |
| 9 | 教训注入增加 token 消耗（两批 +90k/+356k） | **A** | `eval/runs/reflection-noise-gate.json` | `3de15b9e3d93` |
| 10 | 进化带来小的正收益（两批跨批次方向一致且超噪声，+4.2~4.5pp），但增益未随技能累积增长（ρ=−0.21） | **B** | `eval/runs/evolution/checkpoint-no_evolution-r6.json`<br>`eval/runs/evolution/checkpoint-ws-w14d-no_evolution-r6.json` | `269b347d8097`<br>`ce049d648408` |
| 11 | 进化自身开销 6.4–7.4%（基线臂恒 0%） | **A** | `eval/runs/evolution/batch-ws-w14d.json` | `59ed95c1d875` |
| 12 | Harness² 配置轴可配可回退（机制层） | **A** | `tests/test_harness_axes_configurable.py` | `9bab6df6ac1b` |
| 13 | 80% GAIA 失败稳定复现（10 题×2 遍） | **A** | `eval/runs/failure-stability-2.json` | `40b8245cab85` |

## 复跑方式

- **gaia-pass1**：`python -m eval.run_benchmark run --dataset gaia --workers 4 --max-turns 60 --timeout 1200`
- **hle-pass1**：`python -m eval.run_benchmark run --dataset hle`
- **lifecycle-soft-decontam**：`python eval/memory_lifecycle_ab.py --repeats 2`
- **lifecycle-filter-cost**：`python eval/lifecycle_noise_gate.py <batch1.json> <batch2.json>`
- **reflection-recall**：`python eval/reflection_recall_probe.py`
- **reflection-cost**：`python eval/lifecycle_noise_gate.py eval/runs/reflection-effect-highconf-a.json eval/runs/reflection-effect-highconf-b.json`
- **evolution-benefit**：`python eval/batch_results.py show --since 2026-10-29T01:30 && python eval/batch_results.py show --tag ws-w14d`
- **evolution-overhead**：`python eval/batch_results.py show --tag ws-w14d`
- **harness2-mechanism**：`python -m pytest tests/test_harness_axes_configurable.py -q`
- **failure-stability**：`python eval/failure_stability.py --limit 10 --repeats 2 --source full`

以下主张**没有独立复跑命令**（属文档/历史记录类，或需要外部条件）：

- `fold-ablation`
- `locomo`
- `reflection-benefit`
