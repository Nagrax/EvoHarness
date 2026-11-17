"""常在线部署与恢复工具（DE-23）。

- `single_active`：单活写实例守卫（锁文件 + 数据库租约，第二实例明确拒绝并给原因）。
- `supervisor`：启动前迁移/密钥/配置检查、中断恢复、优雅停机、单活启动主管。
- `backup`：业务 DB + token 密钥协调备份、恢复到临时目录核验、升级回退演练。

配套文档：`docs/digital-employee/OPERATIONS.md`。
"""
