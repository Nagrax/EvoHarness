# EvoHarness 闭环体系 — 接管手册

> 面向：接手的另一个 session / agent / 人。
> 目标：**5 分钟内知道这套东西是什么、怎么跑、怎么判断状态、哪些坑不要踩**。
> 最后更新：2026-11-06（第 148 轮）

---

## 0. 一分钟速览

EvoHarness 是自进化 Agentic Harness（Python）。本仓库里还有一套**评测与迭代闭环**：

```
评测(collect) → 差异(finding) → 判读(triage) → 排期(backlog) → 实现 → 再评测 → 回归门禁
```

它不是"跑分脚本集合"，而是**一套能自己发现问题、自己收缩 backlog 的机制**。
闭环产物全部落盘在 `eval/runs/` 与 `eval/loop/BACKLOG.json`。

**当前状态（2026-11-06）**：
| 维度 | 值 |
| --- | --- |
| 测试 | 1178 tests / 1176 passed / 0 failed / 2 skipped（双编码一致） |
| backlog | open 0（34 项已闭合） |
| 闭环 | 可无人值守跑（`loop_autorun.py --once`，退出码 0/3/2） |
| 真实模型预算 | 已解除 300M 上限，但**仍逐条记账** |

---

## 1. 五个关键文件（先读这五个）

| 文件 | 作用 | 什么时候看 |
| --- | --- | --- |
| `eval/loop/CHECKPOINT-20261025.md` | **全部 148 轮的检查点**（约 4500 行） | 想知道"某件事为什么这么做" |
| `eval/loop/BACKLOG.json` | 待办台账（运行时状态，不是源码） | 想知道还有什么没做 |
| `eval/runs/` | 全部产物（门禁结果、评测报告、截图） | 想复算任何数字 |
| `D:/claudecode/.zcode/workflows/evoharness-optimize-audit/gate.py` | **回归门禁**（双编码、baseline 比对、digest） | 每次改产品代码后必跑 |
| `D:/claudecode/.zcode/workflows/evoharness-optimize-audit/HANDOFF.md` | 交接入口（含历史能力表） | 接手第一件事 |

---

## 2. 三条命令（照抄即可）

### ① 跑门禁（改任何产品代码后**必做**）

```powershell
cd D:\claudecode
python .zcode\workflows\evoharness-optimize-audit\gate.py run `
  --repo D:/claudecode/EvoHarness `
  --out EvoHarness\eval\runs\gate-$(Get-Date -Format yyyyMMddHHmm)\gate.json `
  --task MY-TASK `
  --baseline "D:\claudecode\.zcode\workflows\evoharness-optimize-audit\baseline.json"
```

判据：`ok: true` 且两个环境（default / utf8）的 `failed == 0`。
**不要只看 exit code**——历史上出现过 `ok: true` 但 `exit 1` 的矛盾（见 §5）。

### ② 跑一轮闭环（或交给定时器）

```powershell
cd D:\claudecode\EvoHarness
python eval\loop_autorun.py --once
```

退出码含义：
- `0` = 一切正常，无高危待办
- `3` = 有新的 high 级 finding（**需要有人看**）
- `2` = 某一步失败（先查 `eval/runs/loop-autorun-*.json`）

读结果：`eval/runs/loop-autorun-<ts>.json` 与 `eval/loop/BACKLOG.json`。

### ③ 查台账状态

```powershell
cd D:\claudecode\EvoHarness
python eval\loop.py next            # 下一个该做的项（空表示无可做）
python eval\docs_consistency.py     # 文档数字 vs 磁盘事实（应 issues: 0）
python eval\auditor_selfcheck.py    # 审计器自检（应 8/8）
```

---

## 3. 接手第一件事：**先证明工具在工作**

这套体系最大的教训是：**"没有输出"有两种解释——干净，或者坏了。**

所以接手时**不要先看结论，先验证检查器**：

