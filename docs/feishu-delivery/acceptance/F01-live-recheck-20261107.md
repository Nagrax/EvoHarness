# F01 实机复验报告（2026-11-07 晚间自动化轮）

状态：**blocked 项全部解除，可进入完成评审**。本轮由自动化会话通过浏览器 + API 真实操作完成，
所有 ID/回执脱敏（尾 6 位），原始数据只在本机 `data/agent.db` 与 scratch。

## 1. 外部依赖的解除方式

| 原阻塞 | 解除方式（本轮真实操作） |
|---|---|
| 缺少可读/可 @ 的测试群 | 机器人经 `im:chat:create`（已有权限）真实建群" EvoHarness F01 测试群"（oc_…cf62f0），owner 经 members API 入群 |
| owner 无法实机发送消息 | OAuth 自然人授权链路打通：开放平台添加回调地址 → 授权页 owner 账号点"授权"（新增权限共 1~4 项的交互同意截图/日志在案）→ user_access_token（scope: im:message, im:message.send_as_user, im:chat, im:chat:update）→ 以 owner 身份真实发消息 |
| 群历史拉取缺权限 | 开放平台真实开通 `im:message.group_msg`（免审，自动发布） |
| 撤权无真实目标 | 机器人以群主身份解散测试群（`im:chat` 应用身份补开），复验撤权对账 |

## 2. 来源能力矩阵（脱敏实测）

| 来源 | 发现方式 | 事件覆盖 | 历史覆盖 | 读/写身份 | 原话题回复 | 实测样本 |
|---|---|---|---|---|---|---|
| 机器人私聊 | manual（bot_list 不含 p2p） | 通过（前会话+今日纠正/验证指令） | 通过 | bot/bot | —（私聊回复原会话） | 纠正 #1 → 候选 #4，回执 …bab13e；验证 #4 → 发布 v4，回执 …c739e8 |
| 群内 @ 机器人 | bot_list | 通过：`is_mention=1`，生成待批草稿 | 通过（权限传播收敛后） | bot/bot | 是（reply API） | …9ed350，mention=1，cat=question，Draft #6 |
| 群内无 @ | bot_list | 通过：`is_mention=0`，ingest-only（不触发回复） | 同上 | bot/bot | 不适用 | …ea0263 |
| 同群话题 | message.thread_id | 通过：thread_id 随事件入库 | 通过 | bot/bot | reply_in_thread=true | …a7c06d，thread …8f5b46 |
| 用户授权自然人单聊 | OAuth 授权码（v2 token 端点） | 不适用（发送身份） | 不适用 | user/user | — | 4 条真实 owner 消息（纠正/验证/群@/群无@/话题） |
| 撤权会话 | 群解散后 list_chats 不再返回 | 事件自然停止 | 停止（来源撤权） | — | outbox 预检阻断 | 群解散 → `revoked_at=2026-11-07 12:44:49Z` 自动写入 |

## 3. 发送幂等矩阵（真实 API）

| 场景 | uuid | 结果 |
|---|---|---|
| 私聊同键两次 | f01-idem-…b5a366 | 两次都 `sent`，回执相同（…21fdb8）→ 幂等成立 |
| 私聊不同键 | f01-idem-…2570 | 新回执（…3ca66e）→ 正常独立发送 |
| 群聊同键两次（chat_id 类型，修复后） | f01-groupfix-… | 两次 `sent`，回执相同（…cf4b45）→ 幂等成立 |
| 群聊以 open_id 类型发送（修复前缺陷路径） | — | `permanent` code=99992361 "open_id cross app"（缺陷证据，见 §4） |
| 无效目标 oc_invalid… | — | `permanent` code=99992351（4xx 分类正确，不重试） |

## 4. 本轮发现并修复的真实缺陷（均有回归测试）

