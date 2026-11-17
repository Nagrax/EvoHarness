"""DE-29 SearchManifest：绑定原技能身份、冻结完整 bundle 与评测复用契约。

对应 SKILL-EVOLUTION-SEARCH §5/§6 与 NATIVE-ARCHITECTURE-INTEGRATION §4/§5/§8：

- 身份门：只有**明确独立执行 SOP**（frontmatter ``producer-kind: executable_sop``，
  或 ``producer-kind: direct`` + 责任人确认标记）可建立 SearchManifest；
  ``projection`` / ``managed-by: experience.skill_sync`` / ``.projection.json``
  托管投影、以及身份不明（无 producer-kind）的目标一律拒绝自动搜索，
  拒绝时返回可解释原因（不猜身份，§4 存量不明文件先诊断）。
- 完整 bundle 冻结：DE-18 ``skill_content_hash``（四个内容字段，不重复实现）+
  原始 SKILL.md 字节 hash（与 ``publish_skill_bundle`` 的 ``expected_active_sha256``
  同口径）+ 技能目录内全部行为承载文件（路径/sha256/size/kind）+ 工具集
  → ``bundle_hash``/``bundle_id``。正文、辅助脚本、工具集任一变化 → 新 bundle。
- 协议冻结：Case/rubric/verifier 引用与 hash、评测/反思/judge 模型环境、预算、
  算法版本、S/V/H 范围引用冻结进协议；``eval_protocol_fingerprint`` 包含
  bundle_hash —— bundle 任何组成变化 → 新协议指纹 → 旧评测不可复用（S1）。
- 冻结期只读：manifest 记录的全部源文件在搜索期间只读（``check_frozen_write``
  拒绝写入源目录）；候选的重定位信息 / ModuleSpec 只写搜索命名空间
  ``<base>/<tenant>/<space>/<lineage>/<search_id>/``，绝不写回原技能目录。
- CAS 组合：冻结 ``expected_version``（frontmatter version）与
  ``expected_active_sha256``（原始字节 hash），供 DE-18 ``publish_skill_bundle``
  显式采用认证候选时直接使用，不另造第二套 CAS 口径。
- 命名空间：tenant/space/lineage 进入目录分段，同名技能跨空间不混账
  （§5：scoped ref 完整迁移属 DE-06/09/18 后续，S1 只做搜索记录的隔离试验）。

运行记录落在现有 ``get_evolution_dir()/search/`` 下，不复制第二份调度真相。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..frontmatter import parse_frontmatter
from ..skill_evolution import get_evolution_dir, skill_content_hash
from .modules import ModuleSpec, compute_spec_hash, spec_to_dict  # noqa: F401 (ModuleSpec 再导出)

PRODUCER_KIND_EXECUTABLE_SOP = "executable_sop"
PRODUCER_KIND_PROJECTION = "projection"
PRODUCER_KIND_DIRECT = "direct"
PRODUCER_KIND_UNKNOWN = "unknown"
PROJECTION_MANAGED_BY = "experience.skill_sync"
PROJECTION_SIDECAR = ".projection.json"
SKILL_TEXT_NAME = "SKILL.md"
MANIFEST_NAME = "manifest.json"
CANDIDATE_SPEC_NAME = "module-spec.json"
SEARCH_DIR_BASENAME = "search"

# 控制面元数据（投影清单/发布锁/搜索旁车）不承载行为，不计入 bundle 组成；
# 其余任何文件（脚本/references/资源）都是 bundle 身份的一部分。
CONTROL_SIDECAR_FILES = {PROJECTION_SIDECAR, ".publish.lock", ".search-modules.json"}
SCRIPT_SUFFIXES = {".py", ".sh", ".ps1", ".bat", ".js", ".ts"}

MANIFEST_VERSION = 1


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _file_kind(rel_path: str) -> str:
    name = Path(rel_path).name
    if name == SKILL_TEXT_NAME:
        return "skill_text"
    suffix = Path(rel_path).suffix.lower()
    if suffix in SCRIPT_SUFFIXES:
        return "script"
    if suffix in {".md", ".txt"}:
        return "reference"
    return "asset"


def _bundle_files(skill_dir: Path) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for path in sorted(skill_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(skill_dir).as_posix()
        if Path(rel).name in CONTROL_SIDECAR_FILES:
            continue
        raw = path.read_bytes()
        files.append(
            {
                "path": rel,
                "sha256": _sha256_bytes(raw),
                "size": len(raw),
                "kind": _file_kind(rel),
            }
        )
    return files


def _normalize_allowed_tools(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ",".join(str(item).strip() for item in value if str(item).strip())
    return ",".join(part.strip() for part in str(value).split(",") if part.strip())


# ── 身份门：producer_kind 语义沿用 DE-06/DE-18，单一判定口径 ─────────────


def classify_producer_kind(skill_dir: str | Path) -> dict[str, Any]:
    """判定技能目录的 producer 身份（投影/独立 SOP/direct/不明）。

    判定依据（证据全部返回，不猜）：
    - frontmatter ``producer-kind`` / ``managed-by``（DE-06 写入的语义）；
    - ``.projection.json`` 托管清单（write_managed_projection 的旁车）——
      存在即视为投影管理目录（fail-closed：清单管理的目录不允许搜索认领）。
    """
    directory = Path(skill_dir)
    evidence: list[str] = []
    meta: dict[str, str] = {}
    text = ""
    skill_file = directory / SKILL_TEXT_NAME
    if skill_file.is_file():
        text = skill_file.read_text(encoding="utf-8-sig")
        meta = parse_frontmatter(text).meta
    kind = ""
    managed_by = str(meta.get("managed-by") or "")
    front_kind = str(meta.get("producer-kind") or "")
    if front_kind:
        kind = front_kind
        evidence.append(f"frontmatter:producer-kind={front_kind}")
    if managed_by:
        evidence.append(f"frontmatter:managed-by={managed_by}")
    if managed_by == PROJECTION_MANAGED_BY:
        kind = PRODUCER_KIND_PROJECTION
    sidecar = directory / PROJECTION_SIDECAR
    if sidecar.is_file():
        evidence.append("sidecar:.projection.json")
        try:
            sidecar_meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except Exception:
            sidecar_meta = {}
        sidecar_kind = str((sidecar_meta or {}).get("producer_kind") or "")
        sidecar_managed = str((sidecar_meta or {}).get("managed_by") or "")
        if sidecar_kind == PRODUCER_KIND_PROJECTION or sidecar_managed == PROJECTION_MANAGED_BY:
            kind = PRODUCER_KIND_PROJECTION
        elif kind == "":
            # 有托管清单但字段异常：仍按投影管理目录处理（fail-closed，不猜身份）
            kind = PRODUCER_KIND_PROJECTION
    if kind == "":
        kind = PRODUCER_KIND_UNKNOWN
    return {
        "skill_dir": str(directory),
        "producer_kind": kind,
        "managed_by": managed_by,
        "evidence": evidence,
        "searchable": kind in {PRODUCER_KIND_EXECUTABLE_SOP, PRODUCER_KIND_DIRECT},
    }


def _reject(error: str, reason: str, **detail: Any) -> dict[str, Any]:
    return {"ok": False, "error": error, "reason": reason, **detail}


def _valid_owner_confirmation(owner_confirmation: dict[str, Any] | None) -> bool:
    if owner_confirmation is None:
        return False
    if not isinstance(owner_confirmation, dict):
        return False
    return bool(str(owner_confirmation.get("confirmed_by") or "").strip())


@dataclass
class SearchManifest:
    """冻结的搜索种子身份：完整 bundle + 协议 + 范围引用（只增不改）。"""

    search_id: str
    skill_name: str
    skill_dir: str
    tenant: str
    space: str
    lineage: str
    producer_kind: str
    owner_confirmation: dict[str, str] = field(default_factory=dict)
    expected_version: str = ""
    skill_content_hash: str = ""
    skill_text_sha256: str = ""  # 解码文本（utf-8，无 BOM）口径
    expected_active_sha256: str = ""  # 原始字节口径（= publish_skill_bundle 的 CAS 键）
    frontmatter_meta: dict[str, str] = field(default_factory=dict)
    files: list[dict[str, Any]] = field(default_factory=list)
    toolset: dict[str, Any] = field(default_factory=dict)
    bundle_hash: str = ""
    bundle_id: str = ""
    protocol: dict[str, Any] = field(default_factory=dict)
    eval_protocol_fingerprint: str = ""
    module_spec_hash: str = ""
    status: str = "frozen"
    created_at: str = ""
    manifest_version: int = MANIFEST_VERSION

    # ── 完整性核验：bundle 任何组成变化都可发现 ──
    def verify_integrity(self, skill_dir: str | Path | None = None) -> dict[str, Any]:
        root = Path(skill_dir or self.skill_dir)
        drifted: list[dict[str, str]] = []
        skill_file = root / SKILL_TEXT_NAME
        if not skill_file.is_file():
            drifted.append({"path": SKILL_TEXT_NAME, "kind": "missing"})
        else:
            raw = skill_file.read_bytes()
            if _sha256_bytes(raw) != self.expected_active_sha256:
                drifted.append({"path": SKILL_TEXT_NAME, "kind": "modified"})
        current = {f["path"]: f["sha256"] for f in _bundle_files(root)}
        frozen = {f["path"] for f in self.files}
        for item in self.files:
            now = current.get(item["path"])
            if now is None:
                drifted.append({"path": item["path"], "kind": "missing"})
            elif now != item["sha256"]:
                drifted.append({"path": item["path"], "kind": "modified"})
        for path in current:
            if path not in frozen:
                drifted.append({"path": path, "kind": "added"})
        if drifted:
            source_changed = any(d["path"] == SKILL_TEXT_NAME for d in drifted)
            return {
                "ok": False,
                "reason": "blocked_source_changed" if source_changed else "bundle_composition_changed",
                "skill_source_changed": source_changed,
                "drifted": drifted,
            }
        return {"ok": True, "reason": ""}

    # ── 评测复用判定（S1）：绑定 bundle+协议的旧评测才可复用 ──
    def check_evaluation_reuse(self, evaluation: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(evaluation, dict):
            return _reject("evaluation_not_bound_to_bundle", "评测记录缺失，无法证明其绑定的 bundle 身份")
        if not str(evaluation.get("bundle_hash") or "") and not str(evaluation.get("bundle_id") or ""):
            return _reject(
                "evaluation_not_bound_to_bundle",
                "旧评测未绑定 bundle_hash/bundle_id，按 S1 契约不可复用（fail-closed）",
            )
        if str(evaluation.get("bundle_hash") or "") != self.bundle_hash:
            return {
                "ok": False,
                "error": "bundle_changed",
                "reason": "bundle 组成（正文/辅助脚本/工具集）已变化：旧评测不可复用，须以新 bundle 重新评测",
                "evaluation_bundle_hash": str(evaluation.get("bundle_hash") or ""),
                "current_bundle_hash": self.bundle_hash,
            }
        if str(evaluation.get("eval_protocol_fingerprint") or "") != self.eval_protocol_fingerprint:
            return {
                "ok": False,
                "error": "protocol_changed",
                "reason": "评测协议（Case/rubric/模型环境）或 bundle 指纹已变化：跨协议分数不可混用",
                "evaluation_protocol": str(evaluation.get("eval_protocol_fingerprint") or ""),
                "current_protocol": self.eval_protocol_fingerprint,
            }
        return {"ok": True, "reason": ""}

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_version": self.manifest_version,
            "search_id": self.search_id,
            "skill_name": self.skill_name,
            "skill_dir": self.skill_dir,
            "tenant": self.tenant,
            "space": self.space,
            "lineage": self.lineage,
            "producer_kind": self.producer_kind,
            "owner_confirmation": dict(self.owner_confirmation),
            "expected_version": self.expected_version,
            "skill_content_hash": self.skill_content_hash,
            "skill_text_sha256": self.skill_text_sha256,
            "expected_active_sha256": self.expected_active_sha256,
            "frontmatter_meta": dict(self.frontmatter_meta),
            "files": [dict(f) for f in self.files],
            "toolset": dict(self.toolset),
            "bundle_hash": self.bundle_hash,
            "bundle_id": self.bundle_id,
            "protocol": dict(self.protocol),
            "eval_protocol_fingerprint": self.eval_protocol_fingerprint,
            "module_spec_hash": self.module_spec_hash,
            "status": self.status,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SearchManifest":
        required = ("search_id", "skill_name", "bundle_hash", "bundle_id")
        missing = [key for key in required if not str(data.get(key) or "")]
        if missing:
            raise ValueError(f"manifest_missing_fields:{','.join(missing)}")
        return cls(
            search_id=str(data["search_id"]),
            skill_name=str(data.get("skill_name") or ""),
            skill_dir=str(data.get("skill_dir") or ""),
            tenant=str(data.get("tenant") or ""),
            space=str(data.get("space") or ""),
            lineage=str(data.get("lineage") or ""),
            producer_kind=str(data.get("producer_kind") or ""),
            owner_confirmation=dict(data.get("owner_confirmation") or {}),
            expected_version=str(data.get("expected_version") or ""),
            skill_content_hash=str(data.get("skill_content_hash") or ""),
            skill_text_sha256=str(data.get("skill_text_sha256") or ""),
            expected_active_sha256=str(data.get("expected_active_sha256") or ""),
            frontmatter_meta=dict(data.get("frontmatter_meta") or {}),
            files=[dict(f) for f in (data.get("files") or [])],
            toolset=dict(data.get("toolset") or {}),
            bundle_hash=str(data.get("bundle_hash") or ""),
            bundle_id=str(data.get("bundle_id") or ""),
            protocol=dict(data.get("protocol") or {}),
            eval_protocol_fingerprint=str(data.get("eval_protocol_fingerprint") or ""),
            module_spec_hash=str(data.get("module_spec_hash") or ""),
            status=str(data.get("status") or "frozen"),
            created_at=str(data.get("created_at") or ""),
            manifest_version=int(data.get("manifest_version") or MANIFEST_VERSION),
        )


def freeze_protocol(
    *,
    cases_ref: str = "",
    cases_sha256: str = "",
    rubric_ref: str = "",
    rubric_sha256: str = "",
    verifier_sha256: str = "",
    models: dict[str, Any] | None = None,
    budget: dict[str, Any] | None = None,
    algorithm_version: str = "",
    splits: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """冻结评测协议（Case/rubric/verifier/模型环境/预算/算法版本/S-V-H 范围）。

    全部字段规范化后进入 SearchManifest；协议 hash 由 manifest 计算并绑定
    bundle_hash（bundle 变化 → 协议指纹变化）。S/V/H 是访问角色引用，
    每个 Case 的 origin_group/原 split 纪律由上游数据面维护（§6）。
    """
    return {
        "cases": {"ref": str(cases_ref), "sha256": str(cases_sha256)},
        "rubric": {"ref": str(rubric_ref), "sha256": str(rubric_sha256)},
        "verifier_sha256": str(verifier_sha256),
        "models": dict(models or {}),
        "budget": dict(budget or {}),
        "algorithm_version": str(algorithm_version),
        "splits": dict(splits or {}),
        "extra": dict(extra or {}),
    }


def build_search_manifest(
    *,
    skill_dir: str | Path,
    skill_name: str = "",
    tenant: str = "",
    space: str = "",
    lineage: str = "",
    tool_refs: list[str] | None = None,
    owner_confirmation: dict[str, Any] | None = None,
    protocol: dict[str, Any] | None = None,
    module_spec: ModuleSpec | None = None,
    search_id: str | None = None,
) -> dict[str, Any]:
    """建立 SearchManifest：身份门 → 完整 bundle 冻结 → 协议指纹。

    返回 ``{"ok": True, "manifest": SearchManifest}`` 或
    ``{"ok": False, "error", "reason", ...}``（拒绝时零副作用，不写任何文件）。
    """
    directory = Path(skill_dir)
    skill_file = directory / SKILL_TEXT_NAME
    if not directory.is_dir() or not skill_file.is_file():
        return _reject("skill_not_found", f"技能目录不存在或缺少 SKILL.md: {directory}")

    classification = classify_producer_kind(directory)
    kind = str(classification.get("producer_kind") or PRODUCER_KIND_UNKNOWN)
    if owner_confirmation is not None and not _valid_owner_confirmation(owner_confirmation):
        return _reject(
            "owner_confirmation_invalid",
            "责任人确认标记必须包含非空 confirmed_by（及可选 ref/confirmed_at）",
            producer_kind=kind,
        )
    if kind == PRODUCER_KIND_PROJECTION:
        return _reject(
            "projection_not_searchable",
            "托管投影（producer-kind=projection / managed-by=experience.skill_sync / .projection.json）"
            "由源服务重建：搜索不得认领投影正文，源修订走原投影链重建",
            evidence=classification.get("evidence", []),
        )
    allowed = False
    if kind == PRODUCER_KIND_EXECUTABLE_SOP:
        allowed = True
    elif kind == PRODUCER_KIND_DIRECT and _valid_owner_confirmation(owner_confirmation):
        allowed = True
    if not allowed:
        if kind == PRODUCER_KIND_DIRECT:
            return _reject(
                "owner_confirmation_required",
                "direct 技能只有在建立 SearchManifest 时携带责任人确认标记（confirmed_by）才可进入搜索",
                producer_kind=kind,
            )
        return _reject(
            "unknown_identity_not_searchable",
            "身份不明的技能不自动认领：需先明确 producer-kind=executable_sop（独立执行 SOP）"
            "或 direct+责任人确认；存量不明文件先诊断，保留原行为",
            evidence=classification.get("evidence", []),
        )

    # 文本口径与 agents 全库一致（read_text utf-8-sig，universal newlines）；
    # expected_active_sha256 另行保留原始字节口径（与 release._file_sha256 相同）。
    raw = skill_file.read_bytes()
    text = skill_file.read_text(encoding="utf-8-sig")
    parsed = parse_frontmatter(text)
    meta = parsed.meta
    # DE-06：声明了 scope-json 的托管投影必须由调用方给出同域 tenant/space；
    # 空作用域或跨域一律拒绝，旧名别名不能在别的空间被搜到。
    scope_raw = str(meta.get("scope-json") or "").strip()
    if scope_raw:
        try:
            skill_scope = json.loads(scope_raw)
        except ValueError:
            return _reject("scope_json_invalid", "scope-json 不是合法 JSON")
        if not isinstance(skill_scope, dict):
            return _reject("scope_json_invalid", "scope-json 必须是对象")
        resolved_tenant = str(tenant or "").strip()
        resolved_space = str(space or "").strip()
        if not resolved_tenant or not resolved_space:
            return _reject(
                "skill_scope_required",
                "技能声明了 scope-json：搜索必须显式给出 tenant/space，不能跨域解析")
        for key, expected in (("tenant", resolved_tenant), ("space", resolved_space)):
            actual = (skill_scope.get(key) or skill_scope.get(f"{key}_ref")
                      or skill_scope.get(f"{key}_id"))
            if actual not in (None, "") and str(actual) != expected:
                return _reject(
                    f"skill_scope_mismatch:{key}",
                    f"技能作用域 {key}={actual} 与请求 {expected} 不一致")
    resolved_name = str(skill_name or "").strip() or str(meta.get("name") or "") or directory.name
    content_hash = skill_content_hash(
        {
            "name": resolved_name,
            "description": meta.get("description", ""),
            "when_to_use": meta.get("when_to_use") or meta.get("when-to-use") or "",
            "instructions": parsed.body,
        }
    )
    toolset = {
        "allowed_tools": _normalize_allowed_tools(meta.get("allowed-tools")),
        "tool_refs": sorted({str(item).strip() for item in (tool_refs or []) if str(item).strip()}),
    }
    files = _bundle_files(directory)
    bundle_payload = {
        "content_hash": content_hash,
        "files": files,
        "toolset": toolset,
    }
    bundle_hash = _canonical_sha256(bundle_payload)
    bundle_id = "sbundle-" + bundle_hash[:16]
    frozen_protocol = dict(protocol or {})
    fingerprint = _canonical_sha256({"bundle_hash": bundle_hash, "protocol": frozen_protocol})

    module_spec_hash = ""
    if module_spec is not None:
        if str(module_spec.source_text_sha256) != hashlib.sha256(text.encode("utf-8")).hexdigest():
            return _reject(
                "module_spec_source_mismatch",
                "ModuleSpec 与冻结种子的 SKILL.md 文本 hash 不符（spec 来自其他版本）",
            )
        module_spec_hash = compute_spec_hash(module_spec)

    manifest = SearchManifest(
        search_id=str(search_id or "").strip() or ("s-" + uuid.uuid4().hex[:12]),
        skill_name=resolved_name,
        skill_dir=str(directory),
        tenant=str(tenant or "").strip(),
        space=str(space or "").strip(),
        lineage=str(lineage or "").strip() or resolved_name,
        producer_kind=kind,
        owner_confirmation=dict(owner_confirmation or {}),
        expected_version=str(meta.get("version") or ""),
        skill_content_hash=content_hash,
        skill_text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        expected_active_sha256=_sha256_bytes(raw),
        frontmatter_meta=dict(meta),
        files=files,
        toolset=toolset,
        bundle_hash=bundle_hash,
        bundle_id=bundle_id,
        protocol=frozen_protocol,
        eval_protocol_fingerprint=fingerprint,
        module_spec_hash=module_spec_hash,
        status="frozen",
        created_at=_utc_now(),
    )
    return {"ok": True, "manifest": manifest, "classification": classification}


def attach_bundle_identity(manifest: SearchManifest, evaluation: dict[str, Any]) -> dict[str, Any]:
    """把 manifest 冻结的 bundle/协议身份写进评测记录（评测入库前调用）。"""
    return {
        **dict(evaluation or {}),
        "bundle_id": manifest.bundle_id,
        "bundle_hash": manifest.bundle_hash,
        "eval_protocol_fingerprint": manifest.eval_protocol_fingerprint,
        "skill_content_hash": manifest.skill_content_hash,
        "tenant": manifest.tenant,
        "space": manifest.space,
        "lineage": manifest.lineage,
        "search_id": manifest.search_id,
    }


def _ns_component(value: str) -> str:
    raw = str(value or "").strip()
    slug = re.sub(r"[^\w\-.]+", "-", raw, flags=re.UNICODE)
    slug = re.sub(r"-{2,}", "-", slug).strip("-.")
    return slug or "default"


def search_namespace(manifest: SearchManifest, base_dir: str | Path | None = None) -> Path:
    """搜索命名空间：``<base>/<tenant>/<space>/<lineage>/<search_id>/``。

    base 默认跟随现有进化台账根（``get_evolution_dir()/search``），与
    usage/provenance/tier 审计同一事实源目录，不另造第二份调度真相。
    """
    base = Path(base_dir) if base_dir else get_evolution_dir() / SEARCH_DIR_BASENAME
    return base / _ns_component(manifest.tenant) / _ns_component(manifest.space) / _ns_component(manifest.lineage) / _ns_component(manifest.search_id)


def save_manifest(manifest: SearchManifest, *, base_dir: str | Path | None = None) -> dict[str, Any]:
    directory = search_namespace(manifest, base_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / MANIFEST_NAME
    path.write_text(json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"ok": True, "path": str(path)}


def load_manifest(path: str | Path) -> SearchManifest:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return SearchManifest.from_dict(data)


# ── 冻结期只读守卫：源目录只读，候选产物只进搜索命名空间 ─────────────────


def check_frozen_write(manifest: SearchManifest, target: str | Path) -> dict[str, Any]:
    """manifest 冻结的源技能目录在搜索期间只读：任何写入一律拒绝。"""
    root = Path(manifest.skill_dir).resolve()
    candidate = Path(target).resolve()
    if candidate == root or root in candidate.parents:
        return {
            "ok": False,
            "error": "frozen_source_readonly",
            "reason": f"{SKILL_TEXT_NAME} 及 manifest 冻结的辅助脚本/工具在搜索期间只读；"
                      "候选改动与重定位信息只能写入搜索命名空间，绝不写回原技能目录",
            "target": str(candidate),
            "frozen_root": str(root),
        }
    return {"ok": True, "target": str(candidate)}


def _candidate_dir_guard(manifest: SearchManifest, candidate_dir: str | Path) -> dict[str, Any]:
    guard = check_frozen_write(manifest, candidate_dir)
    if not guard.get("ok"):
        return guard
    return {"ok": True}


def write_candidate_artifact(
    manifest: SearchManifest,
    candidate_dir: str | Path,
    filename: str,
    text: str,
) -> dict[str, Any]:
    """候选产物（候选文本/重定位 spec/评测引用）写入候选目录。

    - 目标在冻结源目录内 → 拒绝（``frozen_source_readonly``）；
    - filename 必须是纯文件名（防目录穿越），候选文本按调用方提供的原样写盘。
    """
    guard = _candidate_dir_guard(manifest, candidate_dir)
    if not guard.get("ok"):
        return guard
    raw_name = str(filename or "").strip()
    name = Path(raw_name).name
    if not name or name in {".", ".."} or name != raw_name:
        return _reject("candidate_path_invalid", "候选产物文件名必须是纯文件名（不允许路径分隔符/穿越）")
    directory = Path(candidate_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return {"ok": True, "path": str(path), "bytes": len(text.encode("utf-8"))}


def save_candidate_module_spec(
    manifest: SearchManifest,
    candidate_dir: str | Path,
    spec: ModuleSpec,
) -> dict[str, Any]:
    """候选的重定位信息（子版本 ModuleSpec）只写候选目录，绝不写回原 SKILL.md。"""
    if compute_spec_hash(spec) != spec.approved_spec_hash:
        return _reject("spec_hash_mismatch", "ModuleSpec 校验 hash 与内容不符，拒绝落盘")
    payload = spec_to_dict(spec)
    return write_candidate_artifact(
        manifest,
        candidate_dir,
        CANDIDATE_SPEC_NAME,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def load_candidate_module_spec(path: str | Path) -> ModuleSpec:
    from .modules import spec_from_dict

    return spec_from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
