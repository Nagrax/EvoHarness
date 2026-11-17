"""Permission-preserving identities and fixed namespaces for managed projections."""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import tempfile

@dataclass(frozen=True)
class ProjectionScope:
    tenant: str
    owner: str
    space: str = ""
    visibility: str = "private"
    sensitive_level: str = "private"
    audience_json: str = "{}"

    @classmethod
    def from_payload(cls, value: dict) -> "ProjectionScope":
        tenant, owner = str(value.get("tenant") or ""), str(value.get("owner") or "")
        visibility = str(value.get("visibility") or "private")
        sensitive = str(value.get("sensitive_level") or "private")
        audience = value.get("audience") or {}
        if not tenant or not owner or visibility not in {"private", "public"}:
            raise ValueError("projection_scope_missing_or_unknown")
        if sensitive not in {"private", "internal", "public"} or not isinstance(audience, dict):
            raise ValueError("projection_scope_missing_or_unknown")
        return cls(tenant, owner, str(value.get("space_id") or ""), visibility, sensitive,
                   json.dumps(audience, ensure_ascii=False, sort_keys=True, separators=(",", ":")))

    def as_dict(self) -> dict:
        return dict(tenant=self.tenant, owner=self.owner, space_id=self.space,
                    visibility=self.visibility, sensitive_level=self.sensitive_level,
                    audience=json.loads(self.audience_json))

    @property
    def key(self) -> str:
        return hashlib.sha256(json.dumps(self.as_dict(), sort_keys=True).encode()).hexdigest()

def default_scope() -> ProjectionScope:
    from app.core.config import get_settings
    settings = get_settings()
    return ProjectionScope(settings.tenant_id, settings.feishu_owner_open_id)

def root_for(scope: ProjectionScope) -> Path:
    explicit = os.environ.get("EVOHARNESS_SKILLS_ROOT", "").strip()
    if explicit and scope == default_scope():
        return Path(explicit).resolve()
    if explicit:
        base = Path(explicit).resolve().parent
    else:
        from app.core.config import get_settings
        settings = get_settings()
        base = Path(settings.test_db_prefix or tempfile.gettempdir()).resolve() if settings.test_mode else Path(settings.evoharness_root or Path.cwd()).resolve() / ".evoharness"
    return base / "knowledge-projections" / scope.key / "skills"

def bind_root(root: Path, scope: ProjectionScope) -> None:
    root.mkdir(parents=True, exist_ok=True)
    binding = root / ".projection-scope.json"
    expected = scope.as_dict()
    if not binding.is_file() and any(root.glob("*/SKILL.md")):
        raise ValueError("legacy_projection_root_requires_scope_migration")
    try:
        with binding.open("x", encoding="utf-8") as stream:
            json.dump(expected, stream, ensure_ascii=False, sort_keys=True)
    except FileExistsError:
        if json.loads(binding.read_text(encoding="utf-8")) != expected:
            raise ValueError("projection_root_scope_mismatch")

def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".projection-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
