# EvoHarness 自动迭代闭环

这是把「评测 → 差异 → 迭代设计 → 实现 → 复测 → 回归门禁」固化成可反复运行、无需用户表态的闭环。
入口工作流：`.zcode/workflows/evoharness-auto-iterate.dwf.ts`。

## 1. 三层评测（客观信号，不依赖人工判断）

| 层 | 脚具 | 覆盖 | 产物 |
| --- | --- | --- | --- |
| 后端契约 | `eval/backend_contract.py` | 11 个 API 端点，每个配正向 + 负向（含路径穿越、越界文件、无 key、不泄漏 key） | `eval/runs/backend-contract.json` |
| 前端逐按钮 | `D:/claudecode/scratch/btest/evo_ui_buttons.mjs` | 真实 Chromium 点击 17 个控件；四通道判定（DOM 变更计数 / 状态哈希 / 网络请求 / 遮挡命中） | `eval/runs/ui/ui-current.json` + 截图 |
| 模块扫描 | `eval/module_scan.py` | 9 类可机器验证的信号：静默吞错、async 内阻塞调用、超长函数、魔法阈值、无截断注入、无测试模块、裸 print 等 | `eval/runs/module-scan.json` |
| 回归门禁 | `.zcode/workflows/evoharness-optimize-audit/gate.py` | 双编码全量 pytest + 节点集/skip 差异 + sourceDigest | 每轮 `eval/runs/<round>/gate.json` |

## 2. 闭环状态机

`eval/loop.py` 是唯一状态源（`eval/loop/BACKLOG.json`）：

```
collect → backlog(items) → next(按 severity×出现次数) → start(租约) → 修复+回归 → verify(复测) → done
                                                                        ↘ 外部条件缺失 → blocked
已 done 的项复测再次出现 → 自动 reopened（回归检测）
```

关键纪律（写进代码，不靠自觉）：
- `done` 只认**复测事实**：同一 finding 下一轮不再出现，或该规则对该行不再报警；
- 已闭合项重现会自动重开，防止"改坏了还以为修好了"；
- 每轮都跑全量门禁，闭环不能绕过回归；
- 静态信号必须**判读**：多数静默吞错是合理防御（关闭管道、kill 已退出进程），
  只有满足以下任一才是真缺陷——(a) 吞掉用户可见状态，(b) 留下不一致状态，(c) 本应上报/重试却没有。

## 3. 当前实测结果（2026-10-25）

| 轮次 | findings | 后端失败 | 前端失败 | 修复 | 门禁 |
| --- | ---: | ---: | ---: | ---: | --- |
| r1 | 63 | 0 | 0 | 2 | 587→589 tests, 0 failed |
| r2 | 63 | 0 | 0 | 1 | 见 `eval/runs/r2/gate.json` |

已修复的真实缺陷（都有能区分修复前后的回归）：
1. **Web SSE 构造即崩**：`openai.AsyncOpenAI(api_key=None)` 在本地免 key 端点直接抛
   `OpenAIError`，用户看到 Python 异常而不是"未配置 key"。修复后 `/api/chat` 正确返回
   `error` + `end` 事件，且不泄漏 key（`backend_contract.py` 两条用例守护）。
2. **会话保存失败静默**：`_auto_save` 原 `except Exception: pass`，导致"历史丢了"与
   "从未保存"无法区分。修复后记 `last_save_error`、落盘诊断事件、并在 SSE `done` 帧带
   `saveError`，前端以 warn 提示展示。回归 `tests/test_auto_save_visibility.py` 4 条。

## 4. 怎么跑

```powershell
# 单轮手动
cd D:/claudecode/EvoHarness
$env:EVO_EVAL_BASE='http://127.0.0.1:8899'
python eval/loop.py collect --round r3
python eval/loop.py next
python eval/loop.py start L0001 --owner me
python eval/loop.py done L0001 --note "复测证据"
python eval/loop.py report

# 评测服务（隔离端口，别占用户正在用的）
$env:PORT='8899'; $env:EVOHARNESS_HOST='127.0.0.1'; $env:EVO_RELOAD='0'
python -m uvicorn server.app:app --host 127.0.0.1 --port 8899
```

工作流方式：选择 `evoharness-auto-iterate.dwf.ts`，参数 `rounds`（轮数，0=不设上限）、
`maxItemsPerRound`、`liveBudgetTokens`。

## 5. 边界（不能吹的部分）

- 前端逐按钮只覆盖**当前 17 个已知控件**；新增控件需同步登记到 `CONTROLS`，否则看不见。
- 后端契约是**契约级**，不是全功能等价测试；复杂业务流仍靠 `tests/` 的全量回归。
- 模块扫描是启发式：它找出"值得看"的地方，判读仍需要人工规则（已写进工作流判据）。
- 真实模型评测（GAIA/HLE/LoCoMo 类）不在本闭环内，走 `evoharness-optimize-audit` 的
  W13/W14/W15/W18 与 `live_budget_guard.py` 记账。
