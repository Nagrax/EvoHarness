"""ScopedSkillRef（DE-06/BP-03）：技能读取的作用域引用、命名空间与缓存键。

- ref 携带 tenant/space/owner/producer/source_revision/name；
- 托管投影的 ``scope-json`` 必须与 ref 一致，否则拒绝（两空间同名技能不串读）；
- 无 scope-json 的旧技能默认拒绝（``skill_scope_unbound``）；只有显式
  ``allow_unscoped=True`` 且 ref 为本地默认作用域时才可解析，旧名别名不跨域；
- ``cache_key`` 携带全部字段，避免同名技能共享缓存/成绩/冠军。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from evoharness.ids import sha256_json


class SkillScopeError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ScopedSkillRef:
    name: str
    tenant: str = ""
    space: str = ""
    owner: str = ""
    producer: str = ""
    source_revision: int = 0

    def namespace(self) -> str:
        return "/".join((self.tenant or "-", self.space or "-",
                         self.owner or "-", self.producer or "-"))

    def cache_key(self) -> str:
        return sha256_json({
            "name": self.name, "tenant": self.tenant, "space": self.space,
            "owner": self.owner, "producer": self.producer,
            "source_revision": int(self.source_revision)})

    def validate(self) -> None:
        if not str(self.name or "").strip():
            raise SkillScopeError("skill_name_required")


def skill_scope(skill_file: str | Path) -> dict | None:
    """读取技能 frontmatter 的 ``scope-json``；无该字段返回 None。"""
    from agents.frontmatter import parse_frontmatter
    raw = Path(skill_file).read_text(encoding="utf-8-sig")
    value = parse_frontmatter(raw).meta.get("scope-json")
    if not value:
        return None
    try:
        scope = json.loads(value)
    except ValueError:
        raise SkillScopeError("scope_json_invalid") from None
    if not isinstance(scope, dict):
        raise SkillScopeError("scope_json_invalid")
    return scope


def _scope_value(scope: dict, key: str) -> str:
    for candidate in (key, f"{key}_ref", f"{key}_id"):
        if scope.get(candidate) not in (None, ""):
            return str(scope[candidate])
    return ""


def scope_mismatch(scope: dict, ref: ScopedSkillRef) -> str | None:
    """公开版作用域比对：返回首个不一致字段名，None = 匹配。"""
    return _matches(scope, ref)


def _matches(scope: dict, ref: ScopedSkillRef) -> str | None:
    for key, expected in (("tenant", ref.tenant), ("space", ref.space),
                          ("owner", ref.owner)):
        actual = _scope_value(scope, key)
        if expected and actual and actual != expected:
            return key
    return None


def resolve_scoped_skill_file(skill_name: str, *, ref: ScopedSkillRef,
                              target: str = "active", active_dir: str = "",
                              allow_unscoped: bool = False) -> Path | None:
    """按作用域解析技能文件；跨域/未绑定默认拒绝。"""
    from agents.skill_evolution import resolve_skill_file
    ref.validate()
    if ref.name != skill_name:
        raise SkillScopeError("skill_ref_name_mismatch")
    path = resolve_skill_file(skill_name, target=target, active_dir=active_dir)
    if path is None:
        return None
    scope = skill_scope(path)
    if scope is None:
        if not allow_unscoped:
            raise SkillScopeError("skill_scope_unbound")
        if ref.tenant not in ("", "local") or ref.space not in ("", "local"):
            raise SkillScopeError("skill_scope_unbound")
        return path
    mismatched = _matches(scope, ref)
    if mismatched:
        raise SkillScopeError(f"skill_scope_mismatch:{mismatched}")
    return path
