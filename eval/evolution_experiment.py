"""E-04 纵向进化 / 泛化 / 成本实验：冻结协议 + 可离线复算的骨架。

研究问题（对齐 `RSI论文规划.md` §P0-B）：经验积累后，**未见任务**上的表现是否提高？提高来自
Skill 内容、检索策略，还是额外计算？"完成四阶段代码"不能替代该实验。

冻结协议：
- 任务家族划分：任务流池（进化+开发，每轮暴露给系统）与**固定 holdout 集**不相交；两集之间
  做近重复防泄漏（规范化哈希 + 3-gram Jaccard），避免"进步"其实是过拟合/泄漏。
- 三组核心对照（缺一不可）：`no_evolution`（skills 冻结在起点）/ `ungated_evolution`
  （无门控沉淀，什么都进库）/ `gated_evolution`（完整门控）。固定经验可作补充，不替代无门控对照。
- 度量：每轮结束在固定 holdout 上测 pass@1（沿用 `eval/run_benchmark.py` 冻结分母政策）+
  检索命中率 + 规则通过率；记录每题成本、耗时与失败类型。
- 规模：先 3—5 轮管线试验估计方差与单题成本，再落实 10—15 轮 + 检查点、正式样本量与预算。
- 诚实标注：若用规则化模拟器生成"用户反馈"，报告中必须标注「模拟用户反馈」（limitation，不是遮掩）。

本模块把**离线可算的部分**（划分 / 计划 / 曲线 / CI）做成纯函数，零模型、可单测；真实模型运行
部分需 API 预算，未配置时明确报 `blocked`，绝不产出伪造结果——"预算耗尽的实验列未完成"。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 三种情况要分开：可选依赖缺失（静默）/ 文件不存在（常态，静默）/
# 文件存在但解析失败（用户配了却没生效 → 必须可见）。
try:
    from dotenv import load_dotenv
except ImportError:  # dotenv 是可选依赖
    pass
else:
    _env_file = ROOT / ".env"
    if _env_file.is_file():
        try:
            load_dotenv(_env_file, override=False)
        except Exception as error:  # noqa: BLE001
            print(f"[evolution] .env load failed ({_env_file}): "
                  f"{type(error).__name__}: {error}")

from eval.run_benchmark import DATA_FILES, JUDGE_VERSION, bootstrap_ci95  # noqa: E402

ARMS = ("no_evolution", "ungated_evolution", "gated_evolution")
DEFAULT_DUP_THRESHOLD = 0.8
RESULTS_DIR = ROOT / "eval" / "runs" / "evolution"

def _batch_tag(ws_root: Path) -> str:
    """从 workspace 目录名派生批次标识（稳定、可读、不含路径分隔符）。"""
    name = ws_root.name or "default"
    clean = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in name).strip("-")
    return clean or "default"



# ── 任务家族划分（含近重复防泄漏） ──────────────────────────────────────────

def normalize_text(text: str) -> str:
    text = str(text or "").lower()
    return " ".join(re.sub(r"[^a-z0-9\u4e00-\u9fff\s]", " ", text).split())


def _ngrams(text: str, n: int = 3) -> set[str]:
    norm = normalize_text(text)
    if len(norm) < n:
        return {norm} if norm else set()
    return {norm[i:i + n] for i in range(len(norm) - n + 1)}


def jaccard(a: str, b: str, n: int = 3) -> float:
    ga, gb = _ngrams(a, n), _ngrams(b, n)
    if not ga and not gb:
        return 1.0
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def is_near_duplicate(a: str, b: str, threshold: float = DEFAULT_DUP_THRESHOLD) -> bool:
    if normalize_text(a) == normalize_text(b):
        return True
    return jaccard(a, b) >= threshold


def _question_of(item: dict) -> str:
    return str(item.get("Question") or item.get("question") or "")


def split_task_stream(
    items: list[dict], *, holdout_size: int, seed: int = 20260119, dup_threshold: float = DEFAULT_DUP_THRESHOLD
) -> dict[str, list[dict]]:
    """把题目切成互不相交、且无近重复的 (stream, holdout)。

    stream 是进化/开发任务流（每轮暴露给系统），holdout 是全程固定、不参与进化的考卷。
    去重规则：先按规范化哈希去重；再贪心选 holdout，拒绝与已选 holdout 近重复的题目；
    最后从 stream 中剔除与任一 holdout 近重复的题目（防泄漏）。
    """
    idx = list(range(len(items)))
    random.Random(seed).shuffle(idx)
    seen: set[str] = set()
    candidates: list[dict] = []
    for i in idx:
        key = hashlib.sha256(normalize_text(_question_of(items[i])).encode("utf-8")).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        candidates.append(items[i])

    holdout: list[dict] = []
    for item in candidates:
        if len(holdout) >= holdout_size:
            break
        if any(is_near_duplicate(_question_of(item), _question_of(h), dup_threshold) for h in holdout):
            continue
        holdout.append(item)

    holdout_q = [_question_of(h) for h in holdout]
    stream = [
        it for it in candidates
        if it not in holdout and not any(is_near_duplicate(_question_of(it), q, dup_threshold) for q in holdout_q)
    ]
    return {"stream": stream, "holdout": holdout}


# ── 任务家族化（E-04 修订）：同族重复结构是「可复用经验」成立的前提 ─────────────
#
# 首轮 pilot 证据：一次性、互不相关的任务 + 泛化反馈 → 抽取器恒 action=none，零沉淀。
# 修订：按 (problem_type, Level) 归族；流集合取若干整族（每轮重复同族题），holdout 取与之
# 不相交的族；并保留近重复防泄漏（跨族近重复也必须剔除）。

def _family_key(item: dict) -> str:
    pt = str(item.get("problem_type") or "?").strip().lower() or "?"
    lvl = item.get("Level")
    return f"{pt}:L{lvl}" if lvl not in (None, "") else pt


def assign_families(items: list[dict]) -> dict[str, str]:
    """task_id → family_id（(problem_type, Level)）。"""
    return {str(it.get("task_id") or it.get("id")): _family_key(it) for it in items}


def split_by_families(
    items: list[dict], *, stream_families: int, holdout_size: int, dup_threshold: float = DEFAULT_DUP_THRESHOLD
) -> dict:
    """整族划分：stream=前 stream_families 个整族（供多轮重复），holdout=其余族中凑满的子集。"""
    groups: dict[str, list[dict]] = {}
    for it in items:
        groups.setdefault(_family_key(it), []).append(it)
    ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    stream_keys = [k for k, _ in ordered[:max(1, int(stream_families))]]

    stream: list[dict] = []
    seen: set[str] = set()
    for k in stream_keys:
        for it in groups[k]:
            key = hashlib.sha256(normalize_text(_question_of(it)).encode("utf-8")).hexdigest()
            if key in seen:
                continue
            seen.add(key)
            stream.append(it)
    stream_q = [_question_of(s) for s in stream]

    holdout: list[dict] = []
    for k, _ in ordered[max(1, int(stream_families)):]:
        for it in groups[k]:
            if any(is_near_duplicate(_question_of(it), q, dup_threshold) for q in stream_q):
                continue  # 跨族近重复防泄漏
            holdout.append(it)
            if len(holdout) >= holdout_size:
                break
        if len(holdout) >= holdout_size:
            break
    return {"stream": stream, "holdout": holdout, "stream_families": stream_keys}


def family_label(item: dict) -> str:
    return _family_key(item)


# ── 冻结划分（train / dev / promotion-test / final-holdout） ────────────────
#
# W02：任务身份必须在首题之前冻结，新增样本只能追加到已存在的 train 家族，不能移动
# 任何既有 dev/promotion-test/final-holdout。近重复检查阻止同题跨 split 泄漏；历史
# pilot 已探索过的 final-holdout 任务与其家族登记为污染，不再进入新的 final 集。

SPLIT_TRAIN = "train"
SPLIT_DEV = "dev"
SPLIT_PROMOTION = "promotion_test"
SPLIT_HOLDOUT = "final_holdout"
SPLITS = (SPLIT_TRAIN, SPLIT_DEV, SPLIT_PROMOTION, SPLIT_HOLDOUT)
CONTAMINATION_FILE = "contamination.json"


class SplitManifestDrift(ValueError):
    """既有划分会被新样本/新参数改变，或 manifest 与数据不再一致。"""


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _task_key(item: dict) -> str:
    return str(item.get("task_id") or item.get("id") or "").strip()


def _question_digest(item: dict) -> str:
    return hashlib.sha256(normalize_text(_question_of(item)).encode("utf-8")).hexdigest()


def _canonical_sha256(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _manifest_payload(manifest: dict) -> dict:
    return {
        key: manifest.get(key)
        for key in ("version", "dataset", "seed", "params", "splits", "excluded", "contamination")
    }


def compute_manifest_sha256(manifest: dict) -> str:
    return _canonical_sha256(_manifest_payload(manifest))


def _split_entry(item: dict, split: str) -> dict:
    return {
        "split": split,
        "family": family_label(item),
        "questionSha256": _question_digest(item),
    }


def _counts(splits: dict[str, dict]) -> dict[str, int]:
    counts = {split: 0 for split in SPLITS}
    for entry in splits.values():
        split = str(entry.get("split") or "")
        if split in counts:
            counts[split] += 1
    return counts


def load_contamination(path: Path | str, items: list[dict] | None = None) -> dict:
    """读取/重建历史 final-holdout 污染登记。

    旧 pilot 的 checkpoint 里只要出现过 holdout_records，该题就已被研究者看过成绩；
    它和它的任务族不能再充当新的 final-holdout。登记只写一次，后续只读。
    """
    path = Path(path)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("taskIds"), list):
                return data
        except Exception:
            pass
    task_ids: set[str] = set()
    evidence: list[str] = []
    for checkpoint in sorted((ROOT / "eval" / "runs" / "evolution").rglob("checkpoint-*.json")):
        try:
            record = json.loads(checkpoint.read_text(encoding="utf-8"))
        except Exception:
            continue
        for item in record.get("holdout_records") or []:
            task_id = str(item.get("task_id") or "").strip()
            if task_id:
                task_ids.add(task_id)
                evidence.append(f"{checkpoint.relative_to(ROOT).as_posix()}#{task_id}")
    families: set[str] = set()
    by_id = {_task_key(item): item for item in (items or [])}
    for task_id in task_ids:
        item = by_id.get(task_id)
        if item is not None:
            families.add(family_label(item))
    data = {
        "version": 1,
        "registeredAt": _now_iso(),
        "reason": "historical pilot final-holdout was already observed; its task ids and families are contaminated",
        "taskIds": sorted(task_ids),
        "families": sorted(families),
        "evidence": sorted(evidence),
        "sha256": _canonical_sha256({"taskIds": sorted(task_ids), "families": sorted(families)}),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return data


def register_contamination(
    task_ids: list[str],
    *,
    reason: str,
    evidence: list[str] | None = None,
    path: Path | str = RESULTS_DIR / CONTAMINATION_FILE,
    items: list[dict] | None = None,
) -> dict:
    """显式登记污染；与已有登记合并，不删除旧证据。"""
    existing = load_contamination(path, items)
    merged_ids = sorted(set(existing.get("taskIds") or []) | {str(item) for item in task_ids if str(item).strip()})
    by_id = {_task_key(item): item for item in (items or [])}
    families = set(existing.get("families") or [])
    for task_id in merged_ids:
        item = by_id.get(task_id)
        if item is not None:
            families.add(family_label(item))
    data = {
        "version": 1,
        "registeredAt": _now_iso(),
        "reason": reason,
        "taskIds": merged_ids,
        "families": sorted(families),
        "evidence": sorted(set(existing.get("evidence") or []) | set(evidence or [])),
        "sha256": _canonical_sha256({"taskIds": merged_ids, "families": sorted(families)}),
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return data


def _dedupe_items(items: list[dict]) -> list[dict]:
    by_question: dict[str, dict] = {}
    for item in items:
        digest = _question_digest(item)
        if digest not in by_question:
            by_question[digest] = item
    return [by_question[key] for key in sorted(by_question)]


def assign_splits_by_family(
    items: list[dict],
    *,
    stream_families: int,
    holdout_size: int,
    promotion_size: int,
    dev_ratio: float = 0.2,
    dup_threshold: float = DEFAULT_DUP_THRESHOLD,
    contamination: dict | None = None,
) -> dict[str, dict]:
    """按族分配四个身份；族顺序用族名哈希而非样本数，新增样本不会重排既有族。"""
    groups: dict[str, list[dict]] = {}
    for item in items:
        if not _task_key(item):
            continue
        groups.setdefault(family_label(item), []).append(item)
    ordered = sorted(groups, key=lambda family: hashlib.sha256(("family:" + family).encode("utf-8")).hexdigest())
    stream_count = max(1, int(stream_families))
    train_families = ordered[:stream_count]
    rest_families = ordered[stream_count:]

    train_pool: list[dict] = []
    for family in train_families:
        train_pool.extend(_dedupe_items(groups[family]))
    train_pool = sorted(train_pool, key=_question_digest)
    dev_count = min(max(0, len(train_pool) - 1), int(round(len(train_pool) * max(0.0, min(0.5, dev_ratio)))))
    dev_items = train_pool[:dev_count]
    train_items = train_pool[dev_count:]

    rest_items: list[dict] = []
    for family in rest_families:
        rest_items.extend(_dedupe_items(groups[family]))
    rest_items = sorted(rest_items, key=_question_digest)

    known_questions = [_question_of(item) for item in train_items + dev_items]
    contaminated_ids = {str(item) for item in (contamination or {}).get("taskIds") or []}
    contaminated_families = {str(item) for item in (contamination or {}).get("families") or []}
    promotion: list[dict] = []
    final: list[dict] = []
    for item in rest_items:
        if _task_key(item) in contaminated_ids or family_label(item) in contaminated_families:
            continue
        if any(is_near_duplicate(_question_of(item), question, dup_threshold) for question in known_questions):
            continue
        if len(final) < max(0, int(holdout_size)):
            final.append(item)
            known_questions.append(_question_of(item))
        elif len(promotion) < max(0, int(promotion_size)):
            promotion.append(item)
            known_questions.append(_question_of(item))
        else:
            break

    splits: dict[str, dict] = {}
    for item in train_items:
        splits[_task_key(item)] = _split_entry(item, SPLIT_TRAIN)
    for item in dev_items:
        splits[_task_key(item)] = _split_entry(item, SPLIT_DEV)
    for item in promotion:
        splits[_task_key(item)] = _split_entry(item, SPLIT_PROMOTION)
    for item in final:
        splits[_task_key(item)] = _split_entry(item, SPLIT_HOLDOUT)
    return splits


def build_split_manifest(
    items: list[dict],
    *,
    dataset: str,
    seed: int,
    stream_families: int,
    holdout_size: int,
    promotion_size: int,
    dev_ratio: float = 0.2,
    dup_threshold: float = DEFAULT_DUP_THRESHOLD,
    contamination: dict | None = None,
    revision: int = 1,
    parent_sha256: str | None = None,
) -> dict:
    splits = assign_splits_by_family(
        items,
        stream_families=stream_families,
        holdout_size=holdout_size,
        promotion_size=promotion_size,
        dev_ratio=dev_ratio,
        dup_threshold=dup_threshold,
        contamination=contamination,
    )
    manifest = {
        "version": 1,
        "dataset": dataset,
        "seed": int(seed),
        "params": {
            "stream_families": int(stream_families),
            "holdout_size": int(holdout_size),
            "promotion_size": int(promotion_size),
            "dev_ratio": float(dev_ratio),
            "dup_threshold": float(dup_threshold),
        },
        "splits": splits,
        "excluded": {
            _task_key(item): {
                "family": family_label(item),
                "questionSha256": _question_digest(item),
                "reason": "not selected into train/dev/promotion/final",
            }
            for item in items
            if _task_key(item) and _task_key(item) not in splits
        },
        "counts": _counts(splits),
        "contamination": contamination or {},
        "createdAt": _now_iso(),
        "revision": int(revision),
        "parentSha256": parent_sha256,
    }
    manifest["manifestSha256"] = compute_manifest_sha256(manifest)
    return manifest


def _manifest_params(manifest: dict) -> dict:
    return dict(manifest.get("params") or {})


def load_or_create_split_manifest(
    path: Path | str,
    items: list[dict],
    *,
    dataset: str,
    seed: int,
    stream_families: int,
    holdout_size: int,
    promotion_size: int,
    dev_ratio: float = 0.2,
    dup_threshold: float = DEFAULT_DUP_THRESHOLD,
    contamination: dict | None = None,
) -> dict:
    """读取不可变划分；新样本只能追加到既有 train 家族，任何既有身份变化都拒绝。"""
    path = Path(path)
    expected = {
        "stream_families": int(stream_families),
        "holdout_size": int(holdout_size),
        "promotion_size": int(promotion_size),
        "dev_ratio": float(dev_ratio),
        "dup_threshold": float(dup_threshold),
    }
    if not path.is_file():
        manifest = build_split_manifest(
            items,
            dataset=dataset,
            seed=seed,
            stream_families=stream_families,
            holdout_size=holdout_size,
            promotion_size=promotion_size,
            dev_ratio=dev_ratio,
            dup_threshold=dup_threshold,
            contamination=contamination,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return manifest

    manifest = json.loads(path.read_text(encoding="utf-8"))
    if compute_manifest_sha256(manifest) != manifest.get("manifestSha256"):
        raise SplitManifestDrift(f"split manifest hash mismatch: {path}")
    if _manifest_params(manifest) != expected:
        raise SplitManifestDrift("split manifest params changed; create a new manifest instead of reusing this one")
    if str(manifest.get("dataset") or "") != str(dataset) or int(manifest.get("seed") or 0) != int(seed):
        raise SplitManifestDrift("split manifest dataset/seed changed")
    if contamination and (manifest.get("contamination") or {}).get("sha256") != (contamination or {}).get("sha256"):
        raise SplitManifestDrift("contamination registry changed; create a new final-holdout manifest")

    by_id = {_task_key(item): item for item in items}
    splits = dict(manifest.get("splits") or {})
    excluded = dict(manifest.get("excluded") or {})
    for task_id, entry in splits.items():
        item = by_id.get(task_id)
        if item is None:
            raise SplitManifestDrift(f"existing split task missing from dataset: {task_id}")
        if _question_digest(item) != entry.get("questionSha256"):
            raise SplitManifestDrift(f"question changed for frozen task: {task_id}")
    for task_id, entry in excluded.items():
        item = by_id.get(task_id)
        if item is not None and _question_digest(item) != entry.get("questionSha256"):
            raise SplitManifestDrift(f"question changed for excluded task: {task_id}")

    existing_questions = {entry.get("questionSha256") for entry in splits.values()}
    train_families = {entry.get("family") for entry in splits.values() if entry.get("split") == SPLIT_TRAIN}
    new_items = [item for task_id, item in by_id.items() if task_id not in splits and task_id not in excluded]
    added: list[str] = []
    for item in new_items:
        task_id = _task_key(item)
        family = family_label(item)
        if family not in train_families:
            raise SplitManifestDrift(
                f"new sample {task_id} belongs to non-train family {family}; a new manifest version is required"
            )
        digest = _question_digest(item)
        if digest in existing_questions:
            raise SplitManifestDrift(f"new sample duplicates a frozen question: {task_id}")
        splits[task_id] = _split_entry(item, SPLIT_TRAIN)
        existing_questions.add(digest)
        added.append(task_id)
    if added:
        old_hash = manifest.get("manifestSha256")
        manifest["splits"] = splits
        manifest["counts"] = _counts(splits)
        manifest["revision"] = int(manifest.get("revision") or 1) + 1
        manifest["parentSha256"] = old_hash
        manifest["updatedAt"] = _now_iso()
        manifest["manifestSha256"] = compute_manifest_sha256(manifest)
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def manifest_items(manifest: dict, items: list[dict]) -> dict[str, list[dict]]:
    by_id = {_task_key(item): item for item in items}
    result: dict[str, list[dict]] = {split: [] for split in SPLITS}
    for task_id, entry in sorted((manifest.get("splits") or {}).items()):
        split = str(entry.get("split") or "")
        if split not in result:
            raise SplitManifestDrift(f"unknown split for {task_id}: {split}")
        item = by_id.get(task_id)
        if item is None:
            raise SplitManifestDrift(f"manifest task missing from dataset: {task_id}")
        result[split].append(item)
    return result


# ── 实验计划 ────────────────────────────────────────────────────────────────

def build_plan(
    *, dataset: str, n_rounds: int, tasks_per_round: int, holdout_size: int,
    stream_size: int, est_tokens_per_task: int = 540_000, arms: tuple[str, ...] = ARMS,
) -> dict[str, Any]:
    """生成可打印、可存档的实验计划（含检查点与预算粗估）。"""
    if n_rounds <= 0:
        raise ValueError("n_rounds must be >= 1")
    checkpoints = sorted({max(1, n_rounds // 2), n_rounds})
    return {
        "dataset": dataset,
        "judge_version": JUDGE_VERSION,
        "arms": list(arms),
        "rounds": n_rounds,
        "tasks_per_round": tasks_per_round,
        "holdout_size": holdout_size,
        "stream_size": stream_size,
        "checkpoints": checkpoints,
        "simulated_user_feedback": True,  # 若用规则化模拟器生成经验，报告须标注
        "budget_estimate": {
            "tokens_stream": n_rounds * tasks_per_round * est_tokens_per_task * len(arms),
            "tokens_holdout": n_rounds * holdout_size * est_tokens_per_task * len(arms),
            "est_tokens_per_task": est_tokens_per_task,
            "note": "粗估，不含重试/工具开销；先跑 3—5 轮估计真实方差与单题成本再定规模。",
        },
        "pilot_rounds": min(5, n_rounds),
    }


# ── 曲线汇总（离线可算） ────────────────────────────────────────────────────

def curve_from_rounds(rounds: list[dict], *, seed: int = 20260119) -> dict[str, Any]:
    """把逐轮 holdout 结果汇总为「成功率随进化步数」曲线 + CI + 成本。

    rounds: [{round, arm, correct_flags(0/1 列表), tokens_M, latency_s, failures:{...}}]
    """
    points: list[dict] = []
    for r in rounds:
        flags = [bool(x) for x in r.get("correct_flags", [])]
        ci = bootstrap_ci95(flags, seed=seed)
        points.append({
            "round": int(r.get("round", 0)),
            "arm": str(r.get("arm") or ""),
            "n": len(flags),
            "pass_at_1": round(sum(flags) / len(flags) * 100, 1) if flags else 0.0,
            "ci95": ci,
            "tokens_M": round(float(r.get("tokens_M", 0.0) or 0.0), 1),
            "latency_s": round(float(r.get("latency_s", 0.0) or 0.0), 1),
            "failures": r.get("failures") or {},
        })
    by_arm: dict[str, list[dict]] = {}
    for p in points:
        by_arm.setdefault(p["arm"], []).append(p)
    deltas: dict[str, Any] = {}
    for arm, pts in by_arm.items():
        if len(pts) >= 2:
            deltas[arm] = round(pts[-1]["pass_at_1"] - pts[0]["pass_at_1"], 1)
    return {"points": points, "delta_first_to_last": deltas}


# ── CLI ─────────────────────────────────────────────────────────────────────

def _load_items(dataset: str) -> list[dict]:
    path = DATA_FILES[dataset]
    if not path.is_file():
        raise SystemExit(f"dataset not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _filter_items_by_ids(items: list[dict], ids_file: str | None) -> list[dict]:
    """按给定 task_id 清单筛题（用于"只用有区分度的题"这类校准后实验）。

    背景：holdout 校准实测 GAIA 165 题里 68 题对所有配置都失败、48 题都通过，
    只有 49 题（29.7%）有区分度。在全 0/全 1 的题池上做配置对照，
    得到的只是"无信号"，白烧 token。这里让实验可以只用 discriminating 子集。
    """
    if not ids_file:
        return items
    path = Path(ids_file)
    if not path.is_file():
        raise SystemExit(f"task-id file not found: {path}")
    wanted = {str(x) for x in json.loads(path.read_text(encoding="utf-8"))}
    kept = [it for it in items if str(it.get("task_id") or it.get("id") or "") in wanted]
    if not kept:
        raise SystemExit(f"task-id file {path} 与数据集无交集：检查 id 是否来自同一 dataset")
    return kept


def _cmd_plan(args: argparse.Namespace) -> int:
    items = _filter_items_by_ids(_load_items(args.dataset), getattr(args, "task_ids_file", None))
    split = split_task_stream(items, holdout_size=args.holdout, seed=args.seed)
    plan = build_plan(
        dataset=args.dataset, n_rounds=args.rounds, tasks_per_round=args.tasks_per_round,
        holdout_size=len(split["holdout"]), stream_size=len(split["stream"]),
    )
    plan["split"] = {
        "stream": len(split["stream"]),
        "holdout": len(split["holdout"]),
        "holdout_task_ids": [str(h.get("task_id") or h.get("id")) for h in split["holdout"]],
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / "plan.json"
    out.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    print(f"[plan written] {out}")
    return 0


# ── 真实模型运行（E-04）：arm 隔离 + 两轮（任务→模拟反馈）驱动沉淀 ─────────────

SESSION_MEMORY_DEFAULT = "D:/claudecode/scratch/iterate-run/ws"


def _arm_env(arm: str, ws_root: Path) -> tuple[dict, Path]:
    """每个 arm 独立 workspace：独立 cwd 与 HOME，隔离技能库与记忆；用环境变量切换门控。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    if arm == "no_evolution":
        env["EVOHARNESS_AUTO_SKILL_EVOLUTION"] = "0"
    else:
        env["EVOHARNESS_AUTO_SKILL_EVOLUTION"] = "1"
        env["EVOHARNESS_IR_MODE"] = "strict" if arm == "gated_evolution" else "off"
    env["EVOHARNESS_AUTO_SKILL_TARGET"] = "project"
    home = ws_root / arm / "home"
    cwd = ws_root / arm / "cwd"
    home.mkdir(parents=True, exist_ok=True)
    cwd.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    return env, cwd


