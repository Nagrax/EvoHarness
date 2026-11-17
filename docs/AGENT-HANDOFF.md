# EvoHarness 开发导航

EvoHarness 是一个数字员工组织系统。`evoharness/` 负责组织、目标、任务、授权与知识；`agents/` 是执行运行时；Web、CLI 和飞书是同一产品的入口。个人数字分身沿用个人身份与知识范围，不等于组织员工。

## 授权边界

模型给出执行意图，应用服务校验主体、岗位、空间、任务版本、批准载荷与预算。撤权后旧 Run 不得继续新增动作；发送使用 outbox 与真实回执。群消息默认 @ 触发，不授予群成员额外权限。

## 关键文件索引

- [统一 API](../evoharness/api.py)、[根命令](../evoharness/__main__.py)、[配置](../evoharness/config.py)
- [Worker](../evoharness/work/worker.py)、[执行适配](../evoharness/work/agent_adapter.py)
- [知识检索](../evoharness/knowledge/retrieval.py)、[岗位 Skills](../evoharness/knowledge/scoped_skills.py)
- [Web](../server/app.py)、[工作台](../frontend/index.html)、[门禁](../eval/digital_employee_gate.py)

## 别踩的坑

所有入口复用应用服务；不要再造任务表或复制组织授权判定。不要把个人 OAuth 与组织服务身份混用。评测数据与测试夹具分别管理，缺少真实实验数据时保留未执行状态。

身份与发送守护测试包括 `test_digital_twin_boundary.py`、`test_digital_twin_ownership.py`、`test_twin_send_identity.py`，位于 `feishu-daily-agent/tests/`。测试不得读取真实凭据或改写运行数据库。

## 已知未做

真实渠道权限、本人 OAuth、自然周期样本和生产运行验收需按实际环境完成。GitHub 与本地统一维护同一产品代码；历史上游/本地克隆差异只作版本背景，见[历史兼容说明](与原版的差异.md)。

验证命令与安装见[README](../README.md)，部署见[运行说明](OPERATIONS.md)。