```powershell
cd D:\claudecode\EvoHarness
python eval\auditor_selfcheck.py        # 8/8 通过？
python eval\loop_source_health.py       # 12 个信号源都可跑/干净？
python eval\tool_reachability.py        # broken: 0？
python eval\docs_consistency.py         # issues: 0？
```

四个都过，才谈得上"当前是绿的"。

---

## 4. 改代码的纪律（写死的，别商量）

1. **绝不删测试、绝不弱化断言来凑绿**。测试失败只能改产品代码。
2. **修复必须带回归**。防复发的测试比修复本身更重要。
3. **任何产品代码改动后跑全量门禁**，并核对两个环境的统计行。
4. **临时脚本放 `D:/claudecode/scratch/`**，工程目录不留残留。
5. **不要 `git checkout/reset` 整个工作区**（会丢未提交工作）。
   单个文件恢复可以，但要确认那个文件没有你要保留的改动。
6. **提交前跑 `python eval/git_preflight.py`**（敏感文件/凭据/超大文件）。
7. **提交后立即 `git push` 并核对 `HEAD == origin/main`**。
   *（曾漏过一次 push，远端停了一个月）*

---

## 5. 已知坑（都是踩过的，别再踩）

### 5.1 检查器会静默失效

| 坑 | 症状 | 现状 |
| --- | --- | --- |
| gate 的 `baseline.nodeIds` 为空 | 跑 840 个用例也判绿 | **已修**，现 baseline 含 1083 nodeIds；`eval/update_gate_baseline.py` 有防护 |
| 信号源从没产出 finding | "干净"与"坏了"外观相同 | **已修**，`loop_source_health.py` 双判据（脚本可跑 + 产物新鲜度） |
| `silent_triage.py` 遇 `rule` 为 list 直接崩 | 整个 triage 工具不可用 | **已修**（它没人调用，所以崩了很久没人知道） |
| 工具 docstring 声称"被 loop 使用" | 实际调用早被移走 | **已修**，`silent_triage.py` 已如实标注为手动工具 |

### 5.2 产物被覆盖

`eval/runs/` **不在 git 里**（被 gitignore），所以：
- **重要结论不要只放在 `eval/runs/`**——写进 `CHECKPOINT-*.md`
- 生成器默认**不应覆盖**已有产物（`assess_long_functions.py` 已改为需 `--force`）

### 5.3 字符串/格式的隐式约定

一天内踩了 9 次同类问题（子串当模式、点号 vs 路径、反斜杠 vs 正斜杠、
`rgba(...,.55)` 正则、`nodeIds` 格式……）。它们**永不抛异常，只让判断静默失效**。

**所以：任何"检查类"代码，都要问两个问题**：
1. 它该看到的东西没看到时，会报错吗？
2. 我怎么**证明**它现在真的在看？

### 5.4 门禁的 digest 与运行时台账

`eval/loop/BACKLOG.json` 是**运行时状态**，被算进 gate 的 `sourceDigest`。
测试跑一轮会追加一条 event → digest 变 → gate 报 `ok: false`（**即使测试全过**）。

- 已加守护：测试环境**不写真实台账**（`loop.py` / `mark_triaged.py` 的 `_write_allowed`）
- **判断"有没有写入"用内容 hash，不要用 `git status`**（会被大量既有改动淹没）

### 5.5 别用 flaky 题做"单向改善"实验

功效分析（`eval/power_analysis.py`）证明：**基线越确定，统计功效越高**。
flaky 题的双向噪声在 McNemar 里互相抵消，需要 600 观测/臂；stable-fail 只需 80。

---

## 6. 当前未完成项（诚实清单）

| 项 | 状态 | 说明 |
| --- | --- | --- |
| W19 验收项 ② | ⛔ blocked | 受 W17（真实人标）阻塞，**不能自行解决**，不得标 release-ready |
| W15 四策略对照 | ⏸ 暂停 | 机制已验证（S2 折叠发生过 1 次），但质量差异不可宣称、S4 未跑 |
| 反思收益 | ⏸ 止损 | 5 批 / 6.6M tokens，覆盖"太难/太易/预算不足/陷阱太浅"四种失效，结论 **D 级不可宣称** |
| W14 进化收益 | **定论 B 级** | 有正收益 + 方向稳定 + 超噪声，但**统计不显著**；扩样需 15h+，**已达该题池上限** |
| 脚本级测试覆盖 | 部分 | 库级 0 缺口；**30 个脚本零测试引用**（多为一次性工具，已登记） |

