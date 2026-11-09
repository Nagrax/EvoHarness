"""分类、任务抽取、起草与经验抽取。

默认使用确定性规则，保证离线测试可复现；只有显式 `FEISHU_ALLOW_LIVE_MODEL=1`
且部署模式不是 offline 时才调用真实模型。模型输出永远只是证据/草稿，
不能直接改变发送权限。
"""
from __future__ import annotations

import json
import re
from datetime import timedelta

from app.core.clock import logical_date_for, utcnow
from app.core.config import get_settings
from app.llm.prompts import CLASSIFY_TEMPLATE, EXPERIENCE_TEMPLATE, TASK_EXTRACT_TEMPLATE, prompt_version
from app.models import Category

_QUESTION_RE = re.compile(r"(\?|？|吗|怎么|如何|为什么|能否|可不可以|请问|求助|怎么办)")
_TASK_RE = re.compile(r"(请|麻烦|记得|需要|务必|截止|提交|完成|安排|跟进|确认一下|帮我)")
_NOTICE_RE = re.compile(r"(通知|公告|周知|同步一下|FYI|知会)")
_DUE_RE = re.compile(
    r"(今天|明天|后天|本周[一二三四五六日天]|周[一二三四五六日天]|星期[一二三四五六日天]|"
    r"\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}月\d{1,2}日)"
)
_PROMISE_RE = re.compile(r"(我会|我来|我负责|稍后给|尽快)")


def _live_allowed() -> bool:
    settings = get_settings()
    return bool(settings.allow_live_model and settings.deployment_mode != "offline")


def _call_model(purpose: str, prompt: str) -> dict | None:
    if not _live_allowed():
        return None
    try:
        from app.llm.client import get_llm_client

        settings = get_settings()
        response = get_llm_client().chat.completions.create(
            model=settings.llm_model,
            temperature=settings.llm_temperature,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.choices[0].message.content or ""
        match = re.search(r"\{.*\}", text, re.S)
        return json.loads(match.group(0)) if match else None
    except Exception:
        return None


def classify_message(text: str) -> dict:
    body = (text or "").strip()
    if not body:
        return {"category": Category.UNKNOWN, "summary": "", "due": "", "evidence": []}
    result = _call_model("classify", CLASSIFY_TEMPLATE.replace("{text}", body))
    if result and result.get("category") in {
        Category.QUESTION, Category.TASK, Category.NOTICE, Category.UNKNOWN
    }:
        result.setdefault("summary", "")
        result.setdefault("due", "")
        result.setdefault("evidence", [body[:120]])
        return result
    if _QUESTION_RE.search(body):
        category = Category.QUESTION
    elif _TASK_RE.search(body):
        category = Category.TASK
    elif _NOTICE_RE.search(body):
        category = Category.NOTICE
    else:
        category = Category.UNKNOWN
    due = ""
    match = _DUE_RE.search(body)
    if match and category == Category.TASK:
        due = match.group(0)
    return {
        "category": category,
        "summary": body[:60],
        "due": due,
        "evidence": [body[:120]],
    }


def generate_fallback_reply(question: str) -> str:
    """没有经验时的保守回复：只确认收到，不承诺、不编造。"""
    result = _call_model("fallback", f"为下面的问题拟一条不超过60字的保守回复，只确认收到并说明会人工跟进，不要承诺：\n{question}")
    if result and isinstance(result.get("reply"), str) and result["reply"].strip():
        return result["reply"].strip()
    return "收到，我先确认一下相关信息，稍后回复你。"


def compose_reply(question: str, experience) -> str:
    return (experience.canonical_answer or "").strip()


def extract_experience(question: str, answer: str) -> dict:
    result = _call_model("experience", EXPERIENCE_TEMPLATE.replace("{question}", question).replace("{answer}", answer))
    if result:
        return result
    return {
        "question": question,
        "answer": answer,
        "preconditions": "",
        "steps": "",
        "negative_examples": [],
    }


def extract_tasks(text: str, message_id: str) -> dict:
    result = _call_model("tasks", TASK_EXTRACT_TEMPLATE.replace("{text}", text))
    if result and isinstance(result.get("tasks"), list):
        return result
    classification = classify_message(text)
    if classification["category"] != Category.TASK:
        return {"tasks": []}
    due = classification.get("due", "")
    action = text.strip()[:200]
    return {
        "tasks": [
            {
                "action": action,
                "assignee": "",
                "due": due,
                "evidence": [message_id],
                "confidence": 0.6,
                "needs_confirmation": True,
            }
        ]
    }


def prompt_versions() -> dict:
    return {
        "classify": prompt_version("classify"),
        "tasks": prompt_version("tasks"),
        "experience": prompt_version("experience"),
    }
