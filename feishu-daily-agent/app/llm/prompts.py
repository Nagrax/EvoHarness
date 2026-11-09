"""提示词模板与版本号；每次模型调用都必须把版本号写入账本/决策。"""
from __future__ import annotations

from app.core.config import get_settings


CLASSIFY_TEMPLATE = """你是飞书消息分类器。只输出 JSON：
{"category":"question|task|notice","summary":"不超过40字","due":"原文明确日期或空","evidence":["原文片段"]}
规则：不要把寒暄当问题；日期不明确时 due 必须为空；不要编造负责人。

消息：
{text}
"""

TASK_EXTRACT_TEMPLATE = """从飞书对话提取“需要我处理”的事项。只输出 JSON：
{"tasks":[{"action":"...","assignee":"...","due":"...","evidence":["消息ID或原文片段"],"confidence":0.0}]}
没有证据、负责人不明或日期不明确时，对应字段留空/标记 needs_confirmation。

对话：
{text}
"""

EXPERIENCE_TEMPLATE = """从已批准的真实答复提取可复用解法。只输出 JSON：
{"question":"...","answer":"...","preconditions":"...","steps":"...","negative_examples":[]}
不得把用户专属信息写成公开 FAQ；不得补充原文没有的事实。

问题：{question}
答复：{answer}
"""


def prompt_version(purpose: str) -> str:
    settings = get_settings()
    return f"{settings.llm_prompt_revision}:{purpose}"
