#!/usr/bin/env python3
"""long-function 18 处的评估结论（这是 medium 级"可优化项"，不是缺陷）。

评估标准（先定标准再看代码，避免"看到长就想拆"）：
  该拆 ⇔ ① 函数内有**可独立命名的子步骤** ② 该子步骤**有复用意願** ③ 分支密度高
  不该拆 ⇔ 长是因为**数据字段多/线性写盘**（拆了只是把字典挪走，可读性没提升）

结论分三类，落盘到 eval/runs/long-function-assessment.json。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(r"D:/claudecode/EvoHarness")

# 依据 list_long_funcs.py 的实测指标 + 逐个读代码后的判断
ASSESSMENT = {
    "should_split": [
        {"func": "eval/loop.py:collect", "length": 440, "branches": 52,
         "why": "12 个信号源采集 + 合并 + triage + 落盘全在一个函数里，"
                "分支密度最高（52）；每加一个信号源都要改它",
         "plan": "按信号源拆成 _collect_<source>() 系列，主函数只保留编排",
         "risk": "high（闭环核心，21 轮迭代都在用）"},
        {"func": "agents/online_skill_eval.py:_evaluate_online_skill_evolution_core",
         "length": 398, "branches": 24,
         "why": "评测主流程 + 僵尸归档 + 晋级 + 报告生成串在一起",
         "plan": "拆出 _archive_zombies() / _apply_promotions() / _finalize_report()",
         "risk": "medium"},
        {"func": "eval/evolution_experiment.py:_cmd_run", "length": 289, "branches": 15,
         "why": "三臂编排 + 轮次循环 + 落盘 + 曲线生成混在一起",
         "plan": "拆出 _run_arm_round() / _write_round_artifacts() / _build_curve()",
         "risk": "medium"},
        {"func": "agents/agent.py:_chat_openai", "length": 258, "branches": 45,
         "why": "流式循环 + 工具调用 + 折叠 + 用量记账，分支 45",
         "plan": "拆出 _handle_tool_round() / _maybe_compact() / _account_usage()",
         "risk": "high（主对话链路）"},
        {"func": "agents/agent.py:_chat_anthropic", "length": 226, "branches": 34,
         "why": "同上（两套协议对称实现）",
         "plan": "与 _chat_openai 抽出公共骨架",
         "risk": "high"},
        {"func": "agents/main.py:run_repl", "length": 193, "branches": 43,
         "why": "命令分发 43 个分支，命令表化更合适",
         "plan": "改成命令注册表 dict[name, handler]",
         "risk": "medium"},
    ],
    "optional": [
        {"func": "eval/memory_eval.py:main", "length": 212, "branches": 10,
         "why": "已有 7 个子函数，结构尚可；长是因为场景清单"},
        {"func": "eval/backend_contract.py:main", "length": 189, "branches": 19,
         "why": "契约检查清单线性排列，拆了只是挪位置"},
        {"func": "eval/arch_run.py:main", "length": 137, "branches": 16, "why": "CLI 入口"},
        {"func": "agents/online_skill_eval.py:_build_candidate_eval_bundle_async",
         "length": 151, "branches": 15, "why": "构建候选 bundle，流程线性"},
        {"func": "agents/online_skill_eval.py:_compile_eval_rules", "length": 140,
         "branches": 8, "why": "规则编译，分支少"},
        {"func": "agents/online_skill_evolution.py:maintain_online_skill_candidate",
         "length": 134, "branches": 11, "why": "候选维护流程"},
        {"func": "agents/agent.py:__init__", "length": 146, "branches": 6,
         "why": "__init__ 长通常是字段多，拆分意义小"},
        {"func": "agents/main.py:main", "length": 126, "branches": 10, "why": "CLI 入口"},
    ],
    "should_not_split": [
        {"func": "agents/online_skill_eval.py:_persist_eval_artifacts",
         "length": 170, "branches": 6,
         "why": "**纯数据组装 + 写盘**：170 行里绝大部分是字典字面量。"
                "拆成 _build_outputs()/_build_judgments() 只是把字典挪走，"
                "调用方反而要多跳一层，可读性没有实际提升",
         "note": "已逐个读过代码确认；6 个分支说明逻辑简单"},
        {"func": "server/app.py:skills_overview", "length": 137, "branches": 5,
         "why": "扁平的数据聚合（把多张表拼成一个响应），无子步骤"},
        {"func": "agents/experience_standard.py:validate_record", "length": 131,
         "branches": 40,
         "why": "**校验规则链**：40 个分支每条是一条校验规则，拆开会让"
                "『按顺序校验并在首个失败处返回』的语义变模糊"},
        {"func": "agents/tools.py:check_permission", "length": 130, "branches": 30,
         "why": "策略判定，注释密度 24 行（说明每步都有依据）；"
                "拆成小函数会让『拒绝优先于放行』的判定顺序更难验证"},
    ],
}



def _build_assessment() -> dict:
    """组装评估结论：过滤掉已归 deferred 的项，并补判据文本。

    `_cmd_run` 同时出现在常量 should_split 与 deferred 里（历史演进导致重复）。
    在**输出时**去重，比改常量更安全——常量是多行 dict，手工改容易破坏语法。
    """
    deferred_names = {str(e.get("func") or "") for e in ASSESSMENT.get("deferred") or []}
    out: dict = {}
    for key, value in ASSESSMENT.items():
        if not isinstance(value, list):
            continue
        out[key] = [e for e in value
                    if str(e.get("func") or "") not in deferred_names] \
            if key == "should_split" else value
    # `_cmd_run` 是判据第 4 条（有测试保护）不满足时的处置对象：
    # 循环体需真实模型验证，余额中止 / holdout 只读校验 / 批次隔离等
    # 评测正确性核心都在里面，无安全网时硬拆风险过高 → 推迟。
    out["deferred"] = ASSESSMENT.get("deferred") or [{
        "func": "eval/evolution_experiment.py:_cmd_run",
        "length": 193,
        "branches": 22,
        "reassessed": "推迟拆分：**无测试保护**（无安全网），"
                      "循环体需真实模型才能验证，硬拆等于在没有安全网时动评测核心；"
                      "处置是先补可测性再拆",
    }]
    out["standard_v2"] = (
        "该拆 <=> 有可独立命名的子步骤 AND 有复用意愿 AND 分支密度高 AND 有测试保护。"
        "第一条必须读代码确认；若第 4 条不满足（无测试保护），**先补可测性再拆**——"
        "否则无法验证拆完与否等价")
    out["standard_v4"] = (
        "该拆 <=> 有可独立命名的子步骤 AND 有复用意愿 AND 分支密度高 AND 有测试保护；"
        "且子步骤要占函数主要部分")
    return out


def main() -> int:
    result = {
        "kind": "long-function-assessment",
        "standard": "该拆 ⇔ 有可独立命名的子步骤 ∧ 有复用意愿 ∧ 分支密度高；"
                    "不该拆 ⇔ 长源于数据字段多/线性写盘/规则链顺序语义",
        "total": sum(len(v) for v in ASSESSMENT.values()),
        "counts": {k: len(v) for k, v in ASSESSMENT.items()},
        "assessment": _build_assessment(),
        "decision": "本轮只做评估，不批量拆分——18 处里 4 处（should_not_split）"
                    "拆分反而有害；6 处（should_split）风险 medium/high，需单独排期；"
                    "8 处（optional）收益低。**『长』本身不是缺陷，是信号**",
    }
    out = REPO / "eval" / "runs" / "long-function-assessment.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    # 该产物是**人工判读结论**（18 处逐个读代码后的分类），不是可重算数据。
    # 默认不覆盖，避免又一次把 deferred / standard_v2 冲掉；
    # 需要重算时显式加 --force。
    force = "--force" in sys.argv
    if out.is_file() and not force:
        print(json.dumps({"ok": True, "skipped": "artifact-exists (use --force)",
                          "total": result["total"], "out": str(out)},
                         ensure_ascii=False, indent=2))
        return 0
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print(json.dumps({"ok": True, "total": result["total"],
                      "counts": result["counts"], "out": str(out)},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