**这些都不要为了"看起来完成"而改成 done。**

---

## 7. 证据等级约定（写进 RESULTS 的口径）

| 等级 | 含义 |
| --- | --- |
| **A** | 有复跑命令 + sha256 + 双编码一致，可被独立复算 |
| **B** | 方向稳定、跨批次一致，但无置信区间或统计不显著 |
| **C** | 有数据但存在已知缺陷（如产物丢失、样本不足） |
| **D** | **不可宣称**（实验未测到作用点） |

`eval/final_audit.py` + `eval/evidence_index.py` 会检查：
- A/B/C 级主张**必须有证据文件**
- 只有文档没有代码的主张 → 强制降为 C

---

## 8. 关键数字的出处（别凭记忆）

| 数字 | 出处 |
| --- | --- |
| GAIA 165 题 pass@1 49.1% | `eval/RESULTS.md` + `eval/evidence_index.py --check` |
| HLE n=75 10.7% | 同上 |
| 进化 +4.2~4.5pp（B 级） | `eval/runs/evolution/` + `eval/paired_mcnemar.py` |
| 进化开销 6.4~7.4% | 同上 |
| 前端 21 控件 20 live / 0 dead | `eval/runs/ui/ui-after-redesign.json` |
| 后端 12/12 路由覆盖 | `eval/runs/api-coverage.json` |

**任何数字写进对外材料前，先跑 `eval/evidence_index.py --check` 确认没有漂移。**

---

## 9. 如果你只能做一件事

跑这四条，然后把结果记进 `CHECKPOINT`：

```powershell
cd D:\claudecode\EvoHarness
python eval\auditor_selfcheck.py
python eval\loop_source_health.py
python eval\docs_consistency.py
python eval\loop_autorun.py --once
```

**先证明工具在工作，再看它报告什么。** 顺序不能反。

### `loop.py next` 的正常输出

```json
{"ok": false, "reason": "empty"}
```

`ok: false` **不是出错** —— `reason: "empty"` 表示当前没有可做的项（台账已清空）。
这是设计如此，不要据此判断"工具坏了"。

### 门禁耗时与运行纪律（重要）

- 全量门禁约 **20–25 分钟**（1178 个测试 × 双编码 default/utf8）
- **运行期间不要并行跑其他命令**：任何写文件行为都会改变 `sourceDigest`，
  导致门禁报 `sourceDigest 在门禁期间变化`（即使测试全过）
- 也不要边跑边改代码 —— 改完再跑
- 若看到 `default tests= 0` 而 utf8 正常：那是**采集偶发失败**，不是测试真的没跑；
  重跑一次通常恢复（历史上出现过 3 次）

---

## 10. 2026-11-06 收尾说明（交接时的真实状态）

本轮（第 148 轮）做的是**交接文档**，不是继续跑实验。如实说明：

| 项 | 状态 |
| --- | --- |
| 接管文档 | ✅ 本文件 |
| 台账写盘守护（测试不污染 BACKLOG） | ✅ 已加 `_write_allowed`（`loop.py` / `mark_triaged.py`） |
| 全量门禁 | ⚠️ **未取得一次干净的绿**。最后两次分别因 ① 我并行跑命令改了 digest ② `default` 采集偶发 0 测试 而判 false |
| 已知的真失败 | `utf8` 曾报 1 个失败（未定位到具体用例） |

**接手者第一件事：在没有任何并行操作的情况下，单独跑一次全量门禁**，
确认 `ok: true` 且两环境 `failed == 0`。在那之前，"门禁全绿"不能作为结论使用。