def _seed_workspace(arm: str, ws_root: Path) -> None:
    """把生产起点技能集（仓库 `.evoharness/skills`）拷贝进 arm 的 cwd 作为冻结起点。"""
    _, cwd = _arm_env(arm, ws_root)
    base = ROOT / ".evoharness" / "skills"
    dst = cwd / ".evoharness" / "skills"
    if not dst.exists() and base.is_dir():
        shutil.copytree(base, dst)


def _count_skills(arm: str, ws_root: Path) -> int:
    _, cwd = _arm_env(arm, ws_root)
    root = cwd / ".evoharness" / "skills"
    return sum(1 for _ in root.rglob("SKILL.md")) if root.is_dir() else 0


def simulated_feedback(item: dict, dataset: str) -> str:
    """规则化模拟用户反馈（**必须在报告中标注为模拟**）：由已知 ground truth + 任务族生成。

    修订点（首轮 pilot 证据 action=none）：反馈显式点名任务族并直接要求沉淀可复用经验，
    使抽取器拿到「这是一类可复用模式」的信号，而不是泛泛的一句话。
    """
    gold = str(item.get("answer") or "")
    fam = _family_key(item)
    return (
        "[simulated user feedback] Previous task family: "
        f"{fam}. Ground truth: {gold}. If your answer differed, extract ONE durable, reusable "
        "procedure or decision rule that generalises across this task family (how to attack this "
        "kind of problem, which tools/steps to prefer). Persist it as a skill. Do NOT record "
        "task-specific trivia."
    )


