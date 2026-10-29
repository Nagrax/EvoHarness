#!/usr/bin/env python3
"""把已判读且判定为"合理防御/正常控制流"的位置标记出来（闭合 triage 状态）。

为什么需要：判读是按文件做的，但只修其中真有问题的几处；其余"合理保留"的位置
仍挂在 pending → backlog 不反映真实状态，也无法区分"未判读"与"已判读无问题"。

本脚本按 2026-10-29 的判读结论，给这些位置打 `triage=reasonable-defense` + 理由。
未在表中的位置保持 `needs-review`（即真的还没看）。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

REPO = Path(r"D:/claudecode/EvoHarness")
BACKLOG = REPO / "eval" / "loop" / "BACKLOG.json"

# 文件 → 已判读的"合理保留"理由（按 2026-10-29 逐处读码结论）
VERDICTS: dict[str, str] = {
    "agents/agent.py": (
        "已逐处判读（8 处）：清理路径（coro.close / task.result 已修为留痕）、"
        "会话保存失败已记入 last_save_error、其余为降级但不影响主流程"),
    "agents/tools.py": (
        "已逐处判读（7 处）：tmp 清理失败后接 raise、kill 已退出进程、"
        "grep 非核心路径；mtime 登记失败两处已修为留痕"),
    "agents/mcp_client.py": (
        "已逐处判读（6 处）：管道/transport 关闭是清理路径、stderr 只作排洪道、"
        "kill 已退出进程；坏配置跳过一处已修为可见"),
    "agents/memory.py": (
        "已逐处判读（4 处）：stderr 不可用时放弃（防递归）、UnicodeDecodeError 后转 "
        "read_bytes 是编码迁移正常控制流；索引/扫描两处已修为汇总留痕"),
    "agents/online_skill_eval.py": (
        "已逐处判读（4 处）：JSON 两级解析是正常控制流、僵尸归档 fail-open 有注释；"
        "晋级失败一处已修为进 report"),
    "agents/agent.py#return-none": (
        "已逐处判读（3 处）：在线进化链路的三处静默已全部修为可见——"
        "online_ingest 导入失败、record_usage_samples 失败、外层异常；"
        "否则\"进化没发生\"与\"进化失败\"无法区分"),
    "agents/subagent.py": (
        "已逐处判读（1 处）：自定义代理文件解析失败已修为留痕（仍只跳过单个文件）"),
    "eval/run_benchmark.py": (
        "已逐处判读（2 处）：MCP 连接关闭属清理路径；JSONL 坏行已修为计数+提示"
        "（否则评测样本静默变少）"),
    "agents/online_skill_evolution.py": (
        "已逐处判读（1 处）：JSON 两级解析（先整体后找 {）属正常控制流，不改"),
    "eval/experience_eval/hallucination_probe.py": (
        "已逐处判读（1 处）：可选 ground-truth 文件读取失败返回空字典，调用方按缺省处理"),
    "eval/loop.py": (
        "已逐处判读（2 处）：git/工作树探测失败返回空值属预期（非 git 环境）"),
    "eval/arch_run.py": (
        "已逐处判读（1 处）：variance 文件解析失败 → 跨批噪声退化为 0。"
        "方向是保守的（噪声越小越难宣称），属安全降级"),
    "eval/memory_lifecycle_ab.py": (
        "已逐处判读（1 处）：client.close() 清理失败不影响已产出的结果"),
    "eval/reflection_recall_probe.py": (
        "已逐处判读（1 处）：client.close() 清理失败不影响已产出的结果"),
    "agents/prompt.py": (
        "已逐处判读（2 处）：规则文件与 CLAUDE.md 读取失败已修为留痕——"
        "否则坏规则被静默跳过，用户会以为它在约束模型"),
    "agents/skill_evolution.py": (
        "已逐处判读（2 处）：register_evolution_write 失败属 fail-open，"
        "代码注释明确声明缺口由 tier_audit.jsonl 缺行暴露（有替代检测通道）→ 合理防御，不改"),
    "server/app.py": (
        "已逐处判读（3 处）：淘汰会话与换凭据时的 abort 失败已修为留痕"
        "（对象移除但任务可能仍在跑）；修复后缓存刷新失败已回传给调用方"
        "（cacheRefreshError + hint，避免误报已生效）"),
    "eval/evolution_experiment.py": (
        "已逐处判读（4 处）：.env 加载已修为三态区分（依赖缺失/文件不存在静默，"
        "解析失败可见）；taskIds JSON 读取失败回退到扫目录是正常控制流；"
        "MCP 连接关闭属清理路径；worker 无 JSON 输出的分支已单独留痕"),
    "agents/session.py#silent-except-return-none": (
        "已逐处判读（1 处）：_fsync_dir 的 os.open 失败直接 return —— 目录不存在"
        "或不可打开时放弃 fsync，属 Windows 下的预期降级"),
    "agents/session.py": (
        "已逐处判读（3 条记录/2 个位置）：_fsync_dir 的目录 fsync 在 Windows 上常不可用，"
        "注释已明确\"忽略即可\"；tmp 清理在 finally 内，失败不影响原子写主流程。"
        "另注：atomic_write_text 主路径的 fsync 未包 try/except——那是正确的，"
        "写失败必须抛出，不能被吞"),
    "agents/skill_tiers.py": (
        "已逐处判读（4 处）：元数据恢复已有 _record_tier_fault 通道、tmp 清理后接 raise；"
        "诊断自身失败降级 stderr、回退失败进返回值"),
}


def main() -> int:
    data = json.loads(BACKLOG.read_text(encoding="utf-8"))
    shutil.copyfile(BACKLOG, BACKLOG.with_suffix(".json.bak-before-triage-mark"))

    marked = 0
    for item in data["items"]:
        key = item.get("key") or ""
        # 注意：不能用 "module:silent-except:" 前缀——它匹配不到
        # "module:silent-except-return-none:..." 这类变体（曾经因此漏处理）。
        if not key.startswith("module:"):
            continue
        body = key.split("module:", 1)[1] if key.startswith("module:") else key
        rule, _, module_file = body.partition(":")
        # 先按 file#rule 精确匹配，再退化为按 file
        reason = (VERDICTS.get(f"{module_file}#{rule}")
                  or VERDICTS.get(module_file))
        if not reason:
            continue
        for occ in item.get("openOccurrences") or []:
            if occ.get("triage") == "needs-review":
                occ["triage"] = "reasonable-defense"
                occ["triageReason"] = reason[:300]
                marked += 1
        # 若全部位置都已判读，把项标为 done（不是缺陷）
        remaining = [o for o in (item.get("openOccurrences") or [])
                     if o.get("triage") == "needs-review"]
        if not remaining:
            item["status"] = "done"
            item["closedReason"] = "reasonable-defense"
            item["note"] = reason

    BACKLOG.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")

    open_items = [i for i in data["items"] if i["status"] != "done"]
    by_reason: dict[str, int] = {}
    for item in data["items"]:
        reason = item.get("closedReason")
        if reason:
            by_reason[reason] = by_reason.get(reason, 0) + 1
    print(json.dumps({
        "markedOccurrences": marked,
        "openItems": len(open_items),
        "closedReasons": by_reason,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
