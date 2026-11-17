"""DE-29 最小 Module 定位（SKILL-EVOLUTION-SEARCH §5，外置 ModuleSpec）。

只复用 ``agents.frontmatter`` 的既有解析（不引入第二 Skill 格式）。ModuleSpec
全部外置在内存结构/旁车文件里，**默认不向用户 SKILL.md 添加任何定位标记**：
span 是候选版本绑定的正文区间，锚点是既有内容的 sha256（结构锚点），行号不入盘。

fail-closed 原则（§5.1/§9：重叠、有歧义、无法唯一定位时暂停，不猜边界）：
- 标题行不唯一 / 找不到 / 交叠 span / span 越界 → 拒绝建立或拒绝重定位；
- 陈旧锚点（span 下内容 hash 与冻结值不符）→ 拒绝候选编辑；
- 外部编辑（manifest 冻结后源文件被改）→ ``blocked_source_changed``；
- frontmatter 身份字段默认只读，任何候选编辑不得触碰；
- ``shared.guardrails`` 类型强制只读（租户/数据范围/操作许可即使标题叫
  「业务边界」也不允许自动改写，§5.2）。

小 Skill 可把全部可写正文定义为一个 Module（module_id=body.primary），
不强制拆 routing/capability/shared 多层。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..frontmatter import parse_frontmatter

# 模块类型口径（§5.2 默认类型表）；单 Module 小技能用 capability.primary。
MODULE_TYPE_PATTERN = re.compile(
    r"^(routing|capability(\.[\w.\-]+)?|shared\.(response|boundaries|guardrails))$"
)
# 只读类型：身份、授权、安全、预算、发布/不可逆操作确认；统一划入只读 guardrails。
READONLY_MODULE_TYPES = {"shared.guardrails"}
DEFAULT_MODULE_ID = "body.primary"
DEFAULT_MODULE_TYPE = "capability.primary"
# 内容锚点摘录长度（offset 定位模块的重定位辅助；heading 模块不依赖它）。
ANCHOR_EXCERPT_CHARS = 120


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@dataclass
class SkillModule:
    """一个业务 Module：外置 span + 内容锚点，module_id 与可写策略跨代不变。"""

    module_id: str
    type: str
    writable: bool
    start: int  # 正文区（frontmatter 之后）内的字符区间，含头不含尾
    end: int
    anchor_hash: str  # 区间内容的 sha256（候选版本绑定）
    heading: str = ""  # 标题定位模块的标题行原文（如 "## 工作流程"）；整文 Module 为 ""
    anchor_excerpt: str = ""  # 区间开头摘录（offset 定位模块的重定位辅助）
    dependencies: tuple[str, ...] = ()
    owner: str = ""
    whole_body: bool = False  # 小 Skill 的单一可写正文 Module：恒等覆盖新正文全区

    def to_dict(self) -> dict[str, Any]:
        return {
            "module_id": self.module_id,
            "type": self.type,
            "writable": self.writable,
            "start": self.start,
            "end": self.end,
            "anchor_hash": self.anchor_hash,
            "heading": self.heading,
            "anchor_excerpt": self.anchor_excerpt,
            "dependencies": list(self.dependencies),
            "owner": self.owner,
            "whole_body": self.whole_body,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SkillModule":
        return cls(
            module_id=str(data.get("module_id") or ""),
            type=str(data.get("type") or ""),
            writable=bool(data.get("writable")),
            start=int(data.get("start") or 0),
            end=int(data.get("end") or 0),
            anchor_hash=str(data.get("anchor_hash") or ""),
            heading=str(data.get("heading") or ""),
            anchor_excerpt=str(data.get("anchor_excerpt") or ""),
            dependencies=tuple(str(d) for d in (data.get("dependencies") or [])),
            owner=str(data.get("owner") or ""),
            whole_body=bool(data.get("whole_body")),
        )


@dataclass
class ModuleSpec:
    """子版本可重建的 ModuleSpec：模块逻辑 ID 与可写策略跨代不变，span 跟随版本。"""

    skill_name: str
    source_text_sha256: str  # 冻结时整份 SKILL.md 文本的 sha256（utf-8，无 BOM）
    frontmatter_boundary: int  # 正文区起点（frontmatter 闭合行之后的偏移）
    frontmatter_sha256: str  # frontmatter 区内容 hash（身份字段只读的依据）
    modules: list[SkillModule] = field(default_factory=list)
    approved_spec_hash: str = ""

    def module(self, module_id: str) -> SkillModule | None:
        for item in self.modules:
            if item.module_id == module_id:
                return item
        return None

    def writable_modules(self) -> list[SkillModule]:
        return [m for m in self.modules if m.writable]

    def readonly_modules(self) -> list[SkillModule]:
        return [m for m in self.modules if not m.writable]


def _spec_payload(spec: ModuleSpec) -> dict[str, Any]:
    return {
        "skill_name": spec.skill_name,
        "source_text_sha256": spec.source_text_sha256,
        "frontmatter_boundary": spec.frontmatter_boundary,
        "frontmatter_sha256": spec.frontmatter_sha256,
        "modules": [m.to_dict() for m in sorted(spec.modules, key=lambda m: (m.start, m.end, m.module_id))],
    }


def compute_spec_hash(spec: ModuleSpec) -> str:
    return _canonical_sha256(_spec_payload(spec))


def spec_to_dict(spec: ModuleSpec) -> dict[str, Any]:
    payload = _spec_payload(spec)
    payload["approved_spec_hash"] = spec.approved_spec_hash
    return payload


def spec_from_dict(data: dict[str, Any]) -> ModuleSpec:
    spec = ModuleSpec(
        skill_name=str(data.get("skill_name") or ""),
        source_text_sha256=str(data.get("source_text_sha256") or ""),
        frontmatter_boundary=int(data.get("frontmatter_boundary") or 0),
        frontmatter_sha256=str(data.get("frontmatter_sha256") or ""),
        modules=[SkillModule.from_dict(item) for item in (data.get("modules") or [])],
        approved_spec_hash=str(data.get("approved_spec_hash") or ""),
    )
    return spec


def frontmatter_boundary_of(text: str) -> tuple[int, dict[str, str]]:
    """frontmatter 闭合行之后的正文区起点。

    meta 解析复用 agents.frontmatter（单一格式口径）；这里只补一个确定性的
    闭合行偏移，供外置 span 使用（parse_frontmatter 会 strip 正文，拿不到偏移）。
    """
    meta = parse_frontmatter(text).meta
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return 0, meta
    end_idx = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx == -1:
        return 0, meta
    boundary = sum(len(line) + 1 for line in lines[: end_idx + 1])
    return boundary, meta


def _line_starts(body: str) -> tuple[list[int], int]:
    starts: list[int] = []
    offset = 0
    for line in body.splitlines(keepends=True):
        starts.append(offset)
        offset += len(line)
    if not starts:
        starts = [0]
    return starts, len(body)


def _heading_line_indices(body: str, heading: str) -> list[int]:
    indices: list[int] = []
    offset = 0
    for line in body.splitlines(keepends=True):
        if line.strip() == heading:
            indices.append(offset)
        offset += len(line)
    return indices


def _normalize_defs(module_defs: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    defs = list(module_defs or [])
    seen_ids: set[str] = set()
    for item in defs:
        module_id = str(item.get("module_id") or "").strip()
        if not module_id:
            raise ValueError("module_id_required")
        if module_id in seen_ids:
            raise ValueError("module_id_duplicated")
        seen_ids.add(module_id)
    return defs


def _build_sections(
    body: str, defs: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], str]:
    """按标题/显式区间解析出各 Module 的 body 区间；失败返回错误码。"""
    starts, body_len = _line_starts(body)
    resolved: list[dict[str, Any]] = []
    for item in defs:
        module_id = str(item["module_id"])
        module_type = str(item.get("type") or "").strip()
        if not MODULE_TYPE_PATTERN.match(module_type):
            return [], "unknown_module_type"
        writable = bool(item.get("writable"))
        if module_type in READONLY_MODULE_TYPES and writable:
            return [], "guardrails_must_be_readonly"
        heading = str(item.get("heading") or "").strip()
        explicit_start = item.get("start")
        explicit_end = item.get("end")
        if heading:
            hits = _heading_line_indices(body, heading)
            if len(hits) != 1:
                return [], "span_not_unique"
            resolved.append(
                {
                    "module_id": module_id,
                    "type": module_type,
                    "writable": writable,
                    "heading": heading,
                    "start": hits[0],
                    "end": None,  # 由分区规则补齐
                    "dependencies": tuple(item.get("dependencies") or ()),
                    "owner": str(item.get("owner") or ""),
                }
            )
        elif explicit_start is not None and explicit_end is not None:
            start, end = int(explicit_start), int(explicit_end)
            if start < 0 or end < start or end > body_len:
                return [], "span_out_of_range"
            section = body[start:end]
            resolved.append(
                {
                    "module_id": module_id,
                    "type": module_type,
                    "writable": writable,
                    "heading": "",
                    "start": start,
                    "end": end,
                    "dependencies": tuple(item.get("dependencies") or ()),
                    "owner": str(item.get("owner") or ""),
                    "anchor_excerpt": section[:ANCHOR_EXCERPT_CHARS],
                }
            )
        else:
            return [], "module_locator_missing"

    # 标题定位模块的分区：每个标题段延伸到下一个标题段起点（任意 Module 的标题
    # 行都算边界），最后一段延伸到正文末尾；正文首段（第一个标题之前）不属于
    # 任何 Module，默认只读。
    heading_starts = sorted(entry["start"] for entry in resolved if entry["heading"])
    for entry in resolved:
        if entry["heading"] and entry["end"] is None:
            nxt = [pos for pos in heading_starts if pos > entry["start"]]
            entry["end"] = nxt[0] if nxt else body_len

    ordered = sorted(resolved, key=lambda e: (e["start"], e["end"], e["module_id"]))
    prev = None
    for entry in ordered:
        if entry["end"] is None:  # 防御：不应发生（标题分区已补齐）
            return [], "span_not_unique"
        if prev is not None and entry["start"] < prev["end"]:
            return [], "span_overlap"
        prev = entry
    return ordered, ""


def _make_module(entry: dict[str, Any], body: str) -> SkillModule:
    section = body[entry["start"] : entry["end"]]
    return SkillModule(
        module_id=entry["module_id"],
        type=entry["type"],
        writable=bool(entry["writable"]),
        start=entry["start"],
        end=entry["end"],
        anchor_hash=_sha256_text(section),
        heading=entry.get("heading") or "",
        anchor_excerpt=entry.get("anchor_excerpt") or section[:ANCHOR_EXCERPT_CHARS],
        dependencies=tuple(entry.get("dependencies") or ()),
        owner=str(entry.get("owner") or ""),
    )


def build_module_spec(
    raw_text: str,
    module_defs: list[dict[str, Any]] | None = None,
    *,
    skill_name: str = "",
    owner: str = "",
) -> dict[str, Any]:
    """从既有 SKILL.md 文本建立外置 ModuleSpec（不写盘、不加标记）。

    - module_defs 为空 → 单一可写正文 Module（小 Skill 合法形态）；
    - module_defs 每项：{module_id, type, writable, heading|start/end,
      dependencies?, owner?}；标题必须整行唯一命中，否则 ``span_not_unique``；
    - 交叠/越界/guardrails 声明可写/未知类型 → 拒绝并给原因。
    """
    try:
        defs = _normalize_defs(module_defs)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "reason": "module 定义非法（缺 id 或 id 重复）"}
    boundary, _meta = frontmatter_boundary_of(raw_text)
    body = raw_text[boundary:]
    if not defs:
        defs = [
            {
                "module_id": DEFAULT_MODULE_ID,
                "type": DEFAULT_MODULE_TYPE,
                "writable": True,
                "start": 0,
                "end": len(body),
                "whole_body": True,
            }
        ]
    sections, error = _build_sections(body, defs)
    if error:
        return {
            "ok": False,
            "error": error,
            "reason": _rejection_reason(error),
        }
    spec = ModuleSpec(
        skill_name=str(skill_name or "").strip(),
        source_text_sha256=_sha256_text(raw_text),
        frontmatter_boundary=boundary,
        frontmatter_sha256=_sha256_text(raw_text[:boundary]),
        modules=[_make_module(entry, body) for entry in sections],
    )
    # whole_body 标志随定义传递（默认单 Module 为 True）
    whole_flags = {str(d.get("module_id")): bool(d.get("whole_body")) for d in defs}
    for item in spec.modules:
        item.whole_body = bool(whole_flags.get(item.module_id))
    spec.approved_spec_hash = compute_spec_hash(spec)
    return {"ok": True, "spec": spec, "owner": owner}


_REJECTION_TEXT = {
    "span_not_unique": "标题/锚点在正文中不唯一或缺失，无法唯一定位（不猜边界）",
    "span_overlap": "Module span 交叠",
    "span_out_of_range": "Module span 超出正文区",
    "unknown_module_type": "未知 Module 类型（允许：routing/capability.*/shared.response|boundaries|guardrails）",
    "guardrails_must_be_readonly": "shared.guardrails 一律只读，不允许声明为可写（§5.2）",
    "module_locator_missing": "Module 缺少定位信息（heading 或 start/end）",
    "anchor_lost": "锚点在新版本中丢失，暂停重定位（不猜边界）",
    "module_id_required": "module_id 必填",
    "module_id_duplicated": "module_id 重复",
}


def _rejection_reason(error: str) -> str:
    return _REJECTION_TEXT.get(error, error)


def module_section_text(spec: ModuleSpec, raw_text: str, module_id: str) -> str:
    """按冻结 span 取当前文本里的 Module 区段原文（供校验/展示）。"""
    item = spec.module(module_id)
    if item is None:
        raise KeyError(module_id)
    start = spec.frontmatter_boundary + item.start
    end = spec.frontmatter_boundary + item.end
    return raw_text[start:end]


def _relocate_others(
    child_modules: list[SkillModule],
    target_id: str,
    candidate_body: str,
    target_start: int,
    target_end: int,
) -> tuple[list[SkillModule], str]:
    """目标 Module 之外的重定位：标题唯一命中 + 冻结区段长度（extent）规则。

    区段内容必须与冻结锚点逐字节一致（否则 anchor_lost）；目标 Module 之外的
    前后插入不改变各区段自身内容，span 随标题平移即可正确定位。
    """
    target = next((m for m in child_modules if m.module_id == target_id), None)
    if target is None:
        return [], "module_not_found"
    others = [m for m in child_modules if m.module_id != target_id]
    result: list[SkillModule] = []
    for item in others:
        if item.heading:
            hits = _heading_line_indices(candidate_body, item.heading)
            if len(hits) != 1:
                return [], "span_not_unique" if len(hits) > 1 else "anchor_lost"
            start = hits[0]
        else:
            excerpt = item.anchor_excerpt
            if not excerpt:
                return [], "anchor_lost"
            hits = [i for i in range(len(candidate_body) - len(excerpt) + 1) if candidate_body[i : i + len(excerpt)] == excerpt]
            if len(hits) != 1:
                return [], "span_not_unique" if len(hits) > 1 else "anchor_lost"
            start = hits[0]
        end = start + (item.end - item.start)
        if end > len(candidate_body):
            return [], "anchor_lost"
        section = candidate_body[start:end]
        if _sha256_text(section) != item.anchor_hash:
            return [], "anchor_lost"
        result.append(
            SkillModule(
                module_id=item.module_id,
                type=item.type,
                writable=item.writable,
                start=start,
                end=end,
                anchor_hash=item.anchor_hash,
                heading=item.heading,
                anchor_excerpt=item.anchor_excerpt,
                dependencies=item.dependencies,
                owner=item.owner,
                whole_body=item.whole_body,
            )
        )
    result.append(
        SkillModule(
            module_id=target.module_id,
            type=target.type,
            writable=target.writable,
            start=target_start,
            end=target_end,
            anchor_hash=_sha256_text(candidate_body[target_start:target_end]),
            heading=target.heading,
            anchor_excerpt=candidate_body[target_start : target_start + ANCHOR_EXCERPT_CHARS],
            dependencies=target.dependencies,
            owner=target.owner,
            whole_body=target.whole_body,
        )
    )
    return result, ""


def apply_module_edit(
    spec: ModuleSpec,
    raw_text: str,
    *,
    module_id: str,
    replacement_text: str,
    skill_text_sha256: str | None = None,
) -> dict[str, Any]:
    """有界单 Module 编辑：确定性合并出完整候选文本 + 子版本 ModuleSpec。

    - 先走 validate_candidate_edit（锚点/只读/frontmatter/外部编辑全检）；
    - 替换精确发生在冻结 span 上（无 fuzzy apply），其余源片段字节不变；
    - 子版本 ModuleSpec 的模块逻辑 ID 与可写策略不变，span/锚点跟随候选重定位；
    - 不写任何盘：候选文本与子 spec 由调用方经搜索命名空间落盘。
    """
    checked = validate_candidate_edit(
        spec,
        module_id=module_id,
        replacement_text=replacement_text,
        current_text=raw_text,
        skill_text_sha256=skill_text_sha256,
    )
    if not checked.get("ok"):
        return checked
    item = spec.module(module_id)
    assert item is not None  # validate 已保证
    abs_start = spec.frontmatter_boundary + item.start
    abs_end = spec.frontmatter_boundary + item.end
    candidate_text = raw_text[:abs_start] + replacement_text + raw_text[abs_end:]
    child_boundary = spec.frontmatter_boundary  # frontmatter 字节不变
    candidate_body = candidate_text[child_boundary:]
    target_start = item.start
    target_end = item.start + len(replacement_text)
    relocated, error = _relocate_others(
        spec.modules, module_id, candidate_body, target_start, target_end
    )
    if error:
        return {"ok": False, "error": error, "reason": _rejection_reason(error)}
    ordered = sorted(relocated, key=lambda m: (m.start, m.end, m.module_id))
    prev = None
    for item in ordered:
        if prev is not None and item.start < prev.end:
            return {"ok": False, "error": "span_overlap", "reason": _rejection_reason("span_overlap")}
        prev = item
    child_spec = ModuleSpec(
        skill_name=spec.skill_name,
        source_text_sha256=_sha256_text(candidate_text),
        frontmatter_boundary=child_boundary,
        frontmatter_sha256=spec.frontmatter_sha256,
        modules=ordered,
    )
    child_spec.approved_spec_hash = compute_spec_hash(child_spec)
    return {
        "ok": True,
        "candidate_text": candidate_text,
        "child_spec": child_spec,
        "changed_module": module_id,
    }


def relocate_modules(spec: ModuleSpec, new_text: str) -> dict[str, Any]:
    """正文长度变化后的候选重定位（S3）：按标题/锚点在既有版本里重算 span。

    - 单一整文 Module（whole_body）恒等重定位到新正文全区（内容可变）；
    - 标题/锚点 Module 要求唯一定位且**冻结区段内容逐字节不变**（extent 规则：
      新 span = 新位置 + 冻结长度）；区段外的正文增删不影响重定位，区段内
      内容漂移 → ``anchor_lost``，应走 apply_module_edit 而不是静默重定位；
    - 模块逻辑 ID / 类型 / 可写策略跨代不变（子版本 ModuleSpec 契约）。
    """
    if compute_spec_hash(spec) != spec.approved_spec_hash:
        return {"ok": False, "error": "spec_hash_mismatch", "reason": "ModuleSpec 校验 hash 与内容不符（陈旧/被篡改）"}
    boundary, _meta = frontmatter_boundary_of(new_text)
    if _sha256_text(new_text[:boundary]) != spec.frontmatter_sha256:
        return {"ok": False, "error": "frontmatter_modified", "reason": "frontmatter 身份区被修改，拒绝重定位"}
    new_body = new_text[boundary:]
    relocated: list[SkillModule] = []
    for item in spec.modules:
        if item.whole_body:
            # 整文单 Module：span 恒等覆盖新正文（内容可变，锚点跟随重算）
            section = new_body
            relocated.append(
                SkillModule(
                    module_id=item.module_id,
                    type=item.type,
                    writable=item.writable,
                    start=0,
                    end=len(new_body),
                    anchor_hash=_sha256_text(section),
                    heading="",
                    anchor_excerpt=section[:ANCHOR_EXCERPT_CHARS],
                    dependencies=item.dependencies,
                    owner=item.owner,
                    whole_body=True,
                )
            )
            continue
        if item.heading:
            hits = _heading_line_indices(new_body, item.heading)
            if len(hits) != 1:
                error = "span_not_unique" if len(hits) > 1 else "anchor_lost"
                return {"ok": False, "error": error, "reason": _rejection_reason(error)}
            start = hits[0]
        else:
            excerpt = item.anchor_excerpt
            hits = [i for i in range(len(new_body) - len(excerpt) + 1) if new_body[i : i + len(excerpt)] == excerpt] if excerpt else []
            if len(hits) != 1:
                error = "span_not_unique" if len(hits) > 1 else "anchor_lost"
                return {"ok": False, "error": error, "reason": _rejection_reason(error)}
            start = hits[0]
        end = start + (item.end - item.start)
        if end > len(new_body) or _sha256_text(new_body[start:end]) != item.anchor_hash:
            return {"ok": False, "error": "anchor_lost", "reason": _rejection_reason("anchor_lost")}
        relocated.append(
            SkillModule(
                module_id=item.module_id,
                type=item.type,
                writable=item.writable,
                start=start,
                end=end,
                anchor_hash=item.anchor_hash,
                heading=item.heading,
                anchor_excerpt=item.anchor_excerpt,
                dependencies=item.dependencies,
                owner=item.owner,
                whole_body=item.whole_body,
            )
        )
    ordered = sorted(relocated, key=lambda m: (m.start, m.end, m.module_id))
    prev = None
    for item in ordered:
        if prev is not None and item.start < prev.end:
            return {"ok": False, "error": "span_overlap", "reason": _rejection_reason("span_overlap")}
        prev = item
    child_spec = ModuleSpec(
        skill_name=spec.skill_name,
        source_text_sha256=_sha256_text(new_text),
        frontmatter_boundary=boundary,
        frontmatter_sha256=spec.frontmatter_sha256,
        modules=ordered,
    )
    child_spec.approved_spec_hash = compute_spec_hash(child_spec)
    return {"ok": True, "spec": child_spec}


def validate_candidate_edit(
    spec: ModuleSpec,
    *,
    module_id: str,
    replacement_text: str,
    current_text: str,
    skill_text_sha256: str | None = None,
) -> dict[str, Any]:
    """候选编辑前置校验（S2）：spec 完整性、外部编辑、frontmatter 只读、
    Module 存在且可写、锚点新鲜。任一不过即拒绝并给可解释原因。"""
    if compute_spec_hash(spec) != spec.approved_spec_hash:
        return {"ok": False, "error": "spec_hash_mismatch", "reason": "ModuleSpec 校验 hash 与内容不符（陈旧/被篡改）"}
    if skill_text_sha256 is not None and _sha256_text(current_text) != skill_text_sha256:
        return {
            "ok": False,
            "error": "blocked_source_changed",
            "reason": "源 SKILL.md 在 manifest 冻结后被外部编辑（字节 hash 不符）",
            "expected_sha256": skill_text_sha256,
            "actual_sha256": _sha256_text(current_text),
        }
    boundary, _meta = frontmatter_boundary_of(current_text)
    if boundary != spec.frontmatter_boundary or _sha256_text(current_text[:boundary]) != spec.frontmatter_sha256:
        return {
            "ok": False,
            "error": "frontmatter_modified",
            "reason": "frontmatter 身份字段默认只读：候选编辑不得触碰 name/依赖/工具声明/IR 字段",
        }
    item = spec.module(module_id)
    if item is None:
        return {"ok": False, "error": "module_not_found", "reason": f"目标 Module 不存在: {module_id}"}
    if not item.writable:
        return {
            "ok": False,
            "error": "readonly_module",
            "reason": f"Module {module_id}（{item.type}）在业务可写范围之外：guardrails/只读段不可自动改写",
        }
    section = current_text[spec.frontmatter_boundary + item.start : spec.frontmatter_boundary + item.end]
    if _sha256_text(section) != item.anchor_hash:
        return {
            "ok": False,
            "error": "stale_anchor_hash",
            "reason": "span 下内容与冻结锚点不符（陈旧 hash），拒绝编辑",
        }
    if not isinstance(replacement_text, str):
        return {"ok": False, "error": "replacement_invalid", "reason": "replacement_text 必须是字符串"}
    return {"ok": True, "module": item.to_dict(), "section_text": section}