async def _evolve_from_agent(agent, feedback: str) -> dict:
    """触发在线沉淀并**确认它真的执行了**（pilot3 证据：直接 extract_now 会静默空转）。

    生产路径的沉淀是「延迟」的：上一轮结束时挂起 pending window，下一轮才消费。实验里只有
    1 轮，因此 pending window 可能根本没被设置（pilot3 实测 `ok=false, no pending ...`）。
    回退策略：没有 pending window 时，直接用 agent 现成的对话历史调用生产入口 `online_ingest`
    ——同一套抽取/IR 门/写盘链路，只是不再依赖挂起状态。
    """
    if hasattr(agent, "_online_evolution_enabled") and not agent._online_evolution_enabled():
        return {"via": "disabled", "ok": False, "error": "online skill evolution disabled by env"}

    skills_root = Path.cwd() / ".evoharness" / "skills"
    usage_log = Path.cwd() / ".evoharness" / "skill-evolution" / "usage.jsonl"
    before_skills = _count_skills_in(skills_root)
    before_usage = usage_log.stat().st_size if usage_log.is_file() else -1
    try:
        res = await agent.extract_now(hint=feedback)
    except Exception as exc:  # noqa: BLE001
        res = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    after_skills = _count_skills_in(skills_root)
    after_usage = usage_log.stat().st_size if usage_log.is_file() else -1
    if isinstance(res, dict) and res.get("ok") and (after_skills > before_skills or after_usage != before_usage):
        return {
            "via": "extract_now",
            **res,
            "skills_before": before_skills,
            "skills_after": after_skills,
            "write_observed": after_skills > before_skills or after_usage != before_usage,
        }

    try:
        from agents.online_skill_evolution import online_ingest

        messages = list(agent._recent_dialog_messages(max_messages=8) or [])
        if not messages:
            return {"via": "online_ingest", "ok": False, "error": "no dialog messages to ingest"}
        messages = messages + [{"role": "user", "content": feedback}]
        side_query = agent._build_side_query(max_tokens=2200, label="evolution_extract")
        if side_query is None:
            return {"via": "online_ingest", "ok": False, "error": "no side_query (model config missing)"}
        out = await online_ingest(
            messages=messages,
            side_query=side_query,
            retrieved_reference=None,
            hint=feedback,
            confirm_write=agent._confirm_online_skill_write,
            target=os.environ.get("EVOHARNESS_AUTO_SKILL_TARGET", "project"),
        )
        return {"via": "online_ingest", **(out if isinstance(out, dict) else {"ok": False})}
    except Exception as exc:  # noqa: BLE001
        return {"via": "online_ingest", "ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _count_skills_in(root: Path) -> int:
    return sum(1 for _ in root.rglob("SKILL.md")) if root.is_dir() else 0


def _run_worker_task(item: dict, dataset: str, *, evolve: bool, max_turns: int, timeout_s: int) -> dict:
    """在独立线程跑一个任务（1 轮）；evolve=True 时以模拟反馈为 hint 直接提取沉淀。"""
    import threading

    from eval.run_benchmark import FINAL_ANSWER_RE, build_gaia_prompt, build_hle_prompt, extract_final_answer, gaia_match, hle_match

    holder: dict = {}
    skills_root = Path.cwd() / ".evoharness" / "skills"
    holder["skills_before"] = _count_skills_in(skills_root)

    def _work() -> None:
        from agents.agent import Agent
        from agents.main import _resolve_api_config

        agent = None
        try:
            base, key, use_openai = _resolve_api_config(None)
            agent = Agent(
                permission_mode="bypassPermissions",
                model=os.environ.get("MODEL"),
                max_turns=max_turns,
                api_base=base if use_openai else None,
                anthropic_base_url=base if not use_openai else None,
                api_key=key,
            )
            prompt = build_gaia_prompt(item) if dataset == "gaia" else build_hle_prompt(item)
            feedback = simulated_feedback(item, dataset)

            async def _go() -> str:
                r1 = await agent.run_once(prompt)
                text = str((r1 or {}).get("text") or "")
                if evolve:
                    # 修订（首轮 pilot 证据 action=none；pilot3 证据 extract_now 静默空转）：
                    # 以模拟反馈为 hint 触发沉淀，无 pending window 时回退到生产 online_ingest。
                    holder["evolution"] = await _evolve_from_agent(agent, feedback)
                await agent.drain_background_skill_tasks()
                return text

            holder["text"] = asyncio.run(_go())
            holder["tokens_in"] = agent.total_input_tokens
            holder["tokens_out"] = agent.total_output_tokens
            # 旁路查询开销：主循环之外（沉淀抽取 + 使用判定 + 记忆/折叠）单独记账，
            # 让"沉淀这一步花了多少 token"从整题成本里分出来。
            holder["side_in"] = agent.side_query_input_tokens
            holder["side_out"] = agent.side_query_output_tokens
            holder["side_by_label"] = dict(agent.side_query_tokens_by_label)
            # 折叠次数：与 tokens 一样经 holder 传出（agent 是本线程局部变量）
            holder["folds"] = int(getattr(agent, "_fold_count", 0))
        except Exception as exc:  # noqa: BLE001
            holder["error"] = str(exc)[:300]
        finally:
            if agent is not None:
                for conn in list(getattr(agent._mcp_manager, "_connections", {}).values()):
                    try:
                        conn.close()
                    except Exception:
                        pass

    thread = threading.Thread(target=_work, daemon=True)
    t0 = time.time()
    thread.start()
    while thread.is_alive() and time.time() - t0 < timeout_s:
        time.sleep(5)
    timed_out = thread.is_alive()
    text = str(holder.get("text") or "")
    error = str(holder.get("error") or "")
    if timed_out:
        error = error or f"watchdog: task exceeded {timeout_s}s, abandoned"
    prediction = extract_final_answer(text)
    gold = str(item.get("answer") or "")
    if dataset == "gaia":
        correct = bool(prediction) and gaia_match(prediction, gold)
    else:
        correct = bool(prediction) and hle_match(prediction, gold, str(item.get("answer_type") or ""))
    return {
        "task_id": str(item.get("task_id") or item.get("id")),
        "family": _family_key(item),
        "gold": gold,
        "prediction": prediction,
        "correct": correct,
        "folds": int(holder.get("folds") or 0),
        "has_final_marker": bool(FINAL_ANSWER_RE.findall(text or "")),
        "timed_out": timed_out,
        "error": error,
        "evolution": holder.get("evolution") or {},
        "skills_before": int(holder.get("skills_before", 0) or 0),
        "skills_after": _count_skills_in(skills_root),
        "tokens": {"input": int(holder.get("tokens_in", 0) or 0), "output": int(holder.get("tokens_out", 0) or 0)},
        "side_query_tokens": {
            "input": int(holder.get("side_in", 0) or 0),
            "output": int(holder.get("side_out", 0) or 0),
            "by_label": holder.get("side_by_label") or {},
        },
        "latency_s": round(time.time() - t0, 1),
    }


def _cmd_worker(args: argparse.Namespace) -> int:
    item = json.loads(Path(args.item_json).read_text(encoding="utf-8"))
    rec = _run_worker_task(item, args.dataset, evolve=args.evolve, max_turns=args.max_turns, timeout_s=args.timeout)
    text = json.dumps(rec, ensure_ascii=False)
    # 结果优先写文件：子进程 stdout 混有 spinner/工具日志，长跑崩溃时按 stdout 解析会丢结果。
    if getattr(args, "out", None):
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


BALANCE_ERROR_MARKERS = ("insufficient balance", "insufficient_quota", "no balance", "402")


def _is_balance_error(rec: dict) -> bool:
    err = str(rec.get("error") or "")
    return any(m in err.lower() for m in BALANCE_ERROR_MARKERS)


def _digest_tree(root: Path) -> str:
    digest = hashlib.sha256()
    if not root.is_dir():
        return digest.hexdigest()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in {"__pycache__", ".pytest_cache"} for part in path.parts):
            continue
        if path.name.endswith(".tmp"):
            continue
        rel = path.relative_to(root).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except Exception:
            digest.update(b"<unreadable>")
        digest.update(b"\0")
    return digest.hexdigest()


