# EvoHarness 迭代闭环报告

生成时间：2026-10-25T16:05:47.345Z
累计轮次：4
待办总数：20
状态分布：{"pending": 20}

## 最近三轮

| 轮次 | 时间 | findings | 后端 | 前端 | 模块 | sourceDigest |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| r2 | 2026-10-25T15:16:55.132Z | 63 | 0 | 0 | 63 | f6b364a2f945c8ab |
| r3 | 2026-10-25T15:36:14.179Z | 60 | 0 | 0 | 60 | 31ad7a728d82822f |
| r4 | 2026-10-25T16:04:56.222Z | 57 | 0 | 0 | 57 | 15835ba4bd190858 |

## 未闭合项（按优先级）

- **L0001** [high] 模块信号 silent-except @ agents/agent.py
  - 状态：pending；出现 29 次；首见 r1
  - 说明：合并迁移：已修复：_auto_save 不再静默吞错，记 last_save_error + 诊断事件 + SSE done 帧带 saveError；回归 tests/test_auto_save_visibility.py（4 条）；判定合理防御（cleanup:kill），不改代码；已修复：_stage_write_segment 落盘失败记 last_save_error；回归 test_stage_segment_save_failure_is_visible；已修复：折叠记忆落盘失败记 last_save_error；回归 test_folded_memory_save_failure_is_visible；判定合理防御（cleanup:kill），不改代码；判定合理防御（cleanup:close,kill），不改代码
- **L0011** [high] 模块信号 silent-except @ agents/mcp_client.py
  - 状态：pending；出现 16 次；首见 r1
  - 说明：合并迁移：判定合理防御（cleanup:close），不改代码；判定合理防御（cleanup:close），不改代码；判定合理防御（cleanup:kill），不改代码；判定合理防御（cleanup:close），不改代码
- **L0016** [high] 模块信号 silent-except @ agents/memory.py
  - 状态：pending；出现 13 次；首见 r1
  - 说明：合并迁移：已修复：诊断日志写失败退到 stderr 单次告警，不再静默；memory/encoding 测试 22 passed
- **L0020** [high] 模块信号 silent-except @ agents/online_skill_eval.py
  - 状态：pending；出现 16 次；首见 r1
  - 说明：合并迁移：判定合理防御（cleanup:kill），不改代码；判定合理防御（cleanup:kill），不改代码；判定合理防御（cleanup:kill），不改代码
- **L0025** [high] 模块信号 silent-except @ agents/online_skill_evolution.py
  - 状态：pending；出现 4 次；首见 r1
- **L0026** [high] 模块信号 silent-except @ agents/prompt.py
  - 状态：pending；出现 7 次；首见 r1
- **L0028** [high] 模块信号 silent-except @ agents/session.py
  - 状态：pending；出现 12 次；首见 r1
  - 说明：合并迁移：判定合理防御（cleanup:close,unlink），不改代码；判定合理防御（cleanup:close,unlink），不改代码；已修复：list_sessions 把坏文件记入 last_list_errors；回归 2 条
- **L0032** [high] 模块信号 silent-except @ agents/skill_evolution.py
  - 状态：pending；出现 7 次；首见 r1
- **L0034** [high] 模块信号 silent-except @ agents/skill_repair.py
  - 状态：pending；出现 4 次；首见 r1
  - 说明：判读为合理防御/降级型，不改代码；判据见 triageClosed（1 处）
- **L0035** [high] 模块信号 silent-except @ agents/skill_tiers.py
  - 状态：pending；出现 10 次；首见 r1
  - 说明：合并迁移：判定合理防御（cleanup:kill），不改代码
- **L0038** [high] 模块信号 silent-except @ agents/subagent.py
  - 状态：pending；出现 4 次；首见 r1
- **L0039** [high] 模块信号 silent-except @ agents/tools.py
  - 状态：pending；出现 25 次；首见 r1
  - 说明：部分修复：MEMORY.md 索引重建失败上报 + 单条解析失败保留占位；回归 tests/test_memory_index_visibility.py（4 条）。剩余位置仍待按判读逐个处理
- **L0047** [high] 模块信号 silent-except @ eval/evolution_experiment.py
  - 状态：pending；出现 13 次；首见 r1
  - 说明：合并迁移：判定合理防御（cleanup:close），不改代码
- **L0051** [high] 模块信号 silent-except @ eval/experience_eval/hallucination_probe.py
  - 状态：pending；出现 4 次；首见 r1
- **L0052** [high] 模块信号 silent-except @ eval/loop.py
  - 状态：pending；出现 7 次；首见 r1
- **L0054** [high] 模块信号 silent-except @ eval/run_benchmark.py
  - 状态：pending；出现 7 次；首见 r1
  - 说明：合并迁移：判定合理防御（cleanup:close），不改代码
- **L0056** [high] 模块信号 silent-except @ server/app.py
  - 状态：pending；出现 10 次；首见 r1
  - 说明：合并迁移：判定合理防御（cleanup:kill），不改代码
- **L0059** [high] 模块信号 silent-except-return-none @ agents/agent.py
  - 状态：pending；出现 10 次；首见 r1
  - 说明：合并迁移：判定合理防御（cleanup:kill），不改代码
- **L0062** [high] 模块信号 silent-except-return-none @ agents/session.py
  - 状态：pending；出现 4 次；首见 r1
- **L0063** [high] 模块信号 silent-except-return-none @ agents/tools.py
  - 状态：pending；出现 4 次；首见 r1
  - 说明：判读为合理防御/降级型，不改代码；判据见 triageClosed（2 处）

## 已闭合项（最近 20）

无。
