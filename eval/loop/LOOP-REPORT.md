# EvoHarness 迭代闭环报告

生成时间：2026-11-09T09:09:46.400Z
累计轮次：35
待办总数：37
状态分布：{"done": 34, "pending": 3}

## 最近三轮

| 轮次 | 时间 | findings | 后端 | 前端 | 模块 | sourceDigest |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| auto-r4-0036 | 2026-11-08T16:36:59.528Z | 55 | 0 | 2 | 47 | 8d991cf78ee8e364 |
| auto-r14-0710 | 2026-11-08T23:11:37.514Z | 55 | 0 | 2 | 47 | 32233c2dbb235251 |
| auto-r16-0836 | 2026-11-09T00:31:02.194Z | 55 | 0 | 2 | 47 | ac018594b6063cd8 |

## 未闭合项（按优先级）

- **L0022** [high] 配置噪声 50.0pp：小于该值的差异不可宣称
  - 状态：pending；出现 28 次；首见 r8
  - 说明：heavy-wrapped 同规模 pass@1 极差 50pp（[0,0,0,50]），收敛需付费基准重跑（n>=30 或多轮均值）定位离群 run；本会话禁止新增付费调用。替代路径已就位：eval/roi.py 的 not-claimable:undersampled 判据 + 报告需附同配置重复次数与极差。复验条件：获得付费基准预算后重跑 heavy-wrapped n>=5，若极差收敛到个位数 pp 则重开并复核报告口径
- **L0021** [medium] 配置变体负收益 tool_policy/tight
  - 状态：pending；出现 30 次；首见 r5
  - 说明：信息型 finding：证据已在 detail，非待修工单；复测由对应评测器负责
- **L9025** [medium] 生命周期对照 4/6 指标可宣称，其余须按 not-claimable 口径表述
  - 状态：pending；出现 27 次；首见 r20261028T072239Z
  - 说明：信息型 finding：证据已在 detail，非待修工单；复测由对应评测器负责

## 已闭合项（最近 20）

- **L0052** 模块信号 silent-except @ eval/loop.py — 已逐处判读（2 处）：git/工作树探测失败返回空值属预期（非 git 环境）
- **L0054** 模块信号 silent-except @ eval/run_benchmark.py — 已逐处判读（2 处）：MCP 连接关闭属清理路径；JSONL 坏行已修为计数+提示（否则评测样本静默变少）
- **L0056** 模块信号 silent-except @ server/app.py — 已逐处判读（3 处）：淘汰会话与换凭据时的 abort 失败已修为留痕（对象移除但任务可能仍在跑）；修复后缓存刷新失败已回传给调用方（cacheRefreshError + hint，避免误报已生效）
- **L0059** 模块信号 silent-except-return-none @ agents/agent.py — 已逐处判读（8 处）：清理路径（coro.close / task.result 已修为留痕）、会话保存失败已记入 last_save_error、其余为降级但不影响主流程
- **L0062** 模块信号 silent-except-return-none @ agents/session.py — 已逐处判读（1 处）：_fsync_dir 的 os.open 失败直接 return —— 目录不存在或不可打开时放弃 fsync，属 Windows 下的预期降级
- **L0063** 模块信号 silent-except-return-none @ agents/tools.py — 已逐处判读（7 处）：tmp 清理失败后接 raise、kill 已退出进程、grep 非核心路径；mtime 登记失败两处已修为留痕
- **L0023** 模块信号 silent-except @ eval/arch_run.py — 已逐处判读（1 处）：variance 文件解析失败 → 跨批噪声退化为 0。方向是保守的（噪声越小越难宣称），属安全降级
- **L0024** 模块信号 silent-except @ eval/memory_lifecycle_ab.py — 已逐处判读（1 处）：client.close() 清理失败不影响已产出的结果
- **L9026** 模块信号 silent-except @ eval/reflection_recall_probe.py — 已逐处判读（1 处）：client.close() 清理失败不影响已产出的结果
- **L0027** 失败稳定性：0 稳定 / 1 偶发 / 0 未复现——只有 stable 类可进入修复或反思流程 — 信息型 finding：证据已在 detail，非待修工单；复测由对应评测器负责
- **L9028** GAIA 稳定失败 4 题（共失败 84 题，其中未检验/偶发 161 题已过滤） —
- **L0029** GAIA 失败 165 题均未通过稳定性检验——仅记录，不建工单 — 信息型 finding：证据已在 detail，非待修工单；复测由对应评测器负责
- **L0030** HLE 失败 75 题均未通过稳定性检验——仅记录，不建工单 — 信息型 finding：证据已在 detail，非待修工单；复测由对应评测器负责
- **L9029** 模块可优化项 long-function（18 处 / 13 文件） — 已逐处评估（eval/runs/long-function-assessment.json）：6 处该拆 / 8 处收益低 / 4 处拆了反而有害；已为 skills_overview 补 5 条契约测试
- **L9030** 后端契约失败 GET /api/skills / backend:no_secret_leak_skills — 信息型 finding：证据已在 detail，非待修工单；复测由对应评测器负责
- **L9031** 前端控件失效 ui:starter:起始示例-浏览目录 — 信息型 finding：证据已在 detail，非待修工单；复测由对应评测器负责
- **L9032** 前端控件失效 ui:starter:起始示例-记忆折叠 — 信息型 finding：证据已在 detail，非待修工单；复测由对应评测器负责
- **L9033** 模块信号 silent-except @ agents/extraction_window.py — 已修复：历史位置本轮扫描不再报警（复测证据）
- **L9034** 模块信号 silent-except @ eval/loop_context.py — 已修复：历史位置本轮扫描不再报警（复测证据）
- **L9035** 模块信号 silent-except @ eval/silent_triage.py — 已修复：历史位置本轮扫描不再报警（复测证据）
