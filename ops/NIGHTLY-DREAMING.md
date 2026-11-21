# Dreaming 夜间调度部署（EVO-05 / DE-17 剩余）

Dreaming 单轮编排（`evoharness/knowledge/reconcile.py::run_dreaming_cycle`）的时间触发
部署路径。**候选不自动采用**：夜间产物是报告 + 候选变更集，采用必须由责任人走
`reconcile.adopt_candidate` 显式入口（新值/证据/责任人/CAS 齐备才生效）。

## 机制（复用既有面，零新增常驻进程/新表）

| 层 | 机制 | 职责 |
|---|---|---|
| 时间触发 | `ops/evoharness-dreaming.timer` → `.service`（oneshot） | 每日 02:30（±15min 抖动，Persistent 补跑） |
| 单轮编排 | `python -m ops.dreaming_nightly` | 逐空间：枚举 manifest → 确定性裁决 → 收口报告 |
| 状态与单写者 | `knowledge_reconciliation_jobs`（既有表） | 同空间单 pending；冲突夜跑自动跳过留痕 |
| 常驻执行 | `ops/supervisor.py` + `evoharness-worker.service`（既有） | worker 常驻，与夜跑互不替代 |
| 事件驱动 | `evoharness/work/wakeups.py`（DE-21，既有） | 依赖恢复/回复/CI/期限事件，与时间触发互补 |

## 一键部署

```bash
# 1) 两个 unit 拷入 systemd（服务与定时器已互指一致）
cp ops/evoharness-dreaming.service ops/evoharness-dreaming.timer /etc/systemd/system/
# 2) runtime.env 提供 EVOHARNESS_DATABASE_URL（现有 EnvironmentFile，无需新增）
systemctl daemon-reload
systemctl enable --now evoharness-dreaming.timer
systemctl list-timers evoharness-dreaming.timer   # 确认下次触发时间
```

多空间：在 `evoharness-dreaming.service` 的 `ExecStart` 追加 `--space <空间>`（可重复）。
报告目录 `--report-dir`（默认 `eval/runs/dreaming/`，报告文件 `<date>-<space>.json`）。

离线冒烟（不装 systemd）：

```bash
python -X utf8 -m ops.dreaming_nightly --db sqlite:///data/agent.db \
    --space support --report-dir eval/runs/dreaming --date 2026-11-21
```

## 夜跑做什么 / 不做什么

- 做：对空间内 active/conflicting 主张逐条重读来源快照（`SnapshotRecheckJudge`，
  确定性代码，不调模型）：
  - 快照漂移（重读成功但内容不一致）→ `contradicts` 候选（`propose_supersede` + 漂移 refs 证据）；
  - 来源一致 → `supports`；声明级（无快照）→ `absent`（没有提及不等于推翻）；
  - 来源读取失败 → `error`（不伪造矛盾：计入 failed、partial 可见、可重试）。
- 不做：**不写新值、不自动采用**。少处理一份不报全完成（partial 可见）；
  CAS 版本（报告内 `revision` = manifest 的 max(source_revision)）保证旧报告的
  候选在版本抬升后无法被误采用（`stale_reconcile_revision`）。

## 夜跑之后的显式采用（责任人操作）

```python
from evoharness.knowledge import reconcile

# 报告 candidates 里的 item + 报告自带 revision；新值/证据/责任人缺一拒绝
reconcile.adopt_candidate(
    session, job_id, item="<claim_id>", new_value="<owning service 核实的新值>",
    actor="<责任人>", expected_revision=<报告 revision>,
    source_refs=["<证据 ref>"])
# 旧主张 superseded、历史与来源保留；重复采用幂等
```

## 与配对实验的关系

`eval/paired_dreaming_check.py`（离线、确定性）用同一入口跑两臂对比
（有/无夜跑）：双臂产物哈希 + 成本（真实预算台账 `category=dreaming`）落
`eval/runs/paired_dreaming/`。回归：`tests/test_dreaming_nightly.py`、
`tests/test_paired_dreaming_check.py`。
