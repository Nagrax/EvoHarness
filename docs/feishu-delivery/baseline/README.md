# F00 基线冻结

- EvoHarness HEAD：`c25af95409db64d656ca677b1debf815d099d392`（2026-11-07 核对）
- 飞书原型来源：交接时未跟踪的 `feishu-daily-agent/`；冻结副本 `D:/claudecode/scratch/feishu-prototype-frozen/`，逐文件 sha256 见 `prototype-manifest.json`（45 个文件）。
- 原型测试：6 passed / 0 failed / 0 skipped，JUnit 见 `prototype-tests.junit.xml`；用系统临时目录 SQLite，`FEISHU_TEST_MODE=1`，网络阻断。
- 依赖快照：`prototype-dependencies.txt`（lark-oapi 1.7.5、SQLAlchemy 2.1.8、pydantic 2.14.3 等）。

## 测试隔离

原型 `tests/test_integration.py` 的 fixture 会 `drop_all` 导入时创建的引擎；如果环境没有显式 `DATABASE_URL`，默认指向 `data/agent.db`。F00 起：

- 测试入口固定 `FEISHU_TEST_MODE=1` + 系统临时目录 `DATABASE_URL`；
- `app/core/database.py::build_engine` 在测试模式拒绝非临时 SQLite；
- `tests/conftest.py` 阻断 AF_INET/AF_INET6 socket 连接，飞书 SDK/LLM 不触网。

## 已登记的差异

实现阶段对原型做了结构性改造（独立 intake/outbox/review/tasks/reports/experience），原始文件保存在冻结副本与 `D:/claudecode/scratch/feishu-prototype-module-backups/`，不覆盖历史证据。
