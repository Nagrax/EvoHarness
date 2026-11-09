# 飞书读写探针与脱敏记录

本目录是 F01 的交接模板。**读探针与发信探针必须分开执行**，读权限不能推导写权限。

## 身份与目标（执行前填写，凭据只放环境变量）

| 项 | 值 |
| --- | --- |
| tenant |  |
| owner open_id |  |
| 机器人 open_id |  |
| 读身份 | bot / user |
| 发信身份 | bot / user |
| 试点会话 | p2p / group / thread，chat_id 与 thread_id |
| 应用 scope |  |
| 探针时间 |  |

## 来源能力矩阵

对每个来源逐项记录，不得用“机器人能看到群”代替个人单聊发现能力。

| 来源 | chat_id | 发现方式 | 事件覆盖 | 历史覆盖 | 读 token | 写 token | 原话题回复 | 编辑/撤回 | 限制 | 样本时间 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 机器人私聊 |  | bot_list |  |  |  |  |  |  |  |  |
| 群内 @ 机器人 |  | bot_list |  |  |  |  |  |  |  |  |
| 群内无 @ |  | bot_list |  |  |  |  |  |  |  |  |
| 同群话题 |  | message.thread_id |  |  |  |  |  |  |  |  |
| 用户授权自然人单聊 |  | OAuth/手动登记 |  |  |  |  |  |  |  |  |
| 撤权会话 |  |  |  |  |  |  |  |  |  |  |

## 执行命令

```powershell
$env:FEISHU_APP_ID = '<app id>'
$env:FEISHU_APP_SECRET = '<app secret>'
$env:FEISHU_OWNER_OPEN_ID = '<owner open_id>'
$env:FEISHU_BOT_OPEN_ID = '<bot open_id>'
# 只读探针
python feishu-daily-agent\scripts\probe_feishu.py --read --json out\f01-read.json
# 发信探针（单独执行，目标必须显式）
python feishu-daily-agent\scripts\probe_feishu.py --write --target <chat_id或open_id> --message "EvoHarness F01 探针"
```

## 幂等与对账契约（已按 SDK 能力核对）

- `im.v1.message.create` 与 `im.v1.message.reply` 的请求体均支持 `uuid`；发送 outbox 用稳定 `intent_key` 派生 `uuid`，重试复用同一键。
- 同一 `uuid` 的幂等窗口按 1 小时设计；超过窗口的 `sending` 租约恢复进入 `delivery_unknown`，不盲重发。
- 超时/连接中断：统一停在 `delivery_unknown`；只有拿到真实 `message_id` 或人工核对远端回执才转 `sent`。
- 429 与 5xx：指数退避 + 抖动，最多 5 次；权限/参数类 4xx 直接 `failed`。
- 卡片动作只携带一次性 `action_id`；操作者从受信事件读取并与 token.owner 比对。

## 脱敏纪律

- 不记录 app secret、tenant token、完整消息正文；只记录 chat_id 的尾 6 位、消息哈希、长度与结果码。
- 原始 payload 按 `RAW_PAYLOAD_RETENTION_DAYS` 短留；日报与评测只使用脱敏摘要。
- 真实语料、数据库、探针日志不进入公共仓库。
