"""MCP tool hints 静态清单。

EvoHarness 暴露给 MCP 宿主的每个工具的行为注解（readOnlyHint /
destructiveHint / idempotentHint / openWorldHint），与 agents/tools.py
中 TOOL_HINTS 运行时注入保持同源。此文件作为静态清单存在，供 MCP 生态
信任索引（如 M8ven Trust Index）与 OpenAI 工具目录检查直接读取：
OpenAI 的工具目录要求所有工具的四个 hint 均为显式布尔值。

值必须与工具处理器的实际行为一致：
- readOnlyHint: 工具是否不产生任何环境副作用
- destructiveHint: 工具是否可能覆盖/删除数据（write/edit/shell/skill 落盘类为 true）
- idempotentHint: 相同参数重复调用是否得到相同结果
- openWorldHint: 工具是否与不可预测的外部世界交互（shell 可执行任意命令为 true）
"""

TOOL_HINTS: dict[str, dict[str, bool]] = {
    "read_file":       {"readOnlyHint": True,  "destructiveHint": False, "idempotentHint": True,  "openWorldHint": False},
    "write_file":      {"readOnlyHint": False, "destructiveHint": True,  "idempotentHint": True,  "openWorldHint": False},
    "edit_file":       {"readOnlyHint": False, "destructiveHint": True,  "idempotentHint": True,  "openWorldHint": False},
    "list_files":      {"readOnlyHint": True,  "destructiveHint": False, "idempotentHint": True,  "openWorldHint": False},
    "grep_search":     {"readOnlyHint": True,  "destructiveHint": False, "idempotentHint": True,  "openWorldHint": False},
    "run_shell":       {"readOnlyHint": False, "destructiveHint": True,  "idempotentHint": False, "openWorldHint": True},
    "skill":           {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    "compact_context": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True,  "openWorldHint": False},
    "skill_evolve":    {"readOnlyHint": False, "destructiveHint": True,  "idempotentHint": False, "openWorldHint": False},
    "skill_create":    {"readOnlyHint": False, "destructiveHint": True,  "idempotentHint": False, "openWorldHint": False},
    "enter_plan_mode": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True,  "openWorldHint": False},
    "exit_plan_mode":  {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    "agent":           {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    "tool_search":     {"readOnlyHint": True,  "destructiveHint": False, "idempotentHint": True,  "openWorldHint": False},
}
