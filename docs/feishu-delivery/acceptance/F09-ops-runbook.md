# F09 运维手册（生产灰度值守，2026-11-08 起）

生产配置自 2026-11-08 起为 `whitelist_auto` + `auto_reply_enabled=1`（owner 显式放行，
见 `runs/F09/owner-release.json`）。本手册是验收矩阵"数据删除与撤权后续动作、故障告警和
每日值守方式"的执行说明。

## 1. 每日值守（已自动化）

- 每 30 分钟：cron 会话执行先决检查（健康/token 龄 headless 续期）→ 引擎轮 → 台账核对 →
  评测闭环。产物：`runs/SELF-ITERATE/history.jsonl`、`runs/F08/cycles.json`、
  `runs/F09/shadow-history.jsonl`、`window-precision.json`。
- 服务健康：`feishu-daily-agent/.venv/Scripts/python.exe main.py health`。
  `ok:false` 时 issues 字段给出原因（worker_heartbeat_stale=服务僵死→重启；
  daily_heartbeat_missing=日报漏跑→跑 run_manual_daily）。

## 2. 停发（急停）

任意时刻把 `.env` 改回 `FEISHU_DEPLOYMENT_MODE=single_approval` + `FEISHU_AUTO_REPLY_ENABLED=0`
并重启服务（杀 main.py 进程后用脱离式启动器重启）。策略层在下一次决策即停发；
已进入 outbox 的待发项会在预检中降级。

## 3. 回退与更正

- **回退经验版本**：向机器人发「停用 #<经验id>」→ head 切换/暂停，自动回复立停。
- **更正已发回复**：向机器人发「纠正 #<草稿id> 更正内容」→ 生成新候选 →「验证 #<id>」发布。
- **撤权后续**：机器人被移出群/群解散 → 下一次 `bootstrap_sources` 对账自动写
  `revoked_at`，outbox 预检取消待发（实测：真实卡片批准后自动 cancelled）。
- **数据删除**：会话删除后事件自然停止；`source_sync` 窗口与 `messages.raw_json`
  按 RAW_PAYLOAD_RETENTION_DAYS 过期清理。

## 4. 故障告警与升级路径

- 决策异常（如变体误发）：`reply_decisions` 表可审计每个 auto 发送的 reason_codes；
  严重事件（错误经验命中/敏感放行）→ 立即急停（见 §2）→ 回放 `reply_decisions` +
  `outbox` 定位 → 修复 + 回归 + 重新放行。
- 自动回复上限：owner 于 2026-11-09 明确放开每日条数（AUTO_REPLY_MAX_PER_CHAT_PER_DAY=10000，等效不限，见 runs/F09/owner-release-20261109-nocap.json）；自迭代引擎每轮仍只发 3 条探针，其余硬门不变。
- provider/模型故障：LLM 层降级为模板应答（no_experience 原因码可见），不影响发信安全门。

## 5. 灰度边界（本阶段承诺）

- 白名单：owner 私聊 + 测试群；类别：FAQ；每日每会话 ≤3 条。
- 未放行：群聊自动回复、多类别、更高日限——扩量需新一轮 owner 放行 + 对应统计。
