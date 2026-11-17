"""DE-32 有界单 Module 编辑与拒绝历史（搜索增强 S1 第四步）。

对应 SKILL-EVOLUTION-SEARCH §9（有界单 Module 变异）与
NATIVE-ARCHITECTURE-INTEGRATION §6（候选提案策略边界）/§7（evolve_skill_file 行）。

核心契约（全部 fail-closed，拒绝时零副作用）：

- 模型只产出**结构化编辑操作**（``insert/delete/replace``，每条带 span+预期原文），
  程序确定性合并出完整候选制品（复用 DE-29 ``modules.apply_module_edit``）；
  禁止 fuzzy apply：锚点/预期原文不精确匹配即拒绝；patch 还必须绑定
  ``base_text_sha256``（父版本字节 hash），种子版旧行号不能改子版本（§5.1）。
- 编辑预算：操作数 / 增删字节 / 估算 token / 相对父正文的单轮比例 / 相对种子的
  累计增长全部设上限；上限有**硬顶**（``freeze_budget`` 把调用方传入值夹到硬顶），
  单轮改动再小，累计超冻结上限也挡（阈值是待试点标定的配置，不编造最佳值）。
- 保护面：只允许改一个目标 Module（其余 Module 区段由合并器保证字节不变）；
  frontmatter 身份 / IR 区（Decision Rules / Open Questions）/ 包装器区
  （Evolution Notes / Creation Evidence）/ 工具依赖声明 / 辅助脚本一律不可触碰；
  考卷内容（Case 题面/答案/case_id）出现在 patch 新增文本中 → 拒绝（§8/§9：
  不得把真值中的具体题目/答案写入 SKILL.md）。
- 拒绝历史：按 Module 保存有界拒绝 diff+原因（``RejectionHistory``，可落盘搜索
  命名空间）；同一被拒 patch（``patch_sha256+scope`` 去重）不重复存储，重复提交
  → ``previously_rejected`` 早退，不给无意义重复消耗预算的机会。
- S3 制品一致门：受信包装器（Evolution Notes）的最终包装**先渲染、再评测**——
  ``package_candidate`` 产出最终交付 hash；``assert_evaluated_artifact_is_delivered``
  拒绝"评测 A、交付 B"（含先测裸候选、再带未测包装发布的情形）；评测后再追加
  内容 → hash 变化 → DE-18 晋级门（``skill_tiers.apply_eval_promotions`` 的
  ``active_snapshot_hash`` 比对）拒绝晋级。业务编辑 diff 与包装器审计 diff 分开
  返回、分开记录。
- 策略边界（§6/NATIVE §6）：``route_candidate_strategy`` 保留原
  heuristic/LLM/trigger 路由（旧调用兼容，永不劫持）；新编辑策略默认关闭、有
  独立开关（``MutationPolicy.enable_bounded_module_edit``）。新 patch 只写搜索
  命名空间隔离制品（``save_candidate_text`` 走 DE-29 冻结只读守卫），本模块
  **绝不调用 ``evolve_skill_file`` 改 active、不注册生产 tier**；投影重建与普通
  在线沉淀不经本门——单 Module 门只作用于搜索候选编辑路径。

真实模型能力实验（预算/审核真值/隔离执行环境）另行 blockedOn，见任务 DE-32；
提示词契约见 ``prompts/mutation.md``（真实模型路径，合成测试不依赖）。
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..frontmatter import parse_frontmatter
from .modules import (
    ModuleSpec,
    apply_module_edit,
    compute_spec_hash,
    frontmatter_boundary_of,
    module_section_text,
)
from .contracts import SearchManifest, write_candidate_artifact  # noqa: F401 (再导出便于调用方)

MUTATOR_SCHEMA_VERSION = 1
MUTATION_PROMPT_NAME = "mutation.md"
MUTATION_PROMPT_VERSION = 1
PATCH_SCHEMA_VERSION = 1

OP_INSERT = "insert"
OP_DELETE = "delete"
OP_REPLACE = "replace"
OP_TYPES = (OP_INSERT, OP_DELETE, OP_REPLACE)

# 包装器受信区（§5.2：Evolution Notes / Creation Evidence / Evidence IR 由受信
# 包装器生成，不作为可自由变异正文）与 IR 区（online_skill_evolution 的 M2→M3
# 区格式契约）。patch 新增文本出现这些区头 → 拒绝；op span 覆盖既有区头 → 拒绝。
WRAPPER_SECTION_MARKERS = ("## Evolution Notes", "## Creation Evidence")
IR_SECTION_MARKERS = ("## Decision Rules", "## Open Questions")
# 工具/权限依赖声明模式（新增文本子串命中即拒绝）。权威边界仍是冻结的
# frontmatter allowed-tools 与 manifest toolset（bundle 组成变化即新 bundle）；
# 本清单是 patch 层的第一道确定性防线。
TOOL_DEPENDENCY_PATTERNS = ("allowed-tools", "allowed_tools", "工具声明", "工具权限", "权限声明")
FRONTMATTER_FENCE = "---"

# 考卷内容防硬编码扫描阈值：与 feedback.scan_sealed_content 同口径（短样本跳过
# 防误报）；case_id 阈值另设（id 常短于题面）。
EXAM_MATERIAL_MIN_CHARS = 8
CASE_ID_MIN_CHARS = 6

# ── 编辑预算：配置值 + 硬顶（freeze_budget 夹紧，不可绕过） ─────────────────

HARD_MAX_OPS = 16
HARD_MAX_ROUND_ADDED_BYTES = 4000
HARD_MAX_ROUND_DELETED_BYTES = 4000
HARD_MAX_ROUND_ESTIMATED_TOKENS = 1200
HARD_MAX_ROUND_GROWTH_RATIO = 0.5
HARD_MAX_CUMULATIVE_ADDED_BYTES = 8000
HARD_MAX_CUMULATIVE_GROWTH_RATIO = 0.8
HARD_MAX_REJECTION_DIFF_CHARS = 2000

# 拒绝原因码（登记词表，测试只引用登记内码）
REJ_STRATEGY_DISABLED = "strategy_disabled"
REJ_PATCH_SCHEMA = "patch_schema_invalid"
REJ_BASE_MISMATCH = "base_text_sha256_mismatch"
REJ_MODULE_NOT_FOUND = "module_not_found"
REJ_TARGET_MISMATCH = "target_module_mismatch"
REJ_SECOND_MODULE = "second_module_attempt"
REJ_OP_TYPE = "op_type_invalid"
REJ_OP_SPAN = "op_span_invalid"
REJ_OP_NOOP = "op_noop"
REJ_ANCHOR_MISMATCH = "anchor_mismatch"
REJ_OP_OVERLAP = "op_overlap"
REJ_READONLY_MODULE = "readonly_module"
REJ_FRONTMATTER_MODIFIED = "frontmatter_modified"
REJ_FRONTMATTER_ATTEMPT = "frontmatter_edit_attempt"
REJ_WRAPPER_SECTION = "wrapper_section_protected"
REJ_IR_SECTION = "ir_section_protected"
REJ_TOOL_DEPENDENCY = "tool_dependency_edit"
REJ_EXAM_ANSWER = "exam_answer_hardcoded"
REJ_CASE_ID = "case_id_hardcoded"
REJ_BUDGET_EXCEEDED = "edit_budget_exceeded"
REJ_CUMULATIVE_EXCEEDED = "cumulative_growth_exceeded"
REJ_PREVIOUSLY_REJECTED = "previously_rejected"
REJ_SPEC_HASH = "spec_hash_mismatch"
REJ_FROZEN_SOURCE = "frozen_source_readonly"
REJ_EVALUATION_MISMATCH = "evaluation_artifact_mismatch"
REJ_EVALUATION_UNBOUND = "evaluation_not_bound_to_artifact"
REJ_UNKNOWN_STRATEGY = "unknown_strategy"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def estimate_tokens(text: str) -> int:
    """确定性 token 估算（utf-8 字节 / 4 向上取整）；用于预算上限，不冒称精确分词。"""
    return max(1, (len(str(text or "").encode("utf-8")) + 3) // 4)


@dataclass
class EditBudget:
    """编辑预算（SKILL-EVOLUTION-SEARCH §9：阈值为待试点标定的配置）。"""

    max_ops: int = 8
    max_round_added_bytes: int = 1200
    max_round_deleted_bytes: int = 1200
    max_round_estimated_tokens: int = 400
    max_round_growth_ratio: float = 0.3
    max_cumulative_added_bytes: int = 4000
    max_cumulative_growth_ratio: float = 0.6
    rejection_diff_max_chars: int = 600
    clamped_fields: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_ops": self.max_ops,
            "max_round_added_bytes": self.max_round_added_bytes,
            "max_round_deleted_bytes": self.max_round_deleted_bytes,
            "max_round_estimated_tokens": self.max_round_estimated_tokens,
            "max_round_growth_ratio": self.max_round_growth_ratio,
            "max_cumulative_added_bytes": self.max_cumulative_added_bytes,
            "max_cumulative_growth_ratio": self.max_cumulative_growth_ratio,
            "rejection_diff_max_chars": self.rejection_diff_max_chars,
            "hard_caps": {
                "max_ops": HARD_MAX_OPS,
                "max_round_added_bytes": HARD_MAX_ROUND_ADDED_BYTES,
                "max_round_deleted_bytes": HARD_MAX_ROUND_DELETED_BYTES,
                "max_round_estimated_tokens": HARD_MAX_ROUND_ESTIMATED_TOKENS,
                "max_round_growth_ratio": HARD_MAX_ROUND_GROWTH_RATIO,
                "max_cumulative_added_bytes": HARD_MAX_CUMULATIVE_ADDED_BYTES,
                "max_cumulative_growth_ratio": HARD_MAX_CUMULATIVE_GROWTH_RATIO,
                "rejection_diff_max_chars": HARD_MAX_REJECTION_DIFF_CHARS,
            },
            "clamped_fields": list(self.clamped_fields),
            "frozen": True,
        }


_BUDGET_INT_CAPS = {
    "max_ops": HARD_MAX_OPS,
    "max_round_added_bytes": HARD_MAX_ROUND_ADDED_BYTES,
    "max_round_deleted_bytes": HARD_MAX_ROUND_DELETED_BYTES,
    "max_round_estimated_tokens": HARD_MAX_ROUND_ESTIMATED_TOKENS,
    "max_cumulative_added_bytes": HARD_MAX_CUMULATIVE_ADDED_BYTES,
    "rejection_diff_max_chars": HARD_MAX_REJECTION_DIFF_CHARS,
}
_BUDGET_FLOAT_CAPS = {
    "max_round_growth_ratio": HARD_MAX_ROUND_GROWTH_RATIO,
    "max_cumulative_growth_ratio": HARD_MAX_CUMULATIVE_GROWTH_RATIO,
}


def freeze_budget(**overrides: Any) -> EditBudget:
    """构造冻结预算：任何传入值超过硬顶一律夹到硬顶（``clamped_fields`` 留痕）。

    调用方放大上限不可绕过硬顶——单轮小改、累计超限同样被挡。
    """
    defaults = EditBudget()
    clamped: list[str] = []
    values: dict[str, Any] = {}
    for name, cap in _BUDGET_INT_CAPS.items():
        raw = overrides.get(name, getattr(defaults, name))
        try:
            num = int(raw)
        except (TypeError, ValueError):
            num = getattr(defaults, name)
        if num < 1:
            num = getattr(defaults, name)
        if num > cap:
            num = cap
            clamped.append(name)
        values[name] = num
    for name, cap in _BUDGET_FLOAT_CAPS.items():
        raw = overrides.get(name, getattr(defaults, name))
        try:
            num = float(raw)
        except (TypeError, ValueError):
            num = getattr(defaults, name)
        if num <= 0.0:
            num = getattr(defaults, name)
        if num > cap:
            num = cap
            clamped.append(name)
        values[name] = num
    return EditBudget(**values, clamped_fields=tuple(clamped))


@dataclass
class MutationPolicy:
    """候选提案策略开关（新优化器默认关闭，独立开关；§1/NATIVE §6）。"""

    enable_bounded_module_edit: bool = False
    strategy_id: str = "bounded-module-edit/v1"


LEGACY_PROPOSAL_STRATEGIES = ("heuristic", "llm", "trigger")
BOUNDED_MODULE_PATCH_STRATEGIES = ("bounded_module_patch", "module_patch", "search_patch")


def route_candidate_strategy(policy: MutationPolicy | None, requested: str) -> dict[str, Any]:
    """候选提案策略路由：原 heuristic/LLM/trigger 轨道原样放行（旧调用兼容）。

    新编辑策略（bounded_module_patch）只在 ``policy.enable_bounded_module_edit``
    开启时路由到搜索路径；关闭时返回 ``strategy_disabled`` 并给出 legacy 回退，
    不抛错、不产出 patch（S4：关闭后原候选/trigger 回归保持）。
    """
    req = str(requested or "").strip().lower() or "heuristic"
    if req in LEGACY_PROPOSAL_STRATEGIES:
        return {"ok": True, "route": "legacy", "strategy": req, "bounded_module_edit": False}
    if req in BOUNDED_MODULE_PATCH_STRATEGIES:
        if policy is None or not policy.enable_bounded_module_edit:
            return {
                "ok": False,
                "error": REJ_STRATEGY_DISABLED,
                "route": "legacy",
                "fallback": "heuristic",
                "reason": "新编辑策略默认关闭（独立开关）；关闭时原 heuristic/LLM/trigger 候选流程保持",
            }
        return {"ok": True, "route": "search", "strategy": "bounded_module_patch", "bounded_module_edit": True}
    return {"ok": False, "error": REJ_UNKNOWN_STRATEGY, "route": "", "strategy": req}


def make_feedback_mutator(
    policy: MutationPolicy | None, apply_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """给 DE-31 ``compile_feedback(mutator=...)`` 的适配器（变异门放行后才被调用）。

    - 策略关闭 → ``{"invoked": False, "error": strategy_disabled}``：简报不触发
      变异，原局部候选流程保持（不做任何写动作）；
    - 开启但未配置 patch 生成器 → fail-closed 拒绝，不产出候选；
    - ``apply_fn`` 是注入的 patch 生成/应用函数（真实模型路径或合成测试）。
    """

    def _mutator(brief: dict[str, Any]) -> dict[str, Any]:
        if policy is None or not policy.enable_bounded_module_edit:
            return {
                "invoked": False,
                "ok": False,
                "error": REJ_STRATEGY_DISABLED,
                "note": "新编辑策略关闭：DE-31 简报不触发变异，原局部候选流程保持",
            }
        if apply_fn is None:
            return {
                "invoked": True,
                "ok": False,
                "error": "no_patch_builder",
                "note": "未配置 patch 生成器：fail-closed，不产生候选",
            }
        result = apply_fn(brief)
        if isinstance(result, dict):
            return {"invoked": True, **result}
        return {"invoked": True, "value": str(result)}

    return _mutator


# ── patch 规范化 / 精确合并 ─────────────────────────────────────────────────


def normalize_patch(raw: Any) -> dict[str, Any]:
    """结构校验一个候选 patch（不校验语义锚点，那是合并阶段的事）。"""
    if not isinstance(raw, dict):
        return {"ok": False, "error": REJ_PATCH_SCHEMA, "reason": "patch 必须是 JSON 对象"}
    target = str(raw.get("target_module") or "").strip()
    if not target:
        return {"ok": False, "error": REJ_PATCH_SCHEMA, "reason": "patch 缺少 target_module"}
    base = str(raw.get("base_text_sha256") or "").strip()
    if not base:
        return {"ok": False, "error": REJ_PATCH_SCHEMA, "reason": "patch 缺少 base_text_sha256（必须绑定父版本字节 hash）"}
    ops_raw = raw.get("ops")
    if not isinstance(ops_raw, list) or not ops_raw:
        return {"ok": False, "error": REJ_PATCH_SCHEMA, "reason": "patch 必须包含至少一个编辑操作"}
    ops: list[dict[str, Any]] = []
    for index, item in enumerate(ops_raw):
        if not isinstance(item, dict):
            return {"ok": False, "error": REJ_PATCH_SCHEMA, "reason": f"op#{index} 必须是对象"}
        op = str(item.get("op") or "").strip().lower()
        if op not in OP_TYPES:
            return {"ok": False, "error": REJ_OP_TYPE, "reason": f"op#{index} 类型非法（允许 insert/delete/replace）: {op or 'empty'}"}
        try:
            start = int(item.get("start"))
            end = int(item.get("end"))
        except (TypeError, ValueError):
            return {"ok": False, "error": REJ_OP_SPAN, "reason": f"op#{index} 缺少整数 start/end"}
        expected = item.get("expected")
        replacement = item.get("replacement")
        if not isinstance(expected, str) or not isinstance(replacement, str):
            return {"ok": False, "error": REJ_PATCH_SCHEMA, "reason": f"op#{index} 的 expected/replacement 必须是字符串"}
        module = str(item.get("module") or "").strip()
        ops.append({"op": op, "start": start, "end": end, "expected": expected, "replacement": replacement, "module": module})
    return {
        "ok": True,
        "patch": {
            "schema_version": PATCH_SCHEMA_VERSION,
            "target_module": target,
            "base_text_sha256": base,
            "ops": ops,
            "rationale": str(raw.get("rationale") or ""),
        },
    }


def patch_key(patch: dict[str, Any], *, scope: str = "") -> str:
    """patch 去重键（§9：重复内容按身份去重）：目标+操作+父版本绑定+协议范围。"""
    return _canonical_sha256(
        {
            "target_module": patch.get("target_module") or "",
            "base_text_sha256": patch.get("base_text_sha256") or "",
            "ops": patch.get("ops") or [],
            "scope": str(scope),
        }
    )


def render_patch_diff(patch: dict[str, Any], *, max_chars: int = 600) -> str:
    """有界 diff 渲染（拒绝历史/审计用）：逐操作 expected -> replacement，超长截断。"""
    lines: list[str] = []
    for op in patch.get("ops") or []:
        lines.append(
            f"{op.get('op')} [{op.get('start')}:{op.get('end')}] "
            f"{str(op.get('expected'))!r} -> {str(op.get('replacement'))!r}"
        )
    diff = "\n".join(lines)
    limit = max(1, int(max_chars))
    if len(diff) <= limit:
        return diff
    return diff[:limit] + "...[truncated]"


# ── 拒绝历史（按 Module 有界保存；bundle/protocol scope 去重） ───────────────


@dataclass
class RejectionEntry:
    module_id: str
    reason: str
    detail: str
    patch_sha256: str
    diff: str
    scope: str = ""
    first_seen_at: str = ""
    last_seen_at: str = ""
    seen_count: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "module_id": self.module_id,
            "reason": self.reason,
            "detail": self.detail,
            "patch_sha256": self.patch_sha256,
            "diff": self.diff,
            "scope": self.scope,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "seen_count": self.seen_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RejectionEntry":
        return cls(
            module_id=str(data.get("module_id") or ""),
            reason=str(data.get("reason") or ""),
            detail=str(data.get("detail") or ""),
            patch_sha256=str(data.get("patch_sha256") or ""),
            diff=str(data.get("diff") or ""),
            scope=str(data.get("scope") or ""),
            first_seen_at=str(data.get("first_seen_at") or ""),
            last_seen_at=str(data.get("last_seen_at") or ""),
            seen_count=int(data.get("seen_count") or 1),
        )


@dataclass
class RejectionHistory:
    """按 Module 保存的有界拒绝历史：相同被拒 patch 不重复存储，只累计 seen_count。"""

    entries: list[RejectionEntry] = field(default_factory=list)
    max_per_module: int = 20

    def find(self, sha: str, *, scope: str = "") -> RejectionEntry | None:
        for entry in self.entries:
            if entry.patch_sha256 == sha and entry.scope == scope:
                return entry
        return None

    def touch(self, entry: RejectionEntry) -> None:
        entry.seen_count += 1
        entry.last_seen_at = _utc_now()

    def record(
        self,
        *,
        module_id: str,
        reason: str,
        detail: str,
        patch_sha256: str,
        diff: str,
        scope: str = "",
        max_diff_chars: int = 600,
    ) -> dict[str, Any]:
        """登记一次拒绝：重复 patch 只累加计数不重复存储；每 Module 有界（保留最新）。"""
        now = _utc_now()
        existing = self.find(patch_sha256, scope=scope)
        if existing is not None:
            self.touch(existing)
            return {"stored": False, "duplicate": True, "seen_count": existing.seen_count, "entry": existing.to_dict()}
        entry = RejectionEntry(
            module_id=module_id,
            reason=reason,
            detail=str(detail or "")[:400],
            patch_sha256=patch_sha256,
            diff=diff[: max(1, int(max_diff_chars))],
            scope=scope,
            first_seen_at=now,
            last_seen_at=now,
            seen_count=1,
        )
        self.entries.append(entry)
        same_module = [e for e in self.entries if e.module_id == module_id]
        overflow = len(same_module) - max(1, int(self.max_per_module))
        if overflow > 0:
            drop_ids = {id(e) for e in same_module[:overflow]}
            self.entries = [e for e in self.entries if id(e) not in drop_ids]
        return {"stored": True, "duplicate": False, "seen_count": 1, "entry": entry.to_dict()}

    def rejections_for(self, module_id: str) -> list[RejectionEntry]:
        return [e for e in self.entries if e.module_id == module_id]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": MUTATOR_SCHEMA_VERSION,
            "max_per_module": self.max_per_module,
            "entries": [e.to_dict() for e in self.entries],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RejectionHistory":
        return cls(
            entries=[RejectionEntry.from_dict(item) for item in (data.get("entries") or []) if isinstance(item, dict)],
            max_per_module=int(data.get("max_per_module") or 20),
        )


def save_rejection_history(history: RejectionHistory, path: str) -> dict[str, Any]:
    payload = json.dumps(history.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload, encoding="utf-8")
    return {"ok": True, "path": str(target), "bytes": len(payload.encode("utf-8"))}


def load_rejection_history(path: str) -> RejectionHistory:
    text = Path(path).read_text(encoding="utf-8")
    return RejectionHistory.from_dict(json.loads(text))


# ── 累计谱系计数（相对种子的跨代累计增长） ───────────────────────────────────


def initial_cumulative(seed_text: str) -> dict[str, Any]:
    """种子代累计计数器：随候选谱系逐代传递（``apply_candidate_patch`` 返回子代值）。"""
    boundary, _meta = frontmatter_boundary_of(seed_text)
    return {
        "seed_text_sha256": _sha256_text(seed_text),
        "seed_body_bytes": len(seed_text[boundary:].encode("utf-8")),
        "added_bytes": 0,
        "ops": 0,
        "generations": 0,
    }


# ── 保护面扫描（考卷/包装器/IR/工具依赖/frontmatter 注入） ───────────────────


def _scan_forbidden_new_text(text: str) -> list[dict[str, str]]:
    """扫描 patch 新增文本：命中即拒绝（返回 [{code, hit}]，全部登记码）。"""
    hits: list[dict[str, str]] = []
    lowered = text.lower()
    for marker in WRAPPER_SECTION_MARKERS:
        if any(line.strip() == marker for line in text.split("\n")):
            hits.append({"code": REJ_WRAPPER_SECTION, "hit": marker})
    for marker in IR_SECTION_MARKERS:
        if any(line.strip() == marker for line in text.split("\n")):
            hits.append({"code": REJ_IR_SECTION, "hit": marker})
    for pattern in TOOL_DEPENDENCY_PATTERNS:
        if pattern.lower() in lowered:
            hits.append({"code": REJ_TOOL_DEPENDENCY, "hit": pattern})
    if any(line.strip() == FRONTMATTER_FENCE for line in text.split("\n")):
        hits.append({"code": REJ_FRONTMATTER_ATTEMPT, "hit": FRONTMATTER_FENCE})
    return hits


def _scan_exam_materials(text: str, exam_materials: list[str] | None, case_ids: list[str] | None) -> list[dict[str, str]]:
    hits: list[dict[str, str]] = []
    for material in exam_materials or []:
        token = str(material or "").strip()
        if len(token) >= EXAM_MATERIAL_MIN_CHARS and token in text:
            hits.append({"code": REJ_EXAM_ANSWER, "hit": token})
    for case_id in case_ids or []:
        token = str(case_id or "").strip()
        if len(token) >= CASE_ID_MIN_CHARS and token in text:
            hits.append({"code": REJ_CASE_ID, "hit": token})
    return hits


def _protected_marker_spans(section_text: str) -> list[tuple[int, int, str]]:
    """既有正文里受信包装器/IR 区头的字符区间（op span 覆盖即拒绝）。"""
    spans: list[tuple[int, int, str]] = []
    offset = 0
    for line in section_text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped in WRAPPER_SECTION_MARKERS:
            spans.append((offset, offset + len(line), stripped))
        elif stripped in IR_SECTION_MARKERS:
            spans.append((offset, offset + len(line), stripped))
        offset += len(line)
    return spans


# ── 主入口：有界单 Module patch 应用 ────────────────────────────────────────


def apply_candidate_patch(
    spec: ModuleSpec,
    raw_text: str,
    patch: Any,
    *,
    policy: MutationPolicy | None = None,
    budget: EditBudget | None = None,
    gate_target_module: str | None = None,
    skill_text_sha256: str | None = None,
    exam_materials: list[str] | None = None,
    case_ids: list[str] | None = None,
    cumulative: dict[str, Any] | None = None,
    rejection_history: RejectionHistory | None = None,
    scope: str = "",
) -> dict[str, Any]:
    """应用一个结构化单 Module patch：校验 → 精确合并 → 返回完整候选（不写盘）。

    返回 ``{"ok": True, candidate_text, child_spec, business_diff, cumulative, ...}``
    或 ``{"ok": False, error/reason, ...}``；拒绝时（目标 Module 可归因时）登记
    进 ``rejection_history``，同一被拒 patch 重复提交 → ``previously_rejected``。
    绝不写生产 Skill：候选文本由调用方经 ``save_candidate_text`` 写搜索命名空间。
    """
    resolved_budget = budget or freeze_budget()
    state: dict[str, Any] = {"patch": None}

    def fail(code: str, reason: str, module_id: str = "", patch_sha: str = "", diff: str | None = None, **detail: Any) -> dict[str, Any]:
        if diff is None and state["patch"] is not None:
            diff = render_patch_diff(state["patch"], max_chars=resolved_budget.rejection_diff_max_chars)
        if rejection_history is not None and module_id:
            recorded = rejection_history.record(
                module_id=module_id,
                reason=code,
                detail=reason,
                patch_sha256=patch_sha,
                diff=diff or "",
                scope=scope,
                max_diff_chars=resolved_budget.rejection_diff_max_chars,
            )
            detail["recorded"] = recorded
        return {"ok": False, "error": code, "reason": reason, **detail}

    # 0. 策略开关：关闭 → 不产生 patch（不给 reason 记历史，未尝试变异）
    if policy is not None and not policy.enable_bounded_module_edit:
        return {
            "ok": False,
            "error": REJ_STRATEGY_DISABLED,
            "reason": "新编辑策略关闭（MutationPolicy.enable_bounded_module_edit=False）：原 heuristic/LLM/trigger 候选流程保持",
        }

    # 1. 结构校验
    normalized = normalize_patch(patch)
    if not normalized.get("ok"):
        return {"ok": False, "error": normalized.get("error") or REJ_PATCH_SCHEMA, "reason": normalized.get("reason") or "patch 结构非法"}
    patch = normalized["patch"]
    state["patch"] = patch
    target = patch["target_module"]
    sha = patch_key(patch, scope=scope)

    # 2. 变异门目标一致（DE-31 简报 target_module 与 patch 目标必须同 Module）
    if gate_target_module and target != gate_target_module:
        return fail(
            REJ_TARGET_MISMATCH,
            f"patch 目标 {target} 与变异门目标 {gate_target_module} 不一致：一次只允许改一个目标 Module",
            module_id=target,
            patch_sha=sha,
        )

    # 3. 重复被拒 patch 早退（防无意义重复提交消耗预算）
    if rejection_history is not None:
        existing = rejection_history.find(sha, scope=scope)
        if existing is not None:
            rejection_history.touch(existing)
            return {
                "ok": False,
                "error": REJ_PREVIOUSLY_REJECTED,
                "reason": f"该 patch 曾被拒绝（{existing.reason}），不重复提交：{existing.detail}",
                "first_reason": existing.reason,
                "seen_count": existing.seen_count,
                "patch_sha256": sha,
            }

    # 4. 父版本绑定：种子版旧行号不能改子版本（§5.1）
    actual_base = _sha256_text(raw_text)
    if patch["base_text_sha256"] != actual_base:
        return fail(
            REJ_BASE_MISMATCH,
            "patch 绑定的父版本 hash 与当前文本不符（陈旧 patch / 外部编辑）",
            module_id=target,
            patch_sha=sha,
            expected_sha256=patch["base_text_sha256"],
            actual_sha256=actual_base,
        )

    # 5. spec 完整性 + Module 存在
    if compute_spec_hash(spec) != spec.approved_spec_hash:
        return fail(REJ_SPEC_HASH, "ModuleSpec 校验 hash 与内容不符（陈旧/被篡改）", module_id=target, patch_sha=sha)
    item = spec.module(target)
    if item is None:
        return fail(REJ_MODULE_NOT_FOUND, f"目标 Module 不存在: {target}", module_id=target, patch_sha=sha)

    # 6. 操作级 Module 边界：任何 op 指向第二 Module → 拒绝（S1 拒绝面）
    for index, op in enumerate(patch["ops"]):
        if op["module"] and op["module"] != target:
            return fail(
                REJ_SECOND_MODULE,
                f"op#{index} 企图编辑第二 Module {op['module']}：单 Module patch 只允许目标 {target}",
                module_id=target,
                patch_sha=sha,
            )

    # 7. 精确合并（无 fuzzy apply）：逐 op 校验 span+预期原文后程序合并
    section = module_section_text(spec, raw_text, target)
    ordered = sorted(patch["ops"], key=lambda o: (o["start"], o["end"]))
    prev: dict[str, Any] | None = None
    for index, op in enumerate(ordered):
        start, end = op["start"], op["end"]
        if not 0 <= start <= end <= len(section):
            return fail(REJ_OP_SPAN, f"op#{index} span [{start}:{end}] 超出目标 Module 区段（长度 {len(section)}）", module_id=target, patch_sha=sha)
        if prev is not None and start < prev["end"]:
            return fail(REJ_OP_OVERLAP, f"op#{index} 与前一操作区间交叠（禁止模糊/重叠编辑）", module_id=target, patch_sha=sha)
        prev = op
        if section[start:end] != op["expected"]:
            return fail(
                REJ_ANCHOR_MISMATCH,
                f"op#{index} 预期原文与 span 下实际内容不匹配（禁止 fuzzy apply，锚点不匹配即拒绝）",
                module_id=target,
                patch_sha=sha,
            )
        if op["op"] == OP_INSERT and (start != end or op["replacement"] == ""):
            return fail(REJ_OP_NOOP, f"op#{index} insert 必须是空 span 且带非空 replacement", module_id=target, patch_sha=sha)
        if op["op"] == OP_DELETE and op["replacement"] != "":
            return fail(REJ_OP_NOOP, f"op#{index} delete 的 replacement 必须为空", module_id=target, patch_sha=sha)
        if op["op"] == OP_REPLACE and op["replacement"] == op["expected"]:
            return fail(REJ_OP_NOOP, f"op#{index} replace 的 replacement 与 expected 相同（无操作）", module_id=target, patch_sha=sha)
    merged_section = section
    for op in reversed(ordered):
        merged_section = merged_section[: op["start"]] + op["replacement"] + merged_section[op["end"] :]

    # 8. 保护面扫描：受信包装器区 / IR 区 / 工具依赖 / frontmatter 注入 / 考卷内容
    for hit in _scan_forbidden_new_text(merged_section):
        return fail(hit["code"], f"patch 新增文本触碰受保护内容（{hit['hit']}）：仅受信包装器可生成该区", module_id=target, patch_sha=sha)
    for index, op in enumerate(ordered):
        if op["replacement"] == "":
            continue
        for hit in _scan_forbidden_new_text(op["replacement"]):
            return fail(hit["code"], f"op#{index} 新增文本触碰受保护内容（{hit['hit']}）", module_id=target, patch_sha=sha)
    marker_spans = _protected_marker_spans(section)
    for index, op in enumerate(ordered):
        for m_start, m_end, marker in marker_spans:
            if op["start"] < m_end and m_start < op["end"]:
                return fail(
                    REJ_WRAPPER_SECTION if marker in WRAPPER_SECTION_MARKERS else REJ_IR_SECTION,
                    f"op#{index} span 覆盖既有受保护区「{marker}」：受信包装器/IR 区不可编辑",
                    module_id=target,
                    patch_sha=sha,
                )
    for hit in _scan_exam_materials(merged_section, exam_materials, case_ids):
        code = REJ_EXAM_ANSWER if hit["code"] == REJ_EXAM_ANSWER else REJ_CASE_ID
        kind = "考卷题面/答案" if code == REJ_EXAM_ANSWER else "case_id"
        return fail(code, f"patch 新增文本包含{kind}「{hit['hit']}」：不得把真值具体题目/答案写入 SKILL.md", module_id=target, patch_sha=sha)

    # 9. 预算校验（单轮 + 累计；硬顶已由 freeze_budget 夹紧）
    ops = patch["ops"]
    added_gross = sum(len(op["replacement"].encode("utf-8")) for op in ops)
    deleted_gross = sum(len(op["expected"].encode("utf-8")) for op in ops)
    round_added = added_gross - deleted_gross
    module_bytes = len(section.encode("utf-8"))
    if len(ops) > resolved_budget.max_ops:
        return fail(REJ_BUDGET_EXCEEDED, f"操作数 {len(ops)} 超上限 {resolved_budget.max_ops}", module_id=target, patch_sha=sha)
    if added_gross > resolved_budget.max_round_added_bytes:
        return fail(REJ_BUDGET_EXCEEDED, f"单轮新增字节 {added_gross} 超上限 {resolved_budget.max_round_added_bytes}", module_id=target, patch_sha=sha)
    if deleted_gross > resolved_budget.max_round_deleted_bytes:
        return fail(REJ_BUDGET_EXCEEDED, f"单轮删除字节 {deleted_gross} 超上限 {resolved_budget.max_round_deleted_bytes}", module_id=target, patch_sha=sha)
    tokens = sum(estimate_tokens(op["replacement"]) for op in ops)
    if tokens > resolved_budget.max_round_estimated_tokens:
        return fail(REJ_BUDGET_EXCEEDED, f"单轮估算 token {tokens} 超上限 {resolved_budget.max_round_estimated_tokens}", module_id=target, patch_sha=sha)
    if round_added > 0 and module_bytes > 0 and round_added / module_bytes > resolved_budget.max_round_growth_ratio:
        return fail(
            REJ_BUDGET_EXCEEDED,
            f"单轮相对父 Module 净增比例 {round_added / module_bytes:.3f} 超上限 {resolved_budget.max_round_growth_ratio}",
            module_id=target,
            patch_sha=sha,
        )
    cum = dict(cumulative or initial_cumulative(raw_text))
    projected_added = int(cum.get("added_bytes") or 0) + round_added
    seed_body_bytes = int(cum.get("seed_body_bytes") or 0)
    projected_ratio = projected_added / seed_body_bytes if seed_body_bytes > 0 else 0.0
    if projected_added > resolved_budget.max_cumulative_added_bytes:
        return fail(
            REJ_CUMULATIVE_EXCEEDED,
            f"相对种子累计净增 {projected_added} 字节超冻结上限 {resolved_budget.max_cumulative_added_bytes}"
            "（单轮小改但累计超限同样被挡）",
            module_id=target,
            patch_sha=sha,
        )
    if seed_body_bytes > 0 and projected_ratio > resolved_budget.max_cumulative_growth_ratio:
        return fail(
            REJ_CUMULATIVE_EXCEEDED,
            f"相对种子累计增长比例 {projected_ratio:.3f} 超冻结上限 {resolved_budget.max_cumulative_growth_ratio}",
            module_id=target,
            patch_sha=sha,
        )

    # 10. 委托 DE-29 合并器：frontmatter/只读 Module/锚点新鲜度/外部编辑/兄弟 span 全检
    merged = apply_module_edit(
        spec,
        raw_text,
        module_id=target,
        replacement_text=merged_section,
        skill_text_sha256=skill_text_sha256,
    )
    if not merged.get("ok"):
        return fail(
            str(merged.get("error") or "module_merge_failed"),
            str(merged.get("reason") or "合并器拒绝"),
            module_id=target,
            patch_sha=sha,
        )

    child_cumulative = dict(cum)
    child_cumulative["added_bytes"] = projected_added
    child_cumulative["ops"] = int(cum.get("ops") or 0) + len(ops)
    child_cumulative["generations"] = int(cum.get("generations") or 0) + 1
    return {
        "ok": True,
        "candidate_text": merged["candidate_text"],
        "child_spec": merged["child_spec"],
        "changed_module": target,
        "patch_sha256": sha,
        "business_diff": render_patch_diff(patch, max_chars=resolved_budget.rejection_diff_max_chars),
        "round_stats": {"ops": len(ops), "added_bytes": round_added, "deleted_bytes": deleted_gross, "estimated_tokens": tokens},
        "cumulative": child_cumulative,
    }


# ── S3：受信包装器最终包装 + 评测/交付制品一致门 ─────────────────────────────


def package_candidate(
    candidate_text: str,
    *,
    lesson: str,
    rationale: str = "",
) -> dict[str, Any]:
    """渲染受信包装器的最终交付制品（先包装、后评测——S3 契约）。

    - 业务编辑 diff（``apply_candidate_patch`` 返回的 ``business_diff``）与包装器
      审计 diff（``wrapper_body``）**分开记录**；
    - 包装正文只来自受信模板（复用 ``agents.skill_evolution._append_evolution_note``
      ——Evolution Notes 的唯一模板实现，禁止模型自由追加审计文本）；
    - 产出三个 hash：
      * ``final_content_hash``：最终制品 skill_content_hash 口径（= DE-18 晋级门
        ``active_snapshot_hash`` 比对口径）；
      * ``final_bundle_sha256``：最终制品规范化内容 hash（评测绑定用）；
      * ``release_bundle_sha256``：发布 bundle 声明 hash（= ``publish_skill_bundle``
        的 ``bundle["sha256"]`` 校验口径，包装前正文）。
    """
    from ..skill_evolution import _append_evolution_note, skill_content_hash

    if not lesson or not str(lesson).strip():
        return {"ok": False, "error": "wrapper_lesson_required", "reason": "受信包装器需要非空 lesson（审计文本模板参数）"}
    parsed = parse_frontmatter(candidate_text)
    candidate_body = parsed.body
    if not candidate_body.strip():
        return {"ok": False, "error": "candidate_body_empty", "reason": "候选正文为空，无法包装"}
    wrapper_body = _append_evolution_note("", lesson, rationale).strip()
    final_body = candidate_body.rstrip() + "\n\n" + wrapper_body + "\n"
    # frontmatter 区原样保留（身份字段只读），正文替换为最终正文
    lines = candidate_text.split("\n")
    if lines and lines[0].strip() == "---":
        end_idx = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
        prefix = "\n".join(lines[: end_idx + 1]) if end_idx is not None else ""
    else:
        prefix = ""
    final_text = (prefix + "\n" if prefix else "") + final_body

    name = str(parsed.meta.get("name") or "")
    description = str(parsed.meta.get("description") or "")
    when_to_use = str(parsed.meta.get("when-to-use") or parsed.meta.get("when_to_use") or "")
    final_body_stripped = final_body.strip()
    candidate_content_hash = skill_content_hash(
        {"name": name, "description": description, "when_to_use": when_to_use, "instructions": candidate_body}
    )
    final_content_hash = skill_content_hash(
        {"name": name, "description": description, "when_to_use": when_to_use, "instructions": final_body_stripped}
    )
    from evoharness.knowledge.release import _canonical_bundle_hash

    final_bundle_sha256 = _canonical_bundle_hash(
        {"name": name, "description": description, "when_to_use": when_to_use, "instructions": final_body_stripped}
    )
    release_bundle_sha256 = _canonical_bundle_hash(
        {"name": name, "description": description, "when_to_use": when_to_use, "instructions": candidate_body}
    )
    return {
        "ok": True,
        "final_text": final_text,
        "final_body": final_body_stripped,
        "wrapper_body": wrapper_body,
        "wrapper_lesson": str(lesson).strip(),
        "wrapper_rationale": str(rationale or "").strip(),
        "wrapper_diff_chars": len(wrapper_body.encode("utf-8")),
        "candidate_content_hash": candidate_content_hash,
        "final_content_hash": final_content_hash,
        "final_bundle_sha256": final_bundle_sha256,
        "final_text_sha256": _sha256_text(final_text),
        "release_bundle_sha256": release_bundle_sha256,
        "bundle_name": name,
        "bundle_description": description,
        "bundle_when_to_use": when_to_use,
        "bundle_instructions": candidate_body,
        "note": "业务编辑 diff 与包装器审计 diff 分开记录；最终包装先渲染、再评测（S3）",
    }


def build_release_bundle(packaged: dict[str, Any], *, producer_kind: str = "direct") -> dict[str, Any]:
    """从最终包装构造 ``publish_skill_bundle`` 入参（bundle+发布 kwargs 成对返回）。

    发布流（publish_skill_bundle → evolve_skill_file）会以同一受信模板追加
    Evolution Notes，最终正文 = packaged["final_body"]，与被评测制品完全一致。
    """
    if not packaged.get("ok"):
        return {"ok": False, "error": "package_invalid", "reason": "最终包装不存在，无法构造发布 bundle"}
    bundle = {
        "name": str(packaged.get("bundle_name") or ""),
        "description": str(packaged.get("bundle_description") or ""),
        "when_to_use": str(packaged.get("bundle_when_to_use") or ""),
        "instructions": str(packaged.get("bundle_instructions") or ""),
        "producer_kind": producer_kind,
        "sha256": str(packaged.get("release_bundle_sha256") or ""),
    }
    return {
        "ok": True,
        "bundle": bundle,
        "publish_kwargs": {
            "lesson": str(packaged.get("wrapper_lesson") or ""),
            "reason": str(packaged.get("wrapper_rationale") or ""),
        },
    }


def assert_evaluated_artifact_is_delivered(packaged: dict[str, Any], evaluation: dict[str, Any]) -> dict[str, Any]:
    """S3 制品一致门：评测绑定的制品 hash 必须等于最终交付 hash。

    - 评测记录缺 ``evaluated_bundle_sha256``/``evaluated_content_hash`` 绑定 →
      fail-closed 拒绝（沿用 DE-29 ``check_evaluation_reuse`` 的绑定纪律）；
    - 绑定的是包装前裸候选（先测原始子文档再发布带 Notes 的不同制品）→ 拒绝；
    - 通过后，评测针对的制品就是交付制品；评测后再追加内容 → hash 变化 →
      DE-18 晋级门（``apply_eval_promotions``）拒绝。
    """
    if not packaged.get("ok"):
        return {"ok": False, "error": REJ_EVALUATION_MISMATCH, "reason": "最终包装不存在"}
    if not isinstance(evaluation, dict):
        return {"ok": False, "error": REJ_EVALUATION_UNBOUND, "reason": "评测记录缺失，无法证明评测针对交付制品"}
    bound_bundle = str(evaluation.get("evaluated_bundle_sha256") or "")
    bound_content = str(evaluation.get("evaluated_content_hash") or "")
    if not bound_bundle and not bound_content:
        return {
            "ok": False,
            "error": REJ_EVALUATION_UNBOUND,
            "reason": "评测记录未绑定 evaluated_bundle_sha256/evaluated_content_hash：按 S3 契约不可用作晋级证据（fail-closed）",
        }
    if bound_bundle and bound_bundle != str(packaged.get("final_bundle_sha256")):
        return {
            "ok": False,
            "error": REJ_EVALUATION_MISMATCH,
            "reason": "评测绑定的制品 hash 与最终交付 hash 不一致：不得拿旧制品成绩晋级（追加包装后必须重新评测）",
            "evaluated_bundle_sha256": bound_bundle,
            "final_bundle_sha256": str(packaged.get("final_bundle_sha256")),
        }
    if bound_content and bound_content != str(packaged.get("final_content_hash")):
        return {
            "ok": False,
            "error": REJ_EVALUATION_MISMATCH,
            "reason": "评测绑定的内容 hash 与最终交付内容 hash 不一致：不得拿旧制品成绩晋级",
            "evaluated_content_hash": bound_content,
            "final_content_hash": str(packaged.get("final_content_hash")),
        }
    return {"ok": True, "final_content_hash": packaged.get("final_content_hash"), "final_bundle_sha256": packaged.get("final_bundle_sha256")}


# ── 候选产物落盘（只写搜索命名空间；冻结源目录只读守卫内建） ─────────────────


def save_candidate_text(manifest: SearchManifest, candidate_dir: str, packaged: dict[str, Any]) -> dict[str, Any]:
    """最终包装文本写入候选目录（``candidates/<id>/SKILL.md``）。

    目标在冻结源目录内 → ``frozen_source_readonly`` 拒绝——新 patch 只写隔离制品。
    """
    if not packaged.get("ok"):
        return {"ok": False, "error": "package_invalid", "reason": "最终包装不存在，拒绝落盘"}
    return write_candidate_artifact(manifest, candidate_dir, "SKILL.md", str(packaged.get("final_text") or ""))


# ── 提示词契约（真实模型路径；合成测试不依赖） ───────────────────────────────

_MUTATION_FALLBACK_PROMPT = """你是 EvoHarness 搜索增强的单 Module 变异器。只输出严格 JSON。
纪律：只改唯一目标 Module；结构化 insert/delete/replace 编辑（span+预期原文）；
不返回整份可改写的 Skill；不得硬编码考卷题目/答案；不新增工具/权限/依赖；
guardrails、frontmatter、IR 区、Evolution Notes 不可触碰。
"""


def build_mutation_input(
    *,
    spec: ModuleSpec,
    raw_text: str,
    target_module: str,
    brief: dict[str, Any] | None = None,
    rejection_history: RejectionHistory | None = None,
    budget: EditBudget | None = None,
) -> dict[str, Any]:
    """构建变异器输入（§8 提示词契约：父文档只读视图 + 唯一目标 Module +
    归因/成功保护摘要 + 有界拒绝历史 + 编辑预算）。"""
    item = spec.module(target_module)
    if item is None:
        return {"ok": False, "error": REJ_MODULE_NOT_FOUND, "reason": f"目标 Module 不存在: {target_module}"}
    section = module_section_text(spec, raw_text, target_module)
    resolved_budget = budget or freeze_budget()
    rejections = [
        {"reason": e.reason, "detail": e.detail, "diff": e.diff, "seen_count": e.seen_count}
        for e in (rejection_history.rejections_for(target_module) if rejection_history else [])
    ]
    return {
        "ok": True,
        "target_module": item.to_dict(),
        "module_text": section,
        "module_text_sha256": _sha256_text(section),
        "mutation_brief": dict(brief or {}),
        "rejection_history": rejections,
        "edit_budget": resolved_budget.to_dict(),
        "sealed_content_guard": True,
        "note": "只输出目标 Module 的编辑操作与理由；不返回整份可改写 Skill；不得包含考卷题目/答案",
    }


def render_mutation_prompt(mutation_input: dict[str, Any], *, template: str | None = None) -> str:
    """渲染变异提示词：模板（prompts/mutation.md）+ 输入 JSON 附录。"""
    body = template
    if body is None:
        path = Path(__file__).resolve().parent / "prompts" / MUTATION_PROMPT_NAME
        if path.is_file():
            try:
                body = path.read_text(encoding="utf-8")
            except OSError:
                body = None
    if not body:
        body = _MUTATION_FALLBACK_PROMPT
    appendix = json.dumps(mutation_input, ensure_ascii=False, indent=2, sort_keys=True)
    return f"{body}\n\n<!-- mutation_input -->\n{appendix}"
