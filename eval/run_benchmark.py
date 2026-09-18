"""GAIA / HLE 基准评测 runner + 判分器。

用法：
    python -m eval.run_benchmark run  --dataset gaia --limit 20            # 冒烟
    python -m eval.run_benchmark run  --dataset gaia                        # 全量（断点续跑）
    python -m eval.run_benchmark run  --dataset gaia --no-fold              # 关闭记忆折叠消融
    python -m eval.run_benchmark score --dataset gaia                       # 对已有预测打分

设计要点：
- 逐题落盘 JSONL，重跑自动跳过已完成题目（断点续跑）。
- 每题一个全新 Agent 会话；permission_mode=bypassPermissions（评测口径 = harness 完整能力）。
- --no-fold 通过三个 monkeypatch 实现，不改生产代码：
  AUTO_COMPACT_THRESHOLD→99（自动折叠永不触发）、移除 compact_context 工具、
  折叠引导段置空。上下文自然增长直到模型窗口极限——这是"折叠 off"的诚实条件。
- 判分：GAIA 用官方式宽松匹配（规范化 + 数值容差 + 列表序无关），
  HLE 按 answer_type 分 exactMatch（规范化精确）与 multipleChoice（单字母）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=False)

from agents.agent import Agent
from agents.main import _resolve_api_config

DATA_FILES = {
    "gaia": ROOT / "data" / "GAIA" / "all.json",
    "hle": ROOT / "data" / "HLE" / "all_500.json",
}
RUNS_DIR = ROOT / "eval" / "runs"
SCRATCH_DIR = RUNS_DIR / "scratch"

FINAL_ANSWER_RE = re.compile(r"[Ff]inal\s*[Aa]nswer\s*[:：]\s*(.+)")


# ── 提示构建 ──────────────────────────────────────────────────────────────────

ANSWER_INSTRUCTION = (
    "\n\n完成后，在回复的最后单独一行给出最终答案，格式严格为：Final Answer: <简短答案>。\n"
    "答案必须简短（数字、名称、日期或短语），除非任务明确要求，不要写完整句子。"
)


def build_gaia_prompt(item: dict) -> str:
    file_line = ""
    file_name = str(item.get("file_name") or "").strip()
    if file_name:
        file_path = ROOT / "data" / "GAIA" / "files" / file_name
        if file_path.exists():
            file_line = f"\n\n任务附带文件（可用工具读取）: {file_path}"
    return (
        "请解决以下任务。你可以使用工具（读取文件、运行 shell、浏览器、搜索等）收集信息并推理。"
        f"如需创建临时文件，请在 {SCRATCH_DIR} 下操作。\n\n{item['Question']}{file_line}"
        + ANSWER_INSTRUCTION
    )


def build_hle_prompt(item: dict) -> str:
    image_line = ""
    image = str(item.get("image") or "").strip()
    if image:
        image_path = ROOT / "data" / "HLE" / "images" / image
        if image_path.exists():
            image_line = f"\n\n任务附带图片（可用工具读取）: {image_path}"
    mc_hint = ""
    if item.get("answer_type") == "multipleChoice":
        mc_hint = "\n这是一道多选题，最终答案只给选项字母（如 Final Answer: B）。"
    return (
        "请解决以下高难度推理任务。可以使用工具辅助计算和查证。"
        f"如需创建临时文件，请在 {SCRATCH_DIR} 下操作。\n\n{item['question']}{image_line}{mc_hint}"
        + ANSWER_INSTRUCTION
    )


# ── 折叠开关 ──────────────────────────────────────────────────────────────────

_no_fold_patched = False
_fold_threshold_override: float | None = None


def apply_no_fold_globally() -> None:
    """关闭记忆折叠：自动阈值失效 + 移除 compact_context 工具 + 引导段置空。"""
    global _no_fold_patched
    if _no_fold_patched:
        return
    import agents.agent as agent_mod

    agent_mod.AUTO_COMPACT_THRESHOLD = 99.0
    agent_mod.Agent._build_fold_guidance_section = lambda self: ""
    _no_fold_patched = True


def apply_fold_threshold(threshold: float) -> None:
    """强制折叠频率：把自动折叠阈值调到指定比例（消融用，如 0.30=38k 即折）。"""
    global _fold_threshold_override
    import agents.agent as agent_mod

    agent_mod.AUTO_COMPACT_THRESHOLD = threshold
    _fold_threshold_override = threshold


def strip_fold_tool(agent: Agent) -> None:
    agent.tools = [t for t in agent.tools if t.get("name") != "compact_context"]


# ── 单题执行 ──────────────────────────────────────────────────────────────────

def extract_final_answer(text: str) -> str:
    matches = FINAL_ANSWER_RE.findall(text or "")
    if matches:
        return matches[-1].strip()
    return (text or "").strip()[-300:]


async def run_task(item: dict, dataset: str, *, no_fold: bool, max_turns: int, timeout_s: int) -> dict:
    base, key, use_openai = _resolve_api_config(None)

    # 在独立线程跑任务：run_shell 是同步 subprocess.run，挂死时会冻结事件循环，
    # asyncio.wait_for 无法取消同步阻塞。线程 + 轮询 + 超时即放弃，是唯一可靠看门狗。
    loop = asyncio.get_running_loop()
    holder: dict = {}

    def _work() -> None:
        agent = None
        try:
            agent = Agent(
                permission_mode="bypassPermissions",
                model=os.environ.get("MODEL"),
                max_turns=max_turns,
                api_base=base if use_openai else None,
                anthropic_base_url=base if not use_openai else None,
                api_key=key,
            )
            if no_fold:
                strip_fold_tool(agent)
            prompt = build_gaia_prompt(item) if dataset == "gaia" else build_hle_prompt(item)
            result = asyncio.run(agent.run_once(prompt))
            holder["text"] = str(result.get("text") or "")
            holder["agent"] = agent
        except Exception as exc:  # noqa: BLE001
            holder["error"] = str(exc)[:300]
        finally:
            # 每题一个 Agent 会各自连 3 个 MCP server（npx 子进程树）。
            # 不清理的话 4 workers × 165 题会累积数百个僵尸进程，耗尽系统资源。
            if agent is not None:
                for conn in list(getattr(agent._mcp_manager, "_connections", {}).values()):
                    try:
                        conn.close()
                    except Exception:
                        pass

    import threading

    thread = threading.Thread(target=_work, daemon=True)
    t0 = time.time()
    thread.start()
    while thread.is_alive() and time.time() - t0 < timeout_s:
        await asyncio.sleep(5)
    timed_out = thread.is_alive()
    error = str(holder.get("error") or "")
    text = str(holder.get("text") or "")
    agent = holder.get("agent")

    if timed_out:
        # daemon 线程无法安全强杀；记录并放弃该线程，继续后续任务。
        error = error or f"watchdog: task exceeded {timeout_s}s, abandoned"

    prediction = extract_final_answer(text)
    return {
        "task_id": str(item.get("task_id") or item.get("id")),
        "question": str(item.get("Question") or item.get("question"))[:500],
        "gold": str(item.get("answer")),
        "prediction": prediction,
        "has_final_marker": bool(FINAL_ANSWER_RE.findall(text or "")),
        "timed_out": timed_out,
        "error": error,
        "folds": getattr(agent, "_fold_count", 0) if agent else 0,
        "tokens": {
            "input": getattr(agent, "total_input_tokens", 0) if agent else 0,
            "output": getattr(agent, "total_output_tokens", 0) if agent else 0,
        },
        "latency_s": round(time.time() - t0, 1),
        "problem_type": str(item.get("problem_type") or ""),
        "level": item.get("Level"),
        "answer_type": str(item.get("answer_type") or ""),
    }


# ── 主运行循环 ────────────────────────────────────────────────────────────────

async def run(dataset: str, *, limit: int | None, workers: int, no_fold: bool, max_turns: int, timeout_s: int, fold_threshold: float | None) -> None:
    if no_fold:
        apply_no_fold_globally()
    elif fold_threshold is not None:
        apply_fold_threshold(fold_threshold)

    data = json.loads(DATA_FILES[dataset].read_text(encoding="utf-8"))
    if limit:
        data = data[:limit]

    tag = f"{dataset}{'-nofold' if no_fold else ''}"
    if fold_threshold is not None and not no_fold:
        tag += f"-fold{str(fold_threshold).replace('.', '')}"
    out_dir = RUNS_DIR / tag
    out_dir.mkdir(parents=True, exist_ok=True)
    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "predictions.jsonl"

    done_ids = set()
    if out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    done_ids.add(json.loads(line)["task_id"])
                except Exception:
                    pass
    todo = [item for item in data if str(item.get("task_id") or item.get("id")) not in done_ids]
    print(f"[{tag}] total={len(data)}, done={len(done_ids)}, todo={len(todo)}")

    semaphore = asyncio.Semaphore(workers)
    lock = asyncio.Lock()
    counters = {"finished": 0, "input_tokens": 0, "output_tokens": 0}
    started = time.time()

    async def worker(item: dict) -> None:
        async with semaphore:
            record = await run_task(item, dataset, no_fold=no_fold, max_turns=max_turns, timeout_s=timeout_s)
            async with lock:
                with out_path.open("a", encoding="utf-8") as file:
                    file.write(json.dumps(record, ensure_ascii=False) + "\n")
                counters["finished"] += 1
                counters["input_tokens"] += record["tokens"]["input"]
                counters["output_tokens"] += record["tokens"]["output"]
                finished = counters["finished"]
                if finished % 5 == 0 or finished == len(todo):
                    elapsed = time.time() - started
                    avg_in = counters["input_tokens"] / finished
                    print(
                        f"[{tag}] {finished}/{len(todo)} "
                        f"({elapsed/60:.0f}min, avg {avg_in/1000:.0f}k in/q, "
                        f"{counters['input_tokens']/1e6:.1f}M in total)",
                        flush=True,
                    )

    if todo:
        await asyncio.gather(*(worker(item) for item in todo))
    print(f"[{tag}] RUN COMPLETE -> {out_path}")


# ── 判分 ──────────────────────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    text = str(text).lower()
    text = re.sub(r"\b(a|an|the)\b", " ", text)
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff\s]", " ", text)
    return " ".join(text.split())


def _numbers(text: str) -> list[float]:
    return [
        float(match.replace(",", ""))
        for match in re.findall(r"-?\d[\d,]*\.?\d*", str(text))
    ]


def _residual(text: str) -> str:
    """去掉数字后的规范化残余（单位/修饰词）。数值容差要求残余一致，防单位误配。"""
    return _normalize(re.sub(r"-?\d[\d,]*\.?\d*", " ", str(text)))


def gaia_match(prediction: str, gold: str) -> bool:
    pred_norm, gold_norm = _normalize(prediction), _normalize(gold)
    if not pred_norm:
        return False
    if pred_norm == gold_norm:
        return True
    pred_nums, gold_nums = _numbers(prediction), _numbers(gold)
    if (
        pred_nums
        and gold_nums
        and len(pred_nums) == len(gold_nums)
        and _residual(prediction) == _residual(gold)
        and all(
            abs(a - b) <= max(0.01, abs(b) * 0.01)
            for a, b in zip(pred_nums, gold_nums)
        )
    ):
        return True
    if "," in str(gold):
        pred_items = sorted(_normalize(x) for x in str(prediction).split(","))
        gold_items = sorted(_normalize(x) for x in str(gold).split(","))
        if len(pred_items) == len(gold_items) and pred_items == gold_items:
            return True
    return False


def hle_match(prediction: str, gold: str, answer_type: str) -> bool:
    if answer_type == "multipleChoice":
        letters = re.findall(r"\b([A-H])\b", str(prediction).strip().upper())
        return bool(letters) and letters[-1] == str(gold).strip().upper()
    return _normalize(prediction) == _normalize(gold)


def score(dataset: str) -> None:
    for tag in ([f"{dataset}", f"{dataset}-nofold"] if dataset == "gaia" else [f"{dataset}"]):
        out_path = RUNS_DIR / tag / "predictions.jsonl"
        if not out_path.exists():
            continue
        records = [
            json.loads(line)
            for line in out_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if not records:
            continue
        matched = lambda r: (  # noqa: E731
            gaia_match(r["prediction"], r["gold"])
            if dataset == "gaia"
            else hle_match(r["prediction"], r["gold"], r.get("answer_type", ""))
        )
        correct = [r for r in records if matched(r)]
        summary = {
            "tag": tag,
            "count": len(records),
            "pass_at_1": round(len(correct) / len(records) * 100, 1),
            "avg_folds": round(sum(r.get("folds", 0) for r in records) / len(records), 2),
            "timed_out": sum(1 for r in records if r.get("timed_out")),
            "no_final_marker": sum(1 for r in records if not r.get("has_final_marker")),
            "tokens_M": round(
                sum(r["tokens"]["input"] + r["tokens"]["output"] for r in records) / 1e6, 1
            ),
            "by_problem_type": {},
        }
        for ptype in sorted({r.get("problem_type") or "?" for r in records}):
            subset = [r for r in records if (r.get("problem_type") or "?") == ptype]
            summary["by_problem_type"][ptype] = {
                "count": len(subset),
                "pass": round(sum(1 for r in subset if matched(r)) / len(subset) * 100, 1),
            }
        if dataset == "gaia":
            summary["by_level"] = {}
            for level in sorted({r.get("level") for r in records if r.get("level")}):
                subset = [r for r in records if r.get("level") == level]
                summary["by_level"][str(level)] = {
                    "count": len(subset),
                    "pass": round(sum(1 for r in subset if matched(r)) / len(subset) * 100, 1),
                }
        (RUNS_DIR / tag / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["run", "score"])
    parser.add_argument("--dataset", choices=["gaia", "hle"], required=True)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--no-fold", action="store_true")
    parser.add_argument("--fold-threshold", type=float, default=None,
                        help="override AUTO_COMPACT_THRESHOLD (e.g. 0.30), ablation only")
    parser.add_argument("--max-turns", type=int, default=40)
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()

    if args.command == "run":
        asyncio.run(
            run(
                args.dataset,
                limit=args.limit,
                workers=args.workers,
                no_fold=args.no_fold,
                max_turns=args.max_turns,
                timeout_s=args.timeout,
                fold_threshold=args.fold_threshold,
            )
        )
    else:
        score(args.dataset)


if __name__ == "__main__":
    main()
