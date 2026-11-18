# 运行与维护

本版本按单组织、单活服务部署。Web、Worker 使用同一个数据库 URL 与产物目录；飞书按需配置。组织库不能使用启动目录不同而指向另一份 SQLite 文件的相对路径，常驻部署宜传绝对路径。

## 本地

使用根 README 的初始化命令，启动 Web，再运行 `worker --daemon --profile live`。模型配置来自自己的环境或 `.env`。Web 的浏览器模型设置仅作用于对应请求，后台 Worker 需独立可读的模型配置。

组织与执行空间分别通过 `python -m evoharness budget set --org <组织> --space <空间> --actor <管理员> --tokens <限额>` 设置累计 token 限额；省略 `--space` 设置组织限额。`budget view` 查看预留和结算，调整限额不会清空历史用量。

代码任务的 Worker 在宿主启动，通过 Docker 执行工具与检查器：

```sh
docker build -f ops/Dockerfile.checker -t evoharness-checker:latest .
```

默认工具镜像为 `python:3.13-slim`，默认检查镜像为 `evoharness-checker:latest`。可分别通过 `EVOHARNESS_TOOL_IMAGE`、`EVOHARNESS_CHECKER_IMAGE` 指定有项目依赖的镜像。检查控制器从工作区外只读挂载，记录完整 pytest 收集与 JUnit；shell 容器关闭网络、限制资源，只能写当前任务工作区，不接收宿主凭据或 Docker socket。

## Docker

```sh
docker build -t evoharness .
docker run --rm -p 8800:7860 --env-file .env -v /your/evo-data:/data -e EVOHARNESS_DATABASE_URL=sqlite:////data/org.db -e EVOHARNESS_HOSTED_TOKEN=your-own-token evoharness
```

镜像入口启动 Web；初始化与 Worker 使用相同镜像，通过 `--entrypoint python` 执行根命令。持久化目录需包含数据库与任务产物；代码任务还需配置受限执行环境、所用测试工具与工作目录。根 Compose 的 Worker 可运行无需 shell 的任务；执行环境未接入时，代码任务保留等待状态。宿主 CLI Worker 可使用已配置 Docker 的同一套运行器。

公网或共享部署配置管理令牌和注册主体令牌，在工作台设置访问令牌。岗位授权、任务批准与个人 OAuth 各自校验。浏览器模型 Key 保存在本机，访问令牌仅保存在当前标签页会话。

## 同一镜像运行完整产品

准备根 `.env`，填写模型、管理令牌；启用飞书时将飞书配置模板中的应用配置加入同一文件。

```sh
docker compose build
docker compose run --rm init
docker compose up -d web worker
# 按需启用同一产品的飞书渠道：
docker compose --profile feishu up -d
```

所有服务挂载根 `data/`，共同使用 `/data/org.db` 与产物目录。`feishu-daily-agent/docker-compose.yml` 保留旧入口兼容，构建同一根镜像；日常部署使用根 Compose 文件。

## Linux 常驻服务

`ops/evoharness-{web,worker,feishu}.service` 是使用同一产品代码的 systemd 模板。模板约定专用用户 `evoharness`、代码链接 `/opt/evoharness/current`、Python 环境 `/opt/evoharness-tools/venv`、配置 `/etc/evoharness/runtime.env` 和数据 `/var/lib/evoharness`。按目标机路径调整后安装所需服务。

配置文件至少包括以下字段；飞书启用时再加入渠道模板里的应用配置与独立保管的 token 加密密钥：

```dotenv
EVOHARNESS_DATABASE_URL=sqlite:////var/lib/evoharness/org.db
DATABASE_URL=sqlite:////var/lib/evoharness/org.db
EVOHARNESS_ACCEPTANCE_RECEIPTS=strict
API=https://your-provider.example/v1
APIKEY=your-api-key
MODEL=your-model-id
```

配置由 systemd 读取，权限设为 `0600`；代码目录不保存 `.env`。宿主 Worker 的服务用户需要能连接 Docker，容器内任务不挂载 socket。Web 模板只监听回环地址，可通过 SSH 转发访问；对外开放前配置主体令牌与反向代理。

