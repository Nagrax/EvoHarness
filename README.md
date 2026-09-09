# EvoHarness

**自进化 Agent 运行时 · Self-Evolving Agent Runtime**

[![CI](https://github.com/Nagrax/EvoHarness/actions/workflows/ci.yml/badge.svg)](https://github.com/Nagrax/EvoHarness/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/Web-FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Models](https://img.shields.io/badge/Models-OpenAI%20%7C%20Anthropic-8A2BE2)](https://platform.openai.com/)

模型负责推理，运行时负责执行。EvoHarness 是一个可运行、可阅读、可扩展的本地 Coding Agent Runtime：统一编排大模型推理、工具调用、文件编辑、Shell 执行、权限控制、长期记忆、Skills 自进化、MCP 外部工具、子 Agent 和会话恢复，并提供 **CLI** 与 **Web** 双前端。

## 界面预览

Web 前端通过 SSE 实时展示 Agent 的推理文本、工具调用时间线、token 用量与运行轮次：

<p align="center">
  <img src="docs/screenshots/overview.png" width="82%" alt="EvoHarness Web 首页" />
</p>
<p align="center">
  <img src="docs/screenshots/chat.png" width="82%" alt="对话与工具调用时间线" />
</p>

## 核心特性

| 能力 | 说明 | 主要实现 |
| --- | --- | --- |
| Agentic Harness 运行时 | 完整的推理 → 行动 → 观察闭环；模型只提出 tool call，环境操作由运行时统一执行 | `agents/agent.py` |
| 自主会话记忆折叠 | 长程任务中将历史交互重组为结构化 session state，而非简单裁剪 | `agents/session_memory.py` |
| Skills 自进化 | 从用户反馈中抽取可复用经验，自动 add / merge / discard 写入 `SKILL.md` | `agents/online_skill_evolution.py` |
| Skills 质量评测 | 来源追踪 + replay 样本池 + 规则评测 + LLM Judge + champion 记录 | `agents/online_skill_eval.py` |
| Memory + Skills 分层上下文 | Memory 保存项目事实与偏好，Skills 保存可复用流程 | `agents/memory.py` |
| 工具与权限控制 | 模型意图与本地环境动作隔离；Plan Mode 下阻断写操作与 Shell | `agents/tools.py` |
| MCP 外部工具 | 自研 stdio JSON-RPC 客户端，外部工具统一暴露为 `mcp__server__tool` | `agents/mcp_client.py` |
| 子 Agent | explore / plan / general 及自定义子 Agent，隔离上下文完成任务 | `agents/subagent.py` |
| Web 全栈 | FastAPI + SSE 事件桥，前端零依赖单文件，核心代码零改动接入 | `server/app.py`、`frontend/` |

## 运行时链路

```text
用户输入
  -> 构建 Prompt / 检索 Skills / 预取 Memory / 初始化 MCP
  -> 调用 OpenAI-compatible 或 Anthropic-compatible 模型
  -> 模型返回文本或 tool call
  -> Harness 权限检查
  -> 执行工具 / Skill / MCP / 子 Agent
  -> tool result 回写模型，继续推理
  -> 保存 Session
  -> 后台执行 Skill usage tracking 与在线自进化
```

Web 模式下，`server/app.py` 在运行时命名空间替换终端输出函数，将上述循环中的文本流、工具调用与结果转为 SSE 事件推送给浏览器，`agents/` 核心代码不做任何修改。

## 快速开始

### 1. 环境准备

- Python 3.11+
- ripgrep（可选，推荐）
- 一个 OpenAI-compatible 或 Anthropic-compatible 模型接口

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. 配置 `.env`

项目会自动读取当前目录或父目录中的 `.env`，参考 `.env.example`。

通用变量（协议由 base URL 自动判断，路径包含 `/anthropic` 时走 Anthropic 协议）：

```env
APIKEY=sk-your-api-key
API=https://your-host/v1
MODEL=deepseek-chat
```

也可以使用专属变量 `OPENAI_BASE_URL` + `OPENAI_API_KEY`，或 `ANTHROPIC_BASE_URL` + `ANTHROPIC_API_KEY`。

> [!WARNING]
> `.env` 已被 `.gitignore` 忽略。切勿将 API key 提交到仓库或粘贴到任何公开场合。

### 3. 启动

**Web 前端**（推荐）：

```bash
python -m server.app             # 或 Windows 双击 start_web.bat
```

浏览器打开 <http://127.0.0.1:8800>，选择权限模式（自动编辑 / 计划 / 直接执行）后即可下达任务。

**CLI 前端**：

```bash
python -m agents.main                        # 交互式 REPL
python -m agents.main "总结这个项目的核心模块"  # 一次性任务
python -m agents.main --plan "..."           # Plan Mode：先出计划再执行
python -m agents.main --resume               # 恢复最近会话
```

## Skills 自动沉淀与进化

EvoHarness 的核心特色是自进化 Skills：从用户明确反馈中抽取可复用规则，自动新增或合并到项目级 / 用户级 `SKILL.md`。

```env
EVOHARNESS_AUTO_SKILL_EVOLUTION=1      # 启用在线 Skill 自进化（默认开启）
EVOHARNESS_AUTO_SKILL_TARGET=project   # project: .evoharness/skills/  |  user: ~/.evoharness/skills/
```

后台自动写入 Skill 需要当前权限模式允许写文件，推荐以 `--accept-edits` 启动。

## 重要数据路径

| 数据 | 路径 |
|------|------|
| 项目级 Skills | `.evoharness/skills/<skill_name>/SKILL.md` |
| 用户级 Skills | `~/.evoharness/skills/<skill_name>/SKILL.md` |
| Skills 自进化审计 | `.evoharness/skill-evolution/` |
| 长期记忆 | `~/.evoharness/projects/<project_hash>/memory/` |
| 会话历史 | `~/.evoharness/sessions/` |
| 大工具结果 | `~/.evoharness/tool-results/` |
| Plan Mode 计划 | `~/.evoharness/plans/` |

## Docker

```bash
docker build -t evoharness .

docker run --rm -it \
  --env-file .env \
  -v "$PWD:/workspace" \
  -v evoharness-sessions:/root/.evoharness \
  -v evoharness-memory:/root/.evoharness \
  evoharness
```

## 目录结构

```text
EvoHarness/
├── agents/                     # Agent Runtime 核心
│   ├── main.py                 # CLI 入口与 REPL
│   ├── agent.py                # Agent Loop、模型调用、工具调度、上下文压缩
│   ├── tools.py                # 内置工具与权限系统
│   ├── memory.py               # 长期记忆
│   ├── skills.py               # Skills 加载、检索、执行
│   ├── online_skill_evolution.py / skill_evolution.py
│   ├── online_skill_eval.py    # Skills 在线评测
│   ├── session_memory.py       # 会话记忆折叠
│   ├── mcp_client.py           # MCP stdio 客户端
│   └── subagent.py / session.py / prompt.py / ui.py
├── server/                     # Web 后端（FastAPI + SSE 事件桥）
├── frontend/                   # Web 前端（零依赖单文件）
├── docs/screenshots/           # 界面截图
├── .evoharness/                # 项目级 Skills 与运行时产物
├── Dockerfile
└── requirements.txt
```