1. **`transport.list_chats` 读不存在的 `chat_type` 属性**（SDK ListChat 无该字段）→ 来源发现直接崩溃。
   修复：`chat_type_of()` 按 `p2p_target_id` 推导，保留 dict 兼容。回归：`test_transport_chat_type.py` 4 例。
2. **`send_text/send_card` 硬编码 `receive_id_type="open_id"`** → 对群（oc_）发送必得 99992361。
   日报此前只发 owner 单聊所以未暴露。修复：`receive_id_type_of()` 按前缀推导。回归：前缀矩阵 + monkeypatch 断言。
3. **历史接口提及丢失**：历史 API 的 `mention.id` 是字符串 open_id，原实现只按对象取值且吞异常。
   修复：`_extract_mentions` 兼容两种形态。回归：历史/事件双形态 + `mentions_bot` 断言。
4. **`revoked_at` 无写入路径**（只有读取）：机器人被移出群/群解散后来源永久 enabled。
   修复：`bootstrap_sources` 新增 `_reconcile_group_revocations`——发现成功且群不可见→写 revoked_at；
   重新可见→清除；列举失败→不动（防抖动误杀）；p2p/话题来源不参与。回归：5 例。
5. **`main.py daily` 不写 daily heartbeat**：手动日报后健康探针误报 `daily_heartbeat_missing`。
   修复：`run_manual_daily()` 成功/失败都落心跳（trigger=manual）。回归：2 例。

## 5. 其他真实运维发现（记录，不改代码）

- **权限变更的 token 级传播窗口**：开通 `im:message.group_msg` 后约 1 小时内，新铸造的
  tenant token 一部分带新权限、一部分不带（HTTP 200 + 空 items 而非报错）。探针与补采
  需要"多进程重试"窗口。曾用同一 token 6 连发全部一致（per-token 而非 per-request）。
- **"成功但空"响应会把补采窗口错误封口为 complete**：窗口状态以 success 落盘，之后即使
  权限收敛也不会重拉。对账建议：窗口封口时记录 fetch 的 HTTP 层证据（条数、token 代次），
  或对"0 条窗口"延迟封口。本轮以重置 `source_sync` 窗口行强制重拉（见 §7）。
- **SQLite database locked 与事件重投递自愈**：后台重试脚本与实时写入争抢时，事件处理
  曾报 `持久化失败（不确认事件）`，飞书随后重投递，消息最终入库。确认-重投递机制在真实
  条件下自愈成立；并发写场景建议 WAL（后续工单，不在 F01 范围内改）。
- **群 `chat_mode=topic` API**：PUT 返回 success 但 GET 仍 group。话题覆盖改用
  `reply_in_thread=true`（thread_id 全链路验证通过）证实；怪癖记录在案。
- **bot_list 不含 p2p 会话**：私聊来源必须显式登记（系统已按 manual discovery 支持）。

## 6. 复验命令

```powershell
cd D:\claudecode\EvoHarness\feishu-daily-agent
.venv\Scripts\python.exe -m pytest tests -q            # 93+ 通过
.venv\Scripts\python.exe scripts\probe_feishu.py --read --json D:\claudecode\scratch\f01-read.json
# 写探针（显式目标）：
#   私聊 --target ou_…412dc8；群 chat_id 类型修复后 --target oc_…（见 scratch/f01_idempotency_matrix.py）
```

## 7. 残留与不宣称

- 群历史补采在权限传播窗口内曾"封口但空"；已重置窗口状态，最终轮（token 收敛后）验证
  fetch 6/6 稳定。19:16 首条 @ 消息因当时来源未登记而未被事件路径入库，其内容已由后继
  探针消息等价覆盖；补采对已解散群不再可行（来源已撤权）。
- OAuth 只验证到"owner 授权 + user token 真实发送/群管理"；refresh 生命周期与撤权后的
  token 失效未测（本轮未触碰 refresh 流程），如实留作后续。
- 本报告不含任何真实凭据、完整消息正文与未脱敏 ID。
