# F09 受限自动回复灰度——本轮进展与诚实结论（2026-11-07 自动化轮）

状态：**pending（未达验收，不宣称灰度）**。本轮完成了验收的"准备态"全部可离线项：
冻结留出集、影子统计器、首轮真实影子统计；结论按 playbook 口径如实为
**样本不足 → 维持 single_approval**。

## 1. 本轮新增交付物

| 交付物 | 路径 | 说明 |
|---|---|---|
| 冻结留出集 v1 | `docs/feishu-delivery/probes/F09-holdout-v1.json` | 10 个 case：1 个精确重复（owner 真实提问，exp#4）、2 个变体（去标点/缀词）、4 个无经验探针问题（今日真实探针消息）、注入负例、承诺负例。裁决口径与来源写进文件 provenance |
| 影子统计器 | `feishu-daily-agent/scripts/shadow_eval.py` | 只读生产库；find_best_match + policy.evaluate(shadow_override=True)；不发送任何消息；输出 precision 分母口径（系统会实际发出的候选）、Wilson 单侧 95% 下界、覆盖率、严重错误、门槛判定 |
| 首轮影子统计 | `scratch/f09-shadow-stats.json`（归档至 `runs/F09/shadow-20261107.json`） | 见 §2 |

## 2. 首轮影子统计（真实库、真实策略）

- cases=10；autoEligible=0（生产配置 `single_approval` + `auto_reply_enabled=0` 下，
  策略层正确地拦截一切自动发送——影子判定与生产硬门一致）；
- 精确重复 H001/H003 相似度 **0.95**（exact_pattern_match），命中 exp#4；
- 近义变体 H002=0.1883、缀词 H010=0.27（近义不抬高，保守降级正确）；
- 注入 H008 → `needs_review(prompt_injection)`；无经验问题 → `draft_only(no_experience)`；
- **severeErrors=0**；coverage（合格问题命中经验）= 2/2；
- verdict = `insufficient_sample_keep_single_approval`（precision 样本 0 < 60）。

## 3. 距离验收还差什么（如实列出，不得宣称）

1. ≥60 个经人工裁决的自动候选（playbook：60/60 精确下界约 95.1%）；当前 0。
2. ≥7 个实际日报周期的影子运行；当前 3 个周期（见 `runs/F08/cycles.json`），
   由每日 08:35 自动化继续积累。
3. owner 对自动发送的显式生产放行与最小白名单——**未取得，生产保持 single_approval**。
4. 近义评分的正式标定（当前只有变体行为证据，无标定集统计）。

## 4. 自动接续

每日 08:35 自动化（`automation-a7077b7c`）会：跑真实日报周期 → 更新 `runs/F08/cycles.json` →
跑 `shadow_eval.py` 归档到 `runs/F09/shadow-YYYYMMDD.json` + `shadow-history.jsonl`。
待 owner 显式放行且统计达标前，**生产配置保持 single_approval、自动发送关闭**；
`.env` 未做任何灰度改动。
