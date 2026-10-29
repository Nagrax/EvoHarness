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
- 运行级不可变数据：run_id 在首题之前冻结 run manifest（题单/数据哈希/模型/配置/分母政策/
  平台不可用规则）；predictions/trace 写入 run_id 专属目录，同 tag 多次运行互不混入；
  resume 校验协议指纹（数据/题单/模型/配置/代码，变化即新 run）；
  score 从源 manifest 恢复 planned、补 not_attempted 失败并绑定原始轨迹哈希；
  旧 tag 共享产物只读兼容。
- 判分：GAIA 用官方式宽松匹配（规范化 + 数值容差 + 列表序无关）。
  HLE 按 answer_type 分 multipleChoice（单字母抽取）与 exactMatch —— 注意口径变更：
  本版 exactMatch 复用 gaia_match（规范化 + 数值归一 + 逗号序无关 + 仅 gold 侧声明的近似
  容差），比早期版本的「纯字符串精确比较」更宽松，故跨版本 pass@1 不可直接比较
  （score() 会把该提示写入 summary 的 scoring_note 字段）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
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
# gold 含近似/约等标记时才允许数值容差；否则要求精确相等。
# 中文近似词加 CJK 词界（前后不得紧邻其它汉字），避免 '纽约'、'条约' 等词内字符
# 也被当成近似标记而误触发 1% 数值容差。
_APPROX_RE = re.compile(
    r"[~≈]|\babout\b|\baround\b|\bapproximately\b|\broughly\b"
    r"|\bapprox\b|\bcirca\b"
    r"|(?<![\u4e00-\u9fff])约(?![\u4e00-\u9fff])"
    r"|(?<![\u4e00-\u9fff])大约(?![\u4e00-\u9fff])"
    r"|(?<![\u4e00-\u9fff])近似(?![\u4e00-\u9fff])"
    r"|(?<![\u4e00-\u9fff])左右(?![\u4e00-\u9fff])"
)


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

    # 运行级不可变数据：先冻结题单与协议指纹，再决定 resume 还是新建 run_id 专属产物目录。
    planned_ids = [str(it.get("task_id") or it.get("id")) for it in data]
    fingerprint = _protocol_fingerprint(
        dataset=dataset, planned_ids=planned_ids, data_sha256=_data_sha256(dataset),
        model=str(os.environ.get("MODEL") or ""), no_fold=no_fold,
        fold_threshold=fold_threshold, max_turns=max_turns, timeout_s=timeout_s,
    )
    resume_id = _find_resumable_run(out_dir, fingerprint)
    if resume_id:
        run_id = resume_id
        run_dir = out_dir / RUN_STORE_DIR / run_id
        out_path = run_dir / "predictions.jsonl"
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        # resume 绑定数据/题单/模型/配置：指纹已匹配，题单再显式核对；不一致即报错，绝不混合。
        if [str(t) for t in (manifest.get("data") or {}).get("task_ids") or []] != planned_ids:
            raise ValueError(f"[{tag}] resume 校验失败：manifest 题单与本次题单不一致 (run_id={run_id})")
        print(f"[{tag}] RESUME run_id={run_id} (protocol fingerprint {fingerprint})")
    else:
        run_id = new_run_id()
        run_dir = out_dir / RUN_STORE_DIR / run_id
        out_path = run_dir / "predictions.jsonl"
        # 首题之前冻结 run manifest 并落盘：此后即使中断，planned/题单/配置也可恢复。
        manifest = begin_run(
            run_id=run_id, dataset=dataset, tag=tag, items=data, limit=limit, workers=workers,
            max_turns=max_turns, timeout_s=timeout_s, no_fold=no_fold, fold_threshold=fold_threshold,
            output_path=out_path, planned_ids=planned_ids, protocol_fingerprint=fingerprint,
        )
        print(f"[{tag}] NEW RUN run_id={run_id} (manifest frozen before first task)")

    # 中断续跑：已写轨迹必须可解析且限于题单内，损坏/越界记录显式报错（不静默混入）。
    existing_records = _read_predictions_strict(out_path) if out_path.is_file() else []
    for r in existing_records:
        tid = str(r.get("task_id") or r.get("id") or "")
        if tid not in set(planned_ids):
            raise ValueError(f"[{tag}] resume 校验失败：轨迹含题单外记录 task_id={tid!r} (run_id={run_id})")
    done_ids = {str(r.get("task_id") or r.get("id") or "") for r in existing_records}
    todo = [item for item in data if str(item.get("task_id") or item.get("id")) not in done_ids]
    attempt_no = len(existing_records)
    print(f"[{tag}] total={len(data)}, done={len(done_ids)}, todo={len(todo)}")

    semaphore = asyncio.Semaphore(workers)
    lock = asyncio.Lock()
    counters = {"finished": 0, "input_tokens": 0, "output_tokens": 0}
    started = time.time()

    async def worker(item: dict) -> None:
        nonlocal attempt_no
        async with semaphore:
            record = await run_task(item, dataset, no_fold=no_fold, max_turns=max_turns, timeout_s=timeout_s)
            async with lock:
                attempt_no += 1
                record["run_id"] = run_id
                record["attempt_id"] = f"{run_id}-a{attempt_no:05d}"
                record["attempt"] = attempt_no
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

    # 冻结最终 manifest + 汇总：planned/题单/配置取自首题前冻结的 manifest；同名永不覆盖。
    records = _read_predictions_strict(out_path) if out_path.is_file() else []
    final = dict(manifest)
    final["status"] = "completed"
    final["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    final["predictions_sha256"] = _sha256_file(out_path) if out_path.is_file() else ""
    final["metrics"] = summarize_records(records, dataset, planned=len(data), planned_task_ids=planned_ids)
    final_name = _write_immutable(out_dir / "runs" / f"{run_id}.json", final)
    (out_dir / "latest_run.json").write_text(
        json.dumps(
            {"run_id": run_id, "manifest": f"runs/{final_name}", "run_store": f"{RUN_STORE_DIR}/{run_id}"},
            ensure_ascii=False, indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    print(f"[{tag}] RUN COMPLETE -> {out_path} (run_id={run_id})")
    print(json.dumps(final["metrics"], ensure_ascii=False, indent=2))


# ── 判分 ──────────────────────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    text = str(text).lower()
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff\s]", " ", text)
    normalized = " ".join(text.split())
    # 仅对多词文本删独立冠词；单词答案（选项字母 'A'/'a'、'the' 等）不删，避免被归一成空串。
    if len(normalized.split()) > 1:
        normalized = " ".join(re.sub(r"\b(a|an|the)\b", " ", normalized).split())
    return normalized


def _numbers(text: str) -> list[float]:
    return [
        float(match.replace(",", ""))
        for match in re.findall(r"-?\d[\d,]*\.?\d*", str(text))
    ]


def _residual(text: str) -> str:
    """去掉数字与近似修饰词后的规范化残余（单位等）。数值比较要求残余一致，防单位误配。

    先剥离 ~/about/约 等近似标记，使 'about 100' 与 '100' 的残余都归一到空串，
    让纯数值预测能与含修饰词的 gold 判等（此前残余 'about' != '' 会被误判为错）。
    """
    without_numbers = re.sub(r"-?\d[\d,]*\.?\d*", " ", str(text))
    without_approx = _APPROX_RE.sub(" ", without_numbers)
    return _normalize(without_approx)


def gaia_match(prediction: str, gold: str) -> bool:
    pred_norm, gold_norm = _normalize(prediction), _normalize(gold)
    if not pred_norm:
        return False
    if pred_norm == gold_norm:
        return True
    pred_nums, gold_nums = _numbers(prediction), _numbers(gold)
    # 仅以 gold 侧声明近似（~ / about / 约 …）才允许 1% 相对容差。
    # 若预测侧标记也启用容差，模型只要给预测加 '~' 就能放宽判分、虚高 pass@1。
    approx = bool(_APPROX_RE.search(str(gold)))
    if (
        pred_nums
        and gold_nums
        and len(pred_nums) == len(gold_nums)
        and _residual(prediction) == _residual(gold)
        and all(
            a == b or (approx and abs(a - b) <= abs(b) * 0.01)
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


# multipleChoice 选项字母抽取。只接受「作为答案本身」的字母，而不是散文里任意独立
# 大写字母（代词/罗马数字 'I'、解释里的 'A'）——后者会把 'I think the answer is B'
# 的首个独立字母 'I' 误当答案。
#   1. 显式答案标记后的字母：'ANSWER: C'、'答案C'、'The correct option is C'，
#      允许 IS/ARE 与冒号连用（'The answer is: B'）。
#   2. 紧邻分隔符或被（全角/半角）括号包裹的字母：'C.'、'D)'、'(D)'、'C）'、'（C）'
#      （负向环视排除单词内字母）。
#   3. 整段（仅含可选括号/标点）就是一个单字母：'B'、'C.'、'(D)'、'（C）'。
# 选项范围 A-Z：HLE 多选可达 9 项以上，gold 可为 I/J/L/M/V/P。
_MC_MARKED_RE = re.compile(
    r"(?:FINAL\s+ANSWER|ANSWER|答案|选项|OPTION|CHOICE|CORRECT)"
    r"\s*(?:(?:IS|ARE)\s*)?[:：]?\s*[\(（\[]?([A-Z])(?![A-Z])"
)
_MC_EXPLICIT_RE = re.compile(r"\(([A-Z])\)|（([A-Z])）|(?<![A-Z])([A-Z])(?![A-Z])[.)\]）]")
_MC_LONE_RE = re.compile(r"^\s*[\(（\[]?([A-Z])[\)）\].:：]?\s*$")


def _mc_option_letter(text: str) -> str:
    """从上（已 strip/upper）的答案文本中抽取选项字母；非「答案本身」时返回空串。"""
    for pattern in (_MC_MARKED_RE, _MC_EXPLICIT_RE):
        match = pattern.search(text)
        if match:
            for group in match.groups():
                if group:
                    return group
    lone = _MC_LONE_RE.match(text)
    return lone.group(1) if lone else ""


def hle_match(prediction: str, gold: str, answer_type: str) -> bool:
    if answer_type == "multipleChoice":
        letter = _mc_option_letter(str(prediction).strip().upper())
        return bool(letter) and letter == str(gold).strip().upper()
    # exactMatch：口径变更——由早期的「纯字符串精确比较」放宽为复用 gaia_match（规范化 +
    # 数值归一 + 逗号序无关 + 仅 gold 侧声明的近似容差），使 '42.0' vs '42'、'1,000' vs
    # '1000' 之类格式差异不再被误判为错。因判分放宽，本次口径变更前后的 pass@1 不可直接
    # 比较（score() 会把该提示写入 summary 的 scoring_note 字段）。
    return gaia_match(prediction, gold)


def _is_scorable(record: dict) -> bool:
    """是否计入「有效作答」集合：未超时、无执行错误、且给出了 Final Answer 标记。"""
    return (
        not record.get("timed_out")
        and not record.get("error")
        and bool(record.get("has_final_marker"))
    )


# ── 冻结评测协议：分母政策、平台不可用分类、bootstrap CI、run manifest ────────
#
# 口径漂移是本项目历史报告不可比的主因（README 20.2 / RESULTS 9.3 / 当前判分器三套口径）。
# 这里把「分母政策」在判分前写死，并由 run manifest 绑定代码版本/数据哈希/预算/判分器版本，
# 使同一份预测可被同一判分器复算、汇总可反查逐题记录、每个成绩指向唯一产物。

JUDGE_VERSION = "matcher:gaia_match+hle_match@2026-09-18"
# 冻结失败类别。not_attempted 只在「源 manifest 题单」口径下由 summarize_records 补齐
# （planned 中从未出现记录的题），保证分类加总恒等于 planned。
KINDS = ("correct", "incorrect", "timeout", "error", "no_answer", "not_attempted", "platform_unavailable")
RUN_MANIFEST_SCHEMA = "evoharness-run-manifest/v1"
RUN_STORE_DIR = "run_store"  # run_id 专属产物目录：<tag>/run_store/<run_id>/{manifest.json,predictions.jsonl}
# 主尝试选取规则事先冻结：同一题多次记录取首次记录；重复尝试保留但不重复计分，禁止事后挑最好。
PRIMARY_ATTEMPT_POLICY = "first_recorded"
DENOMINATOR_POLICY = (
    "pass_at_1 = correct / (planned - platform_unavailable)；超时/执行错误/无最终答案/未尝试计为失败；"
    "platform_unavailable 为预先声明、与系统能力无关，单列并从分母剔除。"
    "valid_answer_accuracy = correct / scorable 仅供对照，不与 pass_at_1 混用。"
)
# 平台不可用（与系统能力无关）单列并剔除出主指标分母；这是预先声明的排除项。
PLATFORM_UNAVAILABLE_RE = re.compile(
    r"team_model_access_denied|model_access_denied|\b403\b|quota|rate.?limit|"
    r"insufficient_balance|balance|platform unavailable|service unavailable",
    re.IGNORECASE,
)
BOOTSTRAP_SEED = 20260119
BOOTSTRAP_ITERS = 2000


def classify_record(record: dict, dataset: str) -> str:
    """把单题记录归入冻结的失败类别（优先级即声明顺序）。

    platform_unavailable → timeout → error → no_answer → correct/incorrect。
    超时/执行错误/无最终答案都属于失败，计入主指标分母；平台不可用单列后剔除。
    """
    err = str(record.get("error") or "")
    if err and PLATFORM_UNAVAILABLE_RE.search(err):
        return "platform_unavailable"
    if record.get("timed_out"):
        return "timeout"
    if err:
        return "error"
    if not record.get("has_final_marker"):
        return "no_answer"
    if dataset == "gaia":
        ok = gaia_match(record.get("prediction", ""), record.get("gold", ""))
    else:
        ok = hle_match(
            record.get("prediction", ""), record.get("gold", ""),
            str(record.get("answer_type") or ""),
        )
    return "correct" if ok else "incorrect"


def bootstrap_ci95(flags: list[bool], *, seed: int = BOOTSTRAP_SEED, iters: int = BOOTSTRAP_ITERS) -> list[float] | None:
    """0/1 序列的非参数 bootstrap 95% 置信区间（纯离线、零模型、固定种子可复算）。"""
    n = len(flags)
    if n == 0:
        return None
    rng = random.Random(seed)
    rates = [sum(1 for _ in range(n) if flags[rng.randrange(n)]) / n for _ in range(iters)]
    rates.sort()
    return [round(rates[int(0.025 * iters)] * 100, 1), round(rates[min(iters - 1, int(0.975 * iters))] * 100, 1)]


def _sha256_file(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_predictions(path: Path) -> list[dict]:
    records: list[dict] = []
    bad: list[str] = []
    line_no = 0
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                records.append(json.loads(line))
            except Exception as error:  # noqa: BLE001
                # 坏行被跳过会让评测样本静默变少（指标口径跟着变），必须计数并提示。
                bad.append(f"{type(error).__name__}@{line_no}")
    if bad:
        print(f"[benchmark] 跳过 {len(bad)} 行无法解析的记录（共 {len(bad) + len(records)} 行）")
    return records


def _read_predictions_strict(path: Path) -> list[dict]:
    """严格读取 run_id 专属轨迹：非法 JSON 显式报错（不静默丢弃），保证分母可审计。"""
    records: list[dict] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except Exception as exc:
            raise ValueError(f"{path}:{lineno}: 非法 JSON 记录：{exc}") from exc
    return records


def _read_latest_run_id(tag_dir: Path) -> str:
    ptr = tag_dir / "latest_run.json"
    if not ptr.is_file():
        return ""
    try:
        return str(json.loads(ptr.read_text(encoding="utf-8")).get("run_id") or "")
    except Exception:
        return ""


def _unique_path(path: Path) -> Path:
    """返回一个尚不存在的路径（同名冲突时加序号后缀），供「先定名再写」的不可变产物使用。"""
    target = path
    i = 1
    while target.exists():
        target = path.with_name(f"{path.stem}.{i}{path.suffix}")
        i += 1
    return target


def _write_immutable(path: Path, value: dict) -> str:
    """永不覆盖：同名已存在时改用带序号的新名，返回实际写入文件名。"""
    target = _unique_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target.name


def _git_rev() -> str:
    import subprocess

    try:
        p = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, timeout=15,
        )
        # 显式按 UTF-8 解码：Windows 默认 cp936 下 git 输出若含 CJK，text=True 会在 reader
        # 线程里 UnicodeDecodeError 并把 stdout 置 None（静默退化为 unknown）。
        return (p.stdout or b"").decode("utf-8", errors="replace").strip() or "unknown"
    except Exception:
        return "unknown"


def _dirty_diff_sha256() -> str:
    import hashlib
    import subprocess

    try:
        p = subprocess.run(
            ["git", "-C", str(ROOT), "diff", "HEAD"], capture_output=True, timeout=30
        )
        # 直接哈希原始字节：与任何文本编码无关，确定性最强。
        return hashlib.sha256(p.stdout or b"").hexdigest()[:16]
    except Exception:
        return "unknown"


def _question_ids_hash(items: list[dict]) -> str:
    import hashlib

    ids = sorted(str(it.get("task_id") or it.get("id")) for it in items)
    return hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()[:16]


def _data_sha256(dataset: str) -> str:
    return _sha256_file(DATA_FILES[dataset]) if DATA_FILES[dataset].is_file() else "missing"


def new_run_id() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + os.urandom(3).hex()


def build_run_manifest(*, run_id, dataset, tag, items, limit, workers, max_turns, timeout_s,
                       no_fold, fold_threshold, output_path: Path) -> dict:
    """冻结的运行清单：代码版本/工作树差异/模型/权限/数据哈希/题单/预算/判分器版本/原始产物。"""
    return {
        "run_id": run_id,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "dataset": dataset,
        "tag": tag,
        "code": {"git_rev": _git_rev(), "worktree_diff_sha256_16": _dirty_diff_sha256()},
        "model": str(os.environ.get("MODEL") or ""),
        "permissions": {"permission_mode": "bypassPermissions"},
        "fold": {"no_fold": bool(no_fold), "fold_threshold": fold_threshold},
        "budget": {
            "limit": limit, "workers": workers, "max_turns": max_turns,
            "timeout_s": timeout_s, "planned": len(items),
        },
        "data": {
            "path": str(DATA_FILES[dataset]),
            "sha256": _data_sha256(dataset),
            "question_ids_sha256_16": _question_ids_hash(items),
        },
        "judge_version": JUDGE_VERSION,
        "raw_artifact": str(output_path),
    }


def _protocol_fingerprint(*, dataset: str, planned_ids: list[str], data_sha256: str, model: str,
                          no_fold: bool, fold_threshold: float | None, max_turns: int, timeout_s: int) -> str:
    """协议指纹：resume 只接受相同 数据/题单/模型/配置/代码 的 run；任一变化即新 run。"""
    import hashlib

    payload = {
        "schema": "evoharness-run-protocol/v1",
        "dataset": dataset,
        "task_ids": sorted(planned_ids),
        "data_sha256": data_sha256,
        "model": model,
        "permission_mode": "bypassPermissions",
        "no_fold": bool(no_fold),
        "fold_threshold": fold_threshold,
        "max_turns": max_turns,
        "timeout_s": timeout_s,
        "judge_version": JUDGE_VERSION,
        "code_git_rev": _git_rev(),
        "code_worktree_diff_sha256_16": _dirty_diff_sha256(),
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _find_resumable_run(out_dir: Path, fingerprint: str) -> str:
    """找同 tag 下可续跑的未完成 run：状态 running、协议指纹一致、且尚未产出最终 manifest。

    已完成的 run 不可续跑：同配置重跑 = 新 run，各 run 产物互不混入。
    """
    store = out_dir / RUN_STORE_DIR
    if not store.is_dir():
        return ""
    candidates: list[tuple[str, str]] = []
    for d in sorted(store.iterdir()):
        if not d.is_dir():
            continue
        mf = d / "manifest.json"
        if not mf.is_file():
            continue
        try:
            m = json.loads(mf.read_text(encoding="utf-8"))
        except Exception:
            continue
        if m.get("status") != "running":
            continue
        if m.get("protocol_fingerprint") != fingerprint:
            continue
        if (out_dir / "runs" / f"{d.name}.json").exists():
            continue
        candidates.append((str(m.get("created_at") or ""), d.name))
    return max(candidates)[1] if candidates else ""


def begin_run(*, run_id, dataset, tag, items, limit, workers, max_turns, timeout_s,
              no_fold, fold_threshold, output_path: Path,
              planned_ids: list[str], protocol_fingerprint: str) -> dict:
    """首题之前冻结 run manifest 并建立 run_id 专属产物目录（predictions/trace 与其他 run 隔离）。"""
    manifest = build_run_manifest(
        run_id=run_id, dataset=dataset, tag=tag, items=items, limit=limit, workers=workers,
        max_turns=max_turns, timeout_s=timeout_s, no_fold=no_fold, fold_threshold=fold_threshold,
        output_path=output_path,
    )
    manifest["schema_version"] = RUN_MANIFEST_SCHEMA
    manifest["status"] = "running"
    manifest["data"]["task_ids"] = [str(t) for t in planned_ids]
    manifest["protocol_fingerprint"] = protocol_fingerprint
    manifest["primary_attempt_policy"] = PRIMARY_ATTEMPT_POLICY
    manifest["denominator_policy"] = DENOMINATOR_POLICY
    manifest["platform_unavailable_rule"] = {
        "regex": PLATFORM_UNAVAILABLE_RE.pattern,
        "flags": "IGNORECASE",
        "policy": "命中规则的错误单列 platform_unavailable 并从主指标分母剔除；逐题证据见汇总 platform_unavailable_tasks。",
    }
    run_dir = output_path.parent
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def _resolve_source_run(tag_dir: Path) -> tuple[str, dict | None]:
    """定位最新可评分 run：latest_run.json 指针优先，其次扫描 run_store；无则返回 ('', None)（legacy）。"""
    source_run_id = _read_latest_run_id(tag_dir)
    store = tag_dir / RUN_STORE_DIR
    if source_run_id:
        mf = store / source_run_id / "manifest.json"
        if mf.is_file():
            try:
                return source_run_id, json.loads(mf.read_text(encoding="utf-8"))
            except Exception as exc:
                raise ValueError(f"{mf}: 源 manifest 损坏：{exc}") from exc
    if store.is_dir():
        candidates: list[tuple[str, str]] = []
        for d in sorted(store.iterdir()):
            mf = d / "manifest.json"
            if not (d.is_dir() and mf.is_file()):
                continue
            try:
                m = json.loads(mf.read_text(encoding="utf-8"))
            except Exception:
                continue
            candidates.append((str(m.get("created_at") or ""), d.name))
        if candidates:
            rid = max(candidates)[1]
            try:
                return rid, json.loads((store / rid / "manifest.json").read_text(encoding="utf-8"))
            except Exception as exc:
                raise ValueError(f"{store / rid / 'manifest.json'}: 源 manifest 损坏：{exc}") from exc
    return "", None


def summarize_records(records: list[dict], dataset: str, *, planned: int | None = None,
                      planned_task_ids: list[str] | None = None) -> dict:
    """冻结分母政策下的汇总（纯函数，零 IO）。

    主指标 pass_at_1 = correct / (planned - platform_unavailable)：超时/执行错误/无最终答案/
    未尝试均计为失败。valid_answer_accuracy = correct / scorable，仅供对照，与 pass@1 分开命名。

    两条口径：
    - 源 manifest 题单（planned_task_ids）：planned 与分母只来自 manifest，不得用 attempted
      重定义计划题数；题单外记录、题单内重复 task_id 显式报错；同一题多次尝试按
      first_recorded 取主尝试，重复保留但不重复计分；planned 中无记录的题补 not_attempted
      失败，分类加总恒等于 planned；CI 重采样与点估计同一分母（未尝试按失败参与重采样）。
    - 旧产物兼容（无题单）：planned 缺省取传入 planned 或记录数，行为与旧版一致。
    """
    task_categories: dict[str, str] = {}
    duplicate_attempts = 0
    if planned_task_ids is not None:
        planned_ids = [str(t) for t in planned_task_ids]
        if len(set(planned_ids)) != len(planned_ids):
            raise ValueError("manifest 题单含重复 task_id：分母不可定义，拒绝评分")
        planned_n = len(planned_ids)
        planned_set = set(planned_ids)
        primaries: dict[str, dict] = {}
        for r in records:
            tid = str(r.get("task_id") or r.get("id") or "")
            if tid not in planned_set:
                raise ValueError(f"记录 task_id={tid!r} 不在源 manifest 题单中：拒绝评分")
            if tid in primaries:
                duplicate_attempts += 1  # 重复尝试：保留原始记录，但不重复计分
            else:
                primaries[tid] = r
        primary_records = list(primaries.values())
        not_attempted_ids = [t for t in planned_ids if t not in primaries]
        kinds = [classify_record(r, dataset) for r in primary_records]
        kinds += ["not_attempted"] * len(not_attempted_ids)
        task_categories = {t: classify_record(r, dataset) for t, r in primaries.items()}
        task_categories.update({t: "not_attempted" for t in not_attempted_ids})
        attempted_n = len(primary_records)
    else:
        planned_n = int(planned) if planned is not None else len(records)
        kinds = [classify_record(r, dataset) for r in records]
        primary_records = records
        not_attempted_ids = []
        attempted_n = len(records)

    cnt = {k: kinds.count(k) for k in KINDS}
    pu = cnt["platform_unavailable"]
    effective = max(0, planned_n - pu)
    correct = cnt["correct"]
    scorable = cnt["correct"] + cnt["incorrect"]

    def pct(num: int, den: int) -> float:
        return round(num / den * 100, 1) if den else 0.0

    out: dict = {
        "planned": planned_n,
        "attempted": attempted_n,
        "attempts_total": len(records),
        "duplicate_attempts": duplicate_attempts,
        "platform_unavailable": pu,
        "platform_unavailable_tasks": [
            {
                "task_id": str(r.get("task_id") or r.get("id") or ""),
                "error": str(r.get("error") or "")[:300],
            }
            for r in primary_records
            if classify_record(r, dataset) == "platform_unavailable"
        ],
        "effective_denominator": effective,
        "scorable": scorable,
        "correct": correct,
        "count": len(records),
        "timed_out": cnt["timeout"],
        "errored": cnt["error"],
        "no_answer": cnt["no_answer"],
        "no_final_marker": cnt["no_answer"],
        "not_attempted": cnt["not_attempted"],
        "category_sum": sum(cnt.values()),
        "pass_at_1": pct(correct, effective),
        "valid_answer_accuracy": pct(correct, scorable),
        # CI 重采样与点估计同一分母：not_attempted 作为失败参与重采样（platform_unavailable 剔除）。
        "pass_at_1_ci95": bootstrap_ci95([k == "correct" for k in kinds if k != "platform_unavailable"]),
        "valid_answer_accuracy_ci95": bootstrap_ci95(
            [k == "correct" for k in kinds if k in ("correct", "incorrect")]
        ),
        "judge_version": JUDGE_VERSION,
        "seed": BOOTSTRAP_SEED,
        "denominator_policy": DENOMINATOR_POLICY,
        "primary_attempt_policy": PRIMARY_ATTEMPT_POLICY if planned_task_ids is not None else "n/a(legacy)",
    }
    if planned_task_ids is not None:
        out["task_categories"] = task_categories  # 唯一产物反查每题：task_id → 失败类别
    return out


def _breakdown(records: list[dict], field: str, dataset: str) -> dict:
    """按字段分层（口径：仅在有效作答集合 correct/incorrect 上统计）。"""
    out: dict[str, dict] = {}
    for key in sorted({str(r.get(field) or "?") for r in records}):
        valid = [
            r for r in records
            if str(r.get(field) or "?") == key and classify_record(r, dataset) in ("correct", "incorrect")
        ]
        out[key] = {
            "count": len(valid),
            "pass": round(sum(1 for r in valid if classify_record(r, dataset) == "correct") / len(valid) * 100, 1) if valid else 0.0,
        }
    return out


def judge_calibration(pairs_path) -> dict:
    """judge 校准的显式 opt-in 入口（默认不调用、纯离线，零模型）。

    从 golden pairs JSON（schema 见 eval/judge_calibration/golden_pairs.example.json）
    读取成对判定并计算 Cohen's kappa；若数据里另含 swap / records 字段则一并计算。
    返回可直接并入 report 的字段。仅在 `score --judge-calibration <path>` 显式传入时调用，
    默认路径下不产生该字段，因此不改变既有 report/默认行为。
    """
    from eval.judge_calibration import (
        cohen_kappa,
        length_controlled_diff,
        swap_consistency,
    )

    data = json.loads(Path(pairs_path).read_text(encoding="utf-8"))
    pairs = [(p.get("label_a"), p.get("label_b")) for p in data.get("pairs", [])]
    out: dict = {"source": str(pairs_path), "n_pairs": len(pairs), "kappa": cohen_kappa(pairs)}
    swap = data.get("swap")
    if swap:
        out["swap"] = swap_consistency(swap.get("ordered_a", []), swap.get("ordered_b", []))
    if data.get("records"):
        out["length_controlled"] = length_controlled_diff(data["records"])
    return out


def score(dataset: str, *, judge_calibration_path: str | None = None) -> None:
    for tag in ([f"{dataset}", f"{dataset}-nofold"] if dataset == "gaia" else [f"{dataset}"]):
        tag_dir = RUNS_DIR / tag
        source_run_id, manifest = _resolve_source_run(tag_dir)
        if manifest is not None:
            # 新路径：run_id 专属原始轨迹 + 首题前冻结的 manifest（planned 与题单来自源 manifest）。
            out_path = tag_dir / RUN_STORE_DIR / source_run_id / "predictions.jsonl"
            records = _read_predictions_strict(out_path) if out_path.is_file() else []
        else:
            # 旧 tag 产物只读兼容：无 run_store 时按旧共享文件评分（宽松读取），不改写原始文件。
            out_path = tag_dir / "predictions.jsonl"
            records = _read_predictions(out_path) if out_path.is_file() else []
            if not records:
                continue
        planned_ids = [str(t) for t in manifest["data"]["task_ids"]] if manifest else None
        summary = summarize_records(
            records, dataset,
            planned=(len(planned_ids) if planned_ids is not None else None),
            planned_task_ids=planned_ids,
        )
        summary["tag"] = tag
        summary["planned_source"] = "run_manifest" if manifest else "records_fallback"
        summary["avg_folds"] = round(sum(r.get("folds", 0) for r in records) / len(records), 2) if records else 0.0
        summary["tokens_M"] = round(
            sum(
                int((r.get("tokens") or {}).get("input", 0)) + int((r.get("tokens") or {}).get("output", 0))
                for r in records
            ) / 1e6,
            1,
        )
        summary["by_problem_type"] = _breakdown(records, "problem_type", dataset)
        if dataset == "hle":
            # 报告说明：标注 exactMatch 口径变更，提醒跨版本 pass@1 不可直接比较。
            summary["scoring_note"] = (
                "HLE exactMatch 复用 gaia_match 的宽松归一（数值归一/逗号序无关/gold 侧近似容差），"
                "较早期版本纯字符串精确比较更宽松；口径变更前后的 pass@1 不可直接比较。"
            )
        if dataset == "gaia":
            summary["by_level"] = _breakdown(records, "level", dataset)
        if judge_calibration_path:
            # opt-in：仅在显式传入校准数据时写入该字段；默认 None 时不写、不改口径。
            summary["judge_calibration"] = judge_calibration(judge_calibration_path)

        # 不可变派生产物：绑定原 run、源 manifest、原始轨迹哈希与判分器版本；同名永不覆盖。
        # summary.json 只作为「最新派生物」指针，原始预测轨迹与 manifest 不被改写。
        # 先定唯一文件名再写：provenance.score_run_id 必须与实际产物文件名严格一致。
        scores_dir = tag_dir / "scores"
        scores_dir.mkdir(parents=True, exist_ok=True)
        score_path = _unique_path(scores_dir / f"{new_run_id()}.json")
        provenance = {
            "score_run_id": score_path.stem,
            "source_run_id": source_run_id,
            "predictions_path": str(out_path),
            "predictions_sha256": _sha256_file(out_path) if out_path.is_file() else "",
            "judge_version": JUDGE_VERSION,
            "scored_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "predictions_path_exists": out_path.is_file(),
        }
        if manifest is not None:
            mf_path = tag_dir / RUN_STORE_DIR / source_run_id / "manifest.json"
            recorded = str(manifest.get("predictions_sha256") or "")
            provenance["source_manifest_path"] = str(mf_path)
            provenance["source_manifest_sha256"] = _sha256_file(mf_path)
            provenance["manifest_predictions_sha256"] = recorded
            provenance["trace_hash_matches_manifest"] = bool(recorded) and recorded == provenance["predictions_sha256"]
            provenance["primary_attempt_policy"] = str(manifest.get("primary_attempt_policy") or PRIMARY_ATTEMPT_POLICY)
            provenance["protocol_fingerprint"] = str(manifest.get("protocol_fingerprint") or "")
        immutable = dict(summary)
        immutable["provenance"] = provenance
        score_path.write_text(
            json.dumps(immutable, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        summary["provenance"] = provenance
        (tag_dir / "summary.json").write_text(
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
    parser.add_argument("--judge-calibration", default=None,
                        help="judge 校准 opt-in：golden pairs JSON 路径，仅 score 时写入 report")
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
        score(args.dataset, judge_calibration_path=args.judge_calibration)


if __name__ == "__main__":
    main()
