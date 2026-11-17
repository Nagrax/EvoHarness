# 运行与维护

本版本按单组织、单活服务部署。Web、Worker 使用同一个数据库 URL 与产物目录；飞书按需配置。组织库不能使用启动目录不同而指向另一份 SQLite 文件的相对路径，常驻部署宜传绝对路径。

## 本地

使用根 README 的初始化命令，启动 Web，再运行 `worker --daemon --profile live`。模型配置来自自己的环境或 `.env`。Web 的浏览器模型设置仅作用于对应请求，后台 Worker 需独立可读的模型配置。

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

## 升级与恢复

先停止新派工并等待在途操作收敛，保留未知回执，再备份数据库与产物。SQLite 使用备份工具的一致快照；不要直接复制正在写入的数据库。

```sh
python ops/backup.py --help
python ops/supervisor.py --help
```

升级前在副本上验证迁移和二次幂等，核对组织、任务、知识、outbox 与产物引用。恢复同样先在隔离目录演练；保留原库和原产物，确认通过后再切换服务。

`/api/health` 和 `python -m evoharness --db <URL> worker --health` 用于检查组件、队列与租约。失败由任务事件、运行检查点及测试回执定位。未经授权的动作等待决定；预算耗尽停止新模型调用。
