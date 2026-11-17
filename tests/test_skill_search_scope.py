"""DE-06 搜索消费者作用域门：scope-json 技能必须同域搜索，跨域/空域拒绝。

使用与现有搜索契约相同的身份门（executable_sop）与冻结协议；只验证作用域
检查不改变既有未声明 scope-json 的技能行为。
"""
from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import agents.skill_search.contracts as sc


def _make_skill(root: Path, *, scope: dict | None, producer_kind="executable_sop") -> Path:
    directory = root / "deploy-rollout"
    directory.mkdir(parents=True)
    lines = ["---", "name: deploy-rollout", "description: 部署发布独立 SOP",
             "version: 0.3.0"]
    if producer_kind:
        lines.append(f"producer-kind: {producer_kind}")
    if scope is not None:
        lines.append("scope-json: '" + json.dumps(scope, ensure_ascii=False) + "'")
    lines.append("---")
    (directory / "SKILL.md").write_text(
        "\n".join(lines) + "\n\n# 步骤\n\n1. 检查发布单。\n", encoding="utf-8")
    return directory


def _kwargs(directory: Path, **overrides) -> dict:
    kwargs = {
        "skill_dir": directory,
        "tenant": "acme",
        "space": "sre",
        "protocol": sc.freeze_protocol(
            cases_ref="fixtures/cases@v1", cases_sha256="a" * 64,
            rubric_ref="rubrics/r@v1", rubric_sha256="b" * 64,
            verifier_sha256="c" * 64,
            models={"evaluator": "gpt-x", "params": {"temperature": 0}},
            algorithm_version="gepa-style/s1",
            splits={"S": {"ref": "S@v1", "access": "reflect"},
                    "V": {"ref": "V@v1", "access": "release-check"},
                    "H": {"ref": "H@v1", "access": "sealed"}}),
    }
    kwargs.update(overrides)
    return kwargs


def test_scoped_skill_builds_manifest_in_matching_scope(tmp_path):
    directory = _make_skill(tmp_path, scope={"tenant": "acme", "space": "sre",
                                             "owner": "alice"})
    out = sc.build_search_manifest(**_kwargs(directory))
    assert out["ok"] is True, out
    assert out["manifest"].tenant == "acme" and out["manifest"].space == "sre"


def test_cross_tenant_or_space_is_rejected(tmp_path):
    directory = _make_skill(tmp_path, scope={"tenant": "acme", "space": "sre"})
    tenant_mismatch = sc.build_search_manifest(**_kwargs(directory, tenant="other"))
    assert tenant_mismatch["ok"] is False
    assert tenant_mismatch["error"] == "skill_scope_mismatch:tenant"
    space_mismatch = sc.build_search_manifest(**_kwargs(directory, space="other"))
    assert space_mismatch["error"] == "skill_scope_mismatch:space"


def test_scoped_skill_requires_explicit_scope(tmp_path):
    directory = _make_skill(tmp_path, scope={"tenant": "acme", "space": "sre"})
    for override in ({"tenant": ""}, {"space": ""}):
        out = sc.build_search_manifest(**_kwargs(directory, **override))
        assert out["ok"] is False and out["error"] == "skill_scope_required"


def test_invalid_scope_json_is_rejected(tmp_path):
    directory = tmp_path / "broken"
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        "---\nname: broken\ndescription: d\nproducer-kind: executable_sop\n"
        "scope-json: '{bad json}'\n---\nsteps\n", encoding="utf-8")
    out = sc.build_search_manifest(**_kwargs(directory))
    assert out["ok"] is False and out["error"] == "scope_json_invalid"


def test_unscoped_legacy_skill_keeps_existing_behavior(tmp_path):
    directory = _make_skill(tmp_path, scope=None)
    out = sc.build_search_manifest(**_kwargs(directory))
    assert out["ok"] is True, out
