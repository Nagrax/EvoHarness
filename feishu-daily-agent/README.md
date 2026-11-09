# 飞书日报与自动回复 Agent

每天自动吸收飞书消息，把重复问题沉淀为经验，生成含待办任务的日报；
高置信度问题基于经验自动回复，人工通过卡片批示后持续迭代经验库。

## 工作闭环

```
飞书消息
  │  ① 长连接事件（实时） + ② 每日历史拉取（兜底）
  ▼
消息分类（question / task / notice）
  ▼
经验库语义匹配（embedding 或 jieba 关键词）
  ├─ 高置信度 + 已验证经验 → 直接自动回复，日报备查
  └─ 中低置信度 / 无命中   → 生成草稿进日报
  ▼
日报卡片（待办 + 自动回复 + 新经验 + 草稿审批）
  ▼
人工批示：批准 / 驳回 / 修改 / 验证 / 停用
  ▼
回写经验库，迭代置信度与状态
```

## 目录结构

```
feishu-daily-agent/
├── main.py                 # 入口：run / daily / init-db
├── app/
│   ├── core/               # 配置、日志、数据库
│   ├── models/             # 消息、经验、草稿、审批日志实体
│   ├── feishu/             # 飞书客户端、消息收发、内容解析、卡片
│   ├── llm/                # LLM 客户端、提示词、分类/抽取/回复
│   ├── memory/             # 语义匹配器与经验库操作
│   ├── pipeline/           # 每日流水线与定时调度
│   ├── handlers/           # 消息事件、卡片回调、指令解析
│   └── services/           # 回复决策引擎（实时/日报共用）
├── tests/                  # 端到端闭环集成测试
├── data/                   # SQLite 持久化
├── Dockerfile
└── docker-compose.yml
```

## 快速开始

### 1. 配置飞书应用

按 [FEISHU_APP_SETUP.md](./FEISHU_APP_SETUP.md) 创建自建应用、开通权限、
订阅事件并发布，拿到 App ID / Secret 和你的 open_id。

### 2. 准备配置

```bash
cp .env.example .env
# 编辑 .env，至少填好：
#   FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_OWNER_OPEN_ID
#   LLM_BASE_URL / LLM_API_KEY / LLM_MODEL
```

LLM 与 embedding 均为 OpenAI 兼容协议，可接 DeepSeek、豆包、通义等。
若供应商不支持 embedding，设置 `MATCHER_MODE=keyword`，使用内置
jieba + TF-IDF 关键词匹配，无需 embedding 接口。

### 3. 运行

方式一：Docker（推荐）

```bash
docker compose up -d
docker compose logs -f
```

方式二：本地 Python（3.11+）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

手动触发一次日报（不等定时）：

```bash
python main.py daily
```

## 批示与指令

日报中每条待审批草稿带「批准」「驳回」按钮。也可直接给机器人发指令：

| 指令 | 作用 |
|---|---|
| `改 #草稿编号 新内容` | 用修改后的内容发送该草稿 |
| `验证 #经验编号` | 经验转为已验证，可用于自动回复 |
| `停用 #经验编号` | 停用某条经验 |
| `驳回 #草稿编号` | 驳回草稿 |

## 置信度策略

- `AUTO_REPLY_THRESHOLD`：相似度达到该值且经验为 `verified` 时直接回复，默认 0.85。
- `DRAFT_MIN_THRESHOLD`：低于该值不基于经验拟稿，由 LLM 新拟草稿，默认 0.55。
- 无经验来源的回复被批准后，会自动沉淀为新经验（draft 状态）。
- 经验被连续驳回达到 `EXPERIENCE_DISMISS_LIMIT` 时自动停用。

## 测试

```bash
pip install -r requirements-dev.txt
pytest -v
```

测试覆盖：无命中草稿、批准发送与经验沉淀、已验证经验自动回复、
驳回停用、非问题消息隔离、幂等处理。

## 说明与边界

- 群聊默认仅响应 @机器人 的消息（`FEISHU_GROUP_REPLY_ONLY_MENTION`）。
- 消息内容会经过 LLM，敏感会话可用 `FEISHU_CHAT_BLACKLIST` 排除。
- 长连接为集群模式，同一应用多实例时只有一个实例收到事件，避免多副本重复回复。
- 数据库默认 SQLite，零外部依赖；切换 PostgreSQL 只需改 `DATABASE_URL`。
