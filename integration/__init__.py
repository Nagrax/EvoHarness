"""EvoHarness 本机集成层：飞书 ↔ 闭环自迭代桥接。

模块：
- config        路径与 .env 加载
- feishu_notify 飞书消息投递（无凭证时自动降级为本地落盘）
- loop_report   闭环运行摘要生成
- agent_ask     经 /api/chat 调用 EvoHarness Agent 一次性问答
- task_intake   飞书任务类消息 → eval/loop/BACKLOG.json 台账
- iterate       闭环编排入口（--once / --daemon / --status）
"""