def _state_digest(cwd: Path) -> dict:
    """Skill/usage/provenance/status/tier 的联合哈希；holdout 前后必须完全相同。"""
    base = Path(cwd) / ".evoharness"
    skills = _digest_tree(base / "skills")
    evolution = _digest_tree(base / "skill-evolution")
    combined = hashlib.sha256(f"{skills}:{evolution}".encode("utf-8")).hexdigest()
    return {"skills": skills, "skillEvolution": evolution, "combined": combined}


def _prepare_holdout_workspace(arm: str, ws_root: Path, rnd: int) -> Path:
    """从当前 arm 的只读快照启动 holdout：只拷贝 .evoharness（技能与进化状态）。

    不拷贝上一轮产物/预测，避免 holdout 读到历史任务输出；快照目录本身随后被
    `EVOHARNESS_HOLDOUT_READONLY=1` 保护，写入会被 skill_evolution 层拒绝。
    """
    src_cwd = ws_root / arm / "cwd"
    src_home = ws_root / arm / "home"
    dst = ws_root / arm / "holdout" / f"r{rnd}"
    if dst.exists():
        shutil.rmtree(dst)
    (dst / "cwd").mkdir(parents=True, exist_ok=True)
    (dst / "home").mkdir(parents=True, exist_ok=True)
    if (src_cwd / ".evoharness").is_dir():
        shutil.copytree(
            src_cwd / ".evoharness",
            dst / "cwd" / ".evoharness",
            ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"),
        )
    if (src_home / ".evoharness").is_dir():
        shutil.copytree(
            src_home / ".evoharness",
            dst / "home" / ".evoharness",
            ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"),
        )
    return dst