```sh
sudo cp ops/evoharness-web.service ops/evoharness-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now evoharness-web evoharness-worker
systemctl status evoharness-web evoharness-worker
```

飞书另装 `ops/evoharness-feishu.service`，其 supervisor 使用进程锁和数据库租约拒绝第二个渠道实例。升级时先停止这些服务，完成一致备份与迁移，再切换 `current` 链接。

## 公网演示与正式工作区

公网演示使用独立系统用户和合成数据库。用根 CLI 的 init 命令初始化演示数据后，设置 EVOHARNESS_PUBLIC_DEMO=1 启用只读接口清单。演示实例独立监听回环地址，省略模型密钥、Worker 和飞书进程。

反向代理在 HTTPS 上把首页和普通 API 路由到演示实例。把 /workspace/ 及其所有子路径设为已认证入口，去掉 /workspace 前缀后转给私有 Web 实例。正式实例的直连监听保持在回环地址，反向代理验证每个请求。访问密码及其哈希保存在部署机，放在源码之外。

### 邀请制组织工作区

邀请制工作区使用独立 Web、Worker、数据库和 Skill 目录，不覆盖旧工作区、飞书影子实例或公网演示库。为新服务设置 `EVOHARNESS_WORKSPACE_ORIGIN`（完整 HTTPS Origin，不含路径）、`EVOHARNESS_DATABASE_URL` 和共享的 `EVOHARNESS_SKILLS_ROOT`。两服务使用相同数据库、模型配置和运行用户。先初始化独立数据库并生成一致快照，再启动 Web 与 live Worker。验证启动和登录后，再调整反向代理。

`ops/evoharness-workspace-web.service` 与 `ops/evoharness-workspace-worker.service` 提供单独的 Linux 服务模板。使用专用用户 `evoharness-workspace`；SQLite 库与 Skill 目录应在 `/var/lib/evoharness-workspace/`，配置保存在仅 root 可读的 `/etc/evoharness/workspace.env`。部署前验证 `current` 指向已测试发布目录，并保存反向代理原配置用于回滚。

`/workspace` 应重定向到 `/workspace/`；反向代理剥离 `/workspace` 前缀，将整个路径交给新 Web 服务，并限制请求体大小，例如 128 KiB。此路由不再使用 Basic Auth；应用对每个 API 请求验证独立会话，拒绝直接访问共享运行时。不要将新 Web 服务直接绑定公网。旧工作区继续使用独立入口和原认证，不共享 Cookie。

开通管理员账号：在新实例环境内以服务用户运行 `python -m server.workspace_invite`，通过受控渠道交付输出的一次性邀请码。登录后建立组织、邀请验收成员、添加数字员工，再从工作台创建任务和派工。人类验收后可保留复盘、提炼已有岗位 SOP 的候选修订，采用后仍进入实验分层。

### 飞书单消息导入

默认未配置。需要在独立组织工作区服务设置 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_CHAT_WHITELIST`，然后由运维在同一实例环境执行 `python -m server.workspace_feishu --org <组织ID> --space <空间ID> --chat <已批准会话ID>`。每个业务空间只能解析到一个有效的该应用会话绑定。组织管理员可在新建任务时输入该会话内的文本消息 ID；服务核对实际会话、保存内容快照摘要和原消息引用，不监听全群、不发送飞书消息、不继承旧群权限。需要群内即时协作、飞书身份登录或自动回执时，须另验这些通道，单消息导入不代表它们可用。

## 升级与恢复

先停止新派工并等待在途操作收敛，保留未知回执，再备份数据库与产物。SQLite 使用备份工具的一致快照；不要直接复制正在写入的数据库。

```sh
python ops/backup.py --help
python ops/supervisor.py --help
```

升级前在副本上验证迁移和二次幂等，核对组织、任务、知识、outbox 与产物引用。恢复同样先在隔离目录演练；保留原库和原产物，确认通过后再切换服务。

`/api/health` 和 `python -m evoharness --db <URL> worker --health` 用于检查组件、队列与租约。失败由任务事件、运行检查点及测试回执定位。未经授权的动作等待决定；预算耗尽停止新模型调用。
