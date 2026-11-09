"""规则分类回归：问句词优先于任务词，避免“申请”被当成任务指令。"""
from __future__ import annotations

from app.llm.service import classify_message
from app.models import Category


def test_question_phrases_are_questions():
    for text in (
        "出差申请谁来批",
        "年假申请的步骤",
        "会议室怎么预订？",
        "门禁卡丢了怎么办",
        "报销流程是什么？",
    ):
        assert classify_message(text)["category"] == Category.QUESTION, text


def test_task_cues_remain_tasks():
    for text in (
        "请帮我提交报销单",
        "麻烦明天完成报表",
        "记得跟进会议室预订",
    ):
        assert classify_message(text)["category"] == Category.TASK, text


def test_application_alone_is_not_task():
    assert classify_message("申请年假")["category"] != Category.TASK
