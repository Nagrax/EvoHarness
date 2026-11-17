"""搜索入口作用域守卫装配（DE-06）。

- ``build_scope_guard``：可选注入 session_factory；缺省从产品库配置解析；
  配置不可用返回 None（离线评测保持原行为）。
- 撤权命中时清理技能发现/usage/status 缓存，避免撤销后的正文或成绩继续被
  缓存服务；清理失败不改变拒绝结论。
"""
from __future__ import annotations


def cleanup_skill_caches(denial: dict | None = None) -> None:
    try:
        from agents import skills

        skills._cached_skills = None
    except Exception:  # noqa: BLE001
        pass
    try:
        from agents.skill_evolution import reset_usage_cache

        reset_usage_cache()
    except Exception:  # noqa: BLE001
        pass
    try:
        from agents.skill_status import reset_status_cache

        reset_status_cache()
    except Exception:  # noqa: BLE001
        pass


def build_scope_guard(session_factory=None):
    if session_factory is None:
        try:
            from evoharness.application import Application
            from evoharness.config import resolve_config

            application = Application.open(resolve_config().database_url)
        except Exception:  # noqa: BLE001 — 无产品库时离线评测不接守卫
            return None
        session_factory = application.session_factory
    from evoharness.knowledge.skill_scope_events import make_scope_guard

    return make_scope_guard(session_factory, on_denial=cleanup_skill_caches)