def _arm_manifest(arms: list[str], *, gated_promotion: bool) -> dict:
    manifest: dict[str, dict] = {}
    for arm in arms:
        evolve = arm != "no_evolution"
        manifest[arm] = {
            "autoSkillEvolution": evolve,
            "irMode": "strict" if arm == "gated_evolution" else ("off" if arm == "ungated_evolution" else None),
            "streamEvolve": evolve,
            "holdoutReadonly": True,
            "promotion": (
                "apply_eval_promotions"
                if arm == "gated_evolution" and gated_promotion
                else "disabled"
            ),
        }
    return manifest


def _run_single(
    item: dict,
    dataset: str,
    arm: str,
    ws_root: Path,
    *,
    max_turns: int,
    timeout_s: int,
    evolve: bool,
    holdout: bool = False,
    holdout_dir: Path | None = None,
) -> dict:
    """父进程侧：spawn 一个 worker 子进程（天然隔离 env/cwd/HOME），解析其 JSON 结果。

    holdout=True 时在 `holdout_dir` 的只读快照里运行，并记录快照状态哈希前后对比。
    结果读取优先用 worker 写出的结果文件（`--out`），stdout 解析仅作回退。
    """
    env, cwd = _arm_env(arm, ws_root)
    if holdout:
        if holdout_dir is None:
            raise ValueError("holdout_dir is required when holdout=True")
        cwd = Path(holdout_dir) / "cwd"
        home = Path(holdout_dir) / "home"
        cwd.mkdir(parents=True, exist_ok=True)
        home.mkdir(parents=True, exist_ok=True)
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
        env["EVOHARNESS_HOLDOUT_READONLY"] = "1"
        arm_dir = Path(holdout_dir)
        state_before = _state_digest(cwd)
    else:
        arm_dir = ws_root / arm
        state_before = None
    item_path = arm_dir / "_item.json"
    out_path = arm_dir / "_result.json"
    err_path = arm_dir / "_stderr.txt"
    item_path.write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")
    if out_path.exists():
        out_path.unlink()
    cmd = [
        sys.executable, "-m", "eval.evolution_experiment", "worker",
        "--item-json", str(item_path), "--dataset", dataset,
        "--max-turns", str(max_turns), "--timeout", str(timeout_s),
        "--out", str(out_path),
    ]
    if evolve:
        cmd.append("--evolve")
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True, timeout=timeout_s + 180)
        out = (p.stdout or b"").decode("utf-8", errors="replace")
        err = (p.stderr or b"").decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        rec = {"task_id": str(item.get("task_id") or item.get("id")), "correct": False, "timed_out": True,
               "error": "worker process timeout", "tokens": {"input": 0, "output": 0}, "latency_s": round(time.time() - t0, 1)}
        if holdout:
            rec["holdout_readonly"] = {
                "enabled": True,
                "before": state_before,
                "after": _state_digest(cwd),
                "ok": state_before == _state_digest(cwd),
            }
        return rec
    try:
        err_path.write_text(err, encoding="utf-8")
    except Exception:
        pass

    rec = None
    if out_path.exists():
        try:
            rec = json.loads(out_path.read_text(encoding="utf-8"))
        except Exception:
            rec = None
    if rec is None:
        for line in reversed(out.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    rec = json.loads(line)
                    break
                except Exception:
                    continue
    if rec is None:
        rec = {"task_id": str(item.get("task_id") or item.get("id")), "correct": False, "timed_out": False,
               "tokens": {"input": 0, "output": 0}, "latency_s": round(time.time() - t0, 1),
               # infra_error 与"答错"必须分开：worker 没产出 JSON 几乎总是环境/路径/依赖问题，
               # 把它算成普通 0 分会让曲线看起来"模型不行"，实际是评测自身坏了。
               "infra_error": True,
               "error": "worker_no_json: " + err[-200:]}
    if holdout:
        state_after = _state_digest(cwd)
        rec["holdout_readonly"] = {
            "enabled": True,
            "before": state_before,
            "after": state_after,
            "ok": state_before == state_after,
        }
    return rec


def _apply_gated_promotion(arm: str, ws_root: Path) -> dict:
    """gated arm 的部署晋升门：在该 arm workspace 跑在线评测 → apply_eval_promotions。"""
    env, cwd = _arm_env(arm, ws_root)
    code = (
        "import sys, json, asyncio\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from agents.online_skill_eval import evaluate_online_skill_evolution_async as ev\n"
        "from agents.skill_tiers import apply_eval_promotions\n"
        "r = asyncio.run(ev(write_report=True, write_artifacts=True))\n"
        "print(json.dumps(apply_eval_promotions(r)))\n"
    )
    try:
        p = subprocess.run([sys.executable, "-c", code], cwd=str(cwd), env=env, capture_output=True, timeout=900)
        out = (p.stdout or b"").decode("utf-8", errors="replace")
        for line in reversed(out.splitlines()):
            if line.strip().startswith("{"):
                return json.loads(line.strip())
        return {"error": "no_json", "stderr": (p.stderr or b"").decode("utf-8", "replace")[-200:]}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def _round_batch(stream: list[dict], rnd: int, size: int) -> list[dict]:
    if not stream:
        return []
    start = ((rnd - 1) * size) % len(stream)
    return [stream[(start + i) % len(stream)] for i in range(size)]


def _cmd_run(args: argparse.Namespace) -> int:
    """真实模型运行：需 API 预算。未配置时明确报 blocked，不产出伪造结果。"""
    from agents.main import _resolve_api_config

    base, key, _use_openai = _resolve_api_config(None)
    if not (base and key):
        print(json.dumps({
            "status": "blocked",
            "reason": "no model API budget configured (set API key env); E-04 real-model arm runs not executed",
            "offline_available": "python -m eval.evolution_experiment plan --dataset gaia",
        }, ensure_ascii=False, indent=2))
        return 3

    items = _filter_items_by_ids(_load_items(args.dataset), getattr(args, "task_ids_file", None))
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    contamination_path = Path(getattr(args, "contamination", None) or (RESULTS_DIR / CONTAMINATION_FILE))
    contamination = load_contamination(contamination_path, items)
    promotion_size = int(getattr(args, "promotion_size", 0) or max(1, args.holdout))
    dev_ratio = float(getattr(args, "dev_ratio", 0.2))
    dup_threshold = float(getattr(args, "dup_threshold", DEFAULT_DUP_THRESHOLD))
    split_manifest_path: Path | None = None
    split_manifest: dict | None = None
    dev: list[dict] = []
    promotion: list[dict] = []
    if args.families:
        split_manifest_path = Path(
            getattr(args, "split_manifest", None) or (RESULTS_DIR / f"split-manifest-{args.dataset}.json")
        )
        split_manifest = load_or_create_split_manifest(
            split_manifest_path,
            items,
            dataset=args.dataset,
            seed=args.seed,
            stream_families=args.stream_families,
            holdout_size=args.holdout,
            promotion_size=promotion_size,
            dev_ratio=dev_ratio,
            dup_threshold=dup_threshold,
            contamination=contamination,
        )
        grouped = manifest_items(split_manifest, items)
        stream = grouped[SPLIT_TRAIN]
        dev = grouped[SPLIT_DEV]
        promotion = grouped[SPLIT_PROMOTION]
        holdout = grouped[SPLIT_HOLDOUT]
        if not stream:
            print(json.dumps({
                "status": "failed",
                "reason": "frozen split produced an empty train stream; adjust stream_families/dev_ratio",
                "counts": split_manifest.get("counts"),
            }, ensure_ascii=False, indent=2))
            return 4
        if not holdout:
            print(json.dumps({
                "status": "failed",
                "reason": "frozen split produced an empty final-holdout; refusing to report an unmeasured final claim",
                "counts": split_manifest.get("counts"),
                "contamination": {"taskIds": contamination.get("taskIds"), "families": contamination.get("families")},
            }, ensure_ascii=False, indent=2))
            return 4
    else:
        split = split_task_stream(items, holdout_size=args.holdout, seed=args.seed)
        stream, holdout = split["stream"], split["holdout"]
    arms = [a for a in str(args.arms).split(",") if a in ARMS] or list(ARMS)
    # 必须是绝对路径：worker 子进程用 `cwd=arm cwd` 启动，传相对路径时，
    # `--item-json eval/runs/...` 会在**子进程的 cwd** 下解析 → FileNotFoundError
    # → 被记成 worker_no_json → 该题静默记 0 分。这是最坏的评测污染：
    # 分数看起来"有值"，实际是路径 bug 造成的假失败。
    ws_root = Path(args.workspace or SESSION_MEMORY_DEFAULT).resolve()
    # 批次标识：checkpoint/curve 必须绑定它，否则不同 workspace 的两批会同名覆盖，
    # 覆盖后无法从文件本身分辨归属（2026-10-29 真实事故：差点用混合数据下结论）。
    batch_tag = _batch_tag(ws_root)
    (RESULTS_DIR / f"batch-{batch_tag}.json").write_text(
        json.dumps({
            "batchTag": batch_tag,
            "workspace": str(ws_root),
            "rounds": getattr(args, "rounds", None),
            "tasksPerRound": getattr(args, "tasks_per_round", None),
            "holdout": getattr(args, "holdout", None),
            "arms": arms,
            "model": os.environ.get("MODEL"),
            "startedAt": __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime()),
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    arms_manifest = _arm_manifest(arms, gated_promotion=bool(getattr(args, "gated_promotion", False)))
    (RESULTS_DIR / "arms-manifest.json").write_text(
        json.dumps(arms_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(json.dumps({
        "status": "running", "dataset": args.dataset, "arms": arms, "rounds": args.rounds,
        "tasks_per_round": args.tasks_per_round, "holdout": len(holdout),
        "dev": len(dev), "promotion_test": len(promotion),
        "max_turns": args.max_turns, "timeout_s": args.timeout, "simulated_user_feedback": True,
        "families": bool(args.families), "stream_families": args.stream_families,
        "stream_families_in_use": sorted({family_label(it) for it in stream}),
        "dev_families_in_use": sorted({family_label(it) for it in dev}),
        "promotion_test_families_in_use": sorted({family_label(it) for it in promotion}),
        "holdout_families_in_use": sorted({family_label(it) for it in holdout}),
        "split_manifest": (
            {
                "path": str(split_manifest_path),
                "sha256": split_manifest.get("manifestSha256"),
                "revision": split_manifest.get("revision"),
                "counts": split_manifest.get("counts"),
            }
            if split_manifest
            else None
        ),
        "contamination": {
            "taskIds": contamination.get("taskIds"),
            "families": contamination.get("families"),
        },
        "arms_manifest": arms_manifest,
    }, ensure_ascii=False, indent=2), flush=True)

    rounds_out: list[dict] = []
    for arm in arms:
        _seed_workspace(arm, ws_root)
        base_skills = _count_skills(arm, ws_root)
        for rnd in range(1, args.rounds + 1):
            batch = _round_batch(stream, rnd + args.stream_offset, args.tasks_per_round)
            stream_recs = [
                _run_single(
                    it, args.dataset, arm, ws_root,
                    max_turns=args.max_turns, timeout_s=args.timeout,
                    evolve=arm != "no_evolution",
                )
                for it in batch
            ]
            if any(_is_balance_error(r) for r in stream_recs):
                # 余额耗尽（429/A402）：立刻中止，不再空跑后续题（避免把"无余额"记成"任务失败"）。
                blocked = {
                    "status": "blocked",
                    "reason": "model account balance exhausted (429 insufficient balance); remaining arms/rounds not executed",
                    "arm": arm, "round": rnd, "completed_rounds": len(rounds_out),
                    "evidence": next(r.get("error") for r in stream_recs if _is_balance_error(r)),
                }
                (RESULTS_DIR / f"curve-{batch_tag}.json").write_text(json.dumps(blocked, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                print(json.dumps(blocked, ensure_ascii=False, indent=2))
                return 2
            promo = _apply_gated_promotion(arm, ws_root) if (arm == "gated_evolution" and args.gated_promotion) else {}
            original_before = _state_digest(ws_root / arm / "cwd")
            holdout_dir = _prepare_holdout_workspace(arm, ws_root, rnd)
            holdout_recs = [
                _run_single(
                    it, args.dataset, arm, ws_root,
                    max_turns=args.max_turns, timeout_s=args.timeout,
                    evolve=False, holdout=True, holdout_dir=holdout_dir,
                )
                for it in holdout
            ]
            original_after = _state_digest(ws_root / arm / "cwd")
            holdout_readonly_ok = (
                original_before == original_after
                and all(bool((r.get("holdout_readonly") or {}).get("ok")) for r in holdout_recs)
            )
            skill_count = _count_skills(arm, ws_root)
            entry = {
                "round": rnd,
                "arm": arm,
                "correct_flags": [1 if r.get("correct") else 0 for r in holdout_recs],
                # 与 correct_flags 一一对应的 task_id：没有它就无法确认三臂同题，
                # 也就无法做配对检验（McNemar 比 CI 更适合稀疏事件）。
                "holdout_task_ids": [str(r.get("task_id") or "") for r in holdout_recs],
                "stream_families": sorted({r.get("family", "") for r in stream_recs}),
                "split_counts": split_manifest.get("counts") if split_manifest else None,
                "holdout_readonly_ok": holdout_readonly_ok,
                "holdout_readonly": {
                    "originalBefore": original_before,
                    "originalAfter": original_after,
                    "snapshots": [r.get("holdout_readonly") for r in holdout_recs],
                },
                "promotion_test_count": len(promotion),
                "stream_tokens_M": round(sum(r["tokens"]["input"] + r["tokens"]["output"] for r in stream_recs) / 1e6, 2),
                "holdout_tokens_M": round(sum(r["tokens"]["input"] + r["tokens"]["output"] for r in holdout_recs) / 1e6, 2),
                # 旁路查询（沉淀抽取 + 使用判定 + 记忆/折叠）开销单列，便于回答
                # "沉淀这一步到底花了多少 token"。
                "side_query_tokens_M": round(
                    sum(
                        int((r.get("side_query_tokens") or {}).get("input", 0) or 0)
                        + int((r.get("side_query_tokens") or {}).get("output", 0) or 0)
                        for r in stream_recs + holdout_recs
                    )
                    / 1e6,
                    4,
                ),
                "stream_latency_s": round(sum(r.get("latency_s", 0) for r in stream_recs), 1),
                "holdout_latency_s": round(sum(r.get("latency_s", 0) for r in holdout_recs), 1),
                "skill_count": skill_count,
                "new_skills": skill_count - base_skills,
                "evolution_fired": skill_count > base_skills,
                "stream_tasks_that_evolved": sum(1 for r in stream_recs if r.get("skills_after", 0) > r.get("skills_before", 0)),
                "stream_failures": {
                    "timeout": sum(1 for r in stream_recs if r.get("timed_out")),
                    "error": sum(1 for r in stream_recs if r.get("error")),
                    "infra": sum(1 for r in stream_recs if r.get("infra_error")),
                },
                "holdout_failures": {
                    "timeout": sum(1 for r in holdout_recs if r.get("timed_out")),
                    "error": sum(1 for r in holdout_recs if r.get("error")),
                    "infra": sum(1 for r in holdout_recs if r.get("infra_error")),
                },
                "promotion": promo,
                "holdout_records": holdout_recs,
                "stream_records": stream_recs,
            }
            entry["tokens_M"] = round((entry["stream_tokens_M"] + entry["holdout_tokens_M"]), 2)
            entry["latency_s"] = round(entry["stream_latency_s"] + entry["holdout_latency_s"], 1)
            rounds_out.append(entry)
            ckpt = RESULTS_DIR / f"checkpoint-{batch_tag}-{arm}-r{rnd}.json"
            ckpt.write_text(json.dumps(entry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            # Reflexion 式失败反思（移植闭环第 5 个设计）：本轮失败聚合为教训写入
            # 该 arm 的 HOME 记忆目录，下一轮可召回。默认关闭，供 A/B 对照。
            if os.environ.get("EVOHARNESS_REFLECTION", "0").strip() in {"1", "true", "on"}:
                try:
                    from agents.reflection import FailureEvent, derive_lessons, lesson_to_memory
                    # checkpoint 里的失败计数形如 {"timeout": 0, "error": 3, "infra": 0}
                    _kind_map = {"timeout": "timeout", "error": "tool-error", "infra": "infra"}
                    _events = []
                    for _bucket in ("stream_failures", "holdout_failures"):
                        for _raw_kind, _count in (entry.get(_bucket) or {}).items():
                            _kind = _kind_map.get(str(_raw_kind), "other")
                            for _i in range(int(_count or 0)):
                                _events.append(FailureEvent(
                                    f"{arm}-r{rnd}-{_bucket}-{_raw_kind}-{_i}", _kind, arm))
                    _lessons = derive_lessons(_events)
                    _mem_dir = ws_root / arm / "home" / ".evoharness" / "memories"
                    _mem_dir.mkdir(parents=True, exist_ok=True)
                    _written = []
                    for _lesson in _lessons:
                        _payload = lesson_to_memory(_lesson)
                        (_mem_dir / _payload["filename"]).write_text(
                            _payload["content"], encoding="utf-8")
                        _written.append(_payload["filename"])
                    entry["reflection_lessons"] = _written
                    if _written:
                        ckpt.write_text(json.dumps(entry, ensure_ascii=False, indent=2) + "\n",
                                        encoding="utf-8")
                        print(f"[{arm} r{rnd}] reflection lessons written: {_written}", flush=True)
                except Exception as _error:  # noqa: BLE001
                    # 反思是增强层，绝不能拖垮实验；但要留痕，不静默
                    entry["reflection_error"] = f"{type(_error).__name__}: {_error}"[:200]
                    print(f"[{arm} r{rnd}] reflection failed: {entry['reflection_error']}", flush=True)
            if not holdout_readonly_ok:
                failed = {
                    "status": "failed",
                    "reason": "holdout read-only isolation violated: Skill/usage/provenance/status/tier hash changed",
                    "arm": arm, "round": rnd,
                    "before": original_before, "after": original_after,
                }
                (RESULTS_DIR / f"curve-{batch_tag}.json").write_text(json.dumps(failed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                print(json.dumps(failed, ensure_ascii=False, indent=2))
                return 4
            print(
                f"[{arm} r{rnd}] holdout_pass={entry['correct_flags']} skills={skill_count} "
                f"(+{entry['new_skills']}, evolved_tasks={entry['stream_tasks_that_evolved']}) tokens_M={entry['tokens_M']} side_query_tokens_M={entry['side_query_tokens_M']}",
                flush=True,
            )

    curve = curve_from_rounds(rounds_out, seed=args.seed)
    curve["simulated_user_feedback"] = True
    curve["arms"] = arms
    curve["families"] = bool(args.families)
    curve["split_manifest"] = (
        {
            "path": str(split_manifest_path),
            "sha256": split_manifest.get("manifestSha256"),
            "revision": split_manifest.get("revision"),
            "counts": split_manifest.get("counts"),
        }
        if split_manifest
        else None
    )
    curve["arms_manifest"] = arms_manifest
    curve["contamination"] = {
        "sha256": contamination.get("sha256"),
        "taskIds": contamination.get("taskIds"),
        "families": contamination.get("families"),
    }
    curve["holdout_readonly_ok_by_arm"] = {
        a: all(e["holdout_readonly_ok"] for e in rounds_out if e["arm"] == a) for a in arms
    }
    curve["evolution_fired_by_arm"] = {a: any(e["evolution_fired"] for e in rounds_out if e["arm"] == a) for a in arms}
    curve["new_skills_by_arm"] = {a: max((e["new_skills"] for e in rounds_out if e["arm"] == a), default=0) for a in arms}
    curve["total_tokens_M"] = round(sum(e["tokens_M"] for e in rounds_out), 2)
    curve["side_query_tokens_M"] = round(sum(float(e.get("side_query_tokens_M", 0.0) or 0.0) for e in rounds_out), 4)
    curve["note"] = "pilot；模拟用户反馈；n 很小，pass@1 仅供管线校验，不得当作进化效应"
    (RESULTS_DIR / f"curve-{batch_tag}.json").write_text(json.dumps(curve, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # curve.json 只作"最新指针"（项目既有约定：summary 不覆盖原始，这里同理保留归档）
    (RESULTS_DIR / "curve.json").write_text(json.dumps(curve, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: curve[k] for k in ("evolution_fired_by_arm", "new_skills_by_arm", "total_tokens_M", "side_query_tokens_M", "points")}, ensure_ascii=False, indent=2))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="E-04 纵向进化实验（协议 + 骨架 + 真实运行）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("plan", help="离线生成划分与实验计划（零模型）")
    p.add_argument("--dataset", choices=["gaia", "hle"], default="gaia")
    p.add_argument("--rounds", type=int, default=12)
    p.add_argument("--tasks-per-round", type=int, default=25)
    p.add_argument("--holdout", type=int, default=25)
    p.add_argument("--seed", type=int, default=20260119)
    p.set_defaults(func=_cmd_plan)

    w = sub.add_parser("worker", help="内部：单任务 worker（真实模型，供 run 子进程调用）")
    w.add_argument("--item-json", required=True)
    w.add_argument("--dataset", choices=["gaia", "hle"], default="gaia")
    w.add_argument("--max-turns", type=int, default=30)
    w.add_argument("--timeout", type=int, default=600)
    w.add_argument("--evolve", action="store_true", help="以模拟反馈为 hint 触发在线沉淀（extract_now）")
    w.add_argument("--out", default=None, help="把结果 JSON 写到该文件（父进程优先读文件，避免 stdout 噪声）")
    w.set_defaults(func=_cmd_worker)

    r = sub.add_parser("run", help="真实模型运行：三组对照（需 API 预算）")
    r.add_argument("--dataset", choices=["gaia", "hle"], default="gaia")
    r.add_argument("--task-ids-file", default="", help="只用给定的 task_id 子集（校准后只跑有区分度的题）")
    r.add_argument("--rounds", type=int, default=2)
    r.add_argument("--tasks-per-round", type=int, default=2)
    r.add_argument("--holdout", type=int, default=2)
    r.add_argument("--arms", default=",".join(ARMS))
    r.add_argument("--max-turns", type=int, default=30)
    r.add_argument("--timeout", type=int, default=600)
    r.add_argument("--seed", type=int, default=20260119)
    r.add_argument("--workspace", default=None)
    r.add_argument("--no-families", dest="families", action="store_false", help="关闭任务家族化划分（回到按题划分）")
    r.add_argument("--stream-families", type=int, default=2, help="用作任务流的整族数量")
    r.add_argument("--stream-offset", type=int, default=0, help="任务流批次起点偏移（复用同一 workspace 做累积检查用）")
    r.add_argument("--gated-promotion", action="store_true", help="gated arm 是否执行部署晋升门（离线评测+apply_eval_promotions）")
    r.add_argument("--split-manifest", default=None, help="冻结划分 manifest 路径；默认 eval/runs/evolution/split-manifest-<dataset>.json")
    r.add_argument("--contamination", default=None, help="污染登记路径；默认 eval/runs/evolution/contamination.json")
    r.add_argument("--promotion-size", type=int, default=0, help="promotion-test 题数；0 表示与 holdout 相同")
    r.add_argument("--dev-ratio", type=float, default=0.2, help="train 族内划给 dev 的比例（只读失败用于变体改写）")
    r.add_argument("--dup-threshold", type=float, default=DEFAULT_DUP_THRESHOLD, help="近重复判定阈值")
    r.set_defaults(func=_cmd_run, families=True)

    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
