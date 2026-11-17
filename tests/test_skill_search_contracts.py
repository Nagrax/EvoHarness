"""DE-29 契约回归：SearchManifest 身份门、完整 bundle 冻结、评测复用与冻结只读。

覆盖（tasks.json DE-29 acceptance_scenarios / SKILL-EVOLUTION-SEARCH §5/§6 /
NATIVE-ARCHITECTURE-INTEGRATION §4/§5/§8）：

- S1：正文不变而辅助脚本/工具集变化 → 新 bundle id + 新协议指纹，旧评测被拒绝复用；
  manifest 冻结后源漂移立即被 verify_integrity 发现（blocked_source_changed /
  bundle_composition_changed）
- 身份门：托管投影（write_managed_projection 产物）与身份不明目标建立
  SearchManifest 被拒绝并给可解释原因；executable_sop 直接可建；
  direct 须携带责任人确认标记
- 冻结只读：manifest 冻结的源目录（SKILL.md + 辅助脚本）在搜索期间写入被拒；
  候选重定位信息只落搜索命名空间，绝不写回原技能目录
- 与 DE-18 publish/CAS 的组合：manifest 冻结的 expected_version /
  expected_active_sha256 与 publish_skill_bundle 同口径，可直接用于显式采用
- 同名跨空间：tenant/space 进入搜索命名空间分段，同名技能不混账；
  原字符串名 CLI（resolve_skill_file / discover_skills）仍兼容，champion/
  status/tier 台账零写入

沙箱纪律对齐 test_skill_lifecycle_seams.py：monkeypatch.chdir + Path.home 补丁
+ reset_skill_cache()/reset_tier_cache()。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents import skill_evolution as se
from agents import skill_tiers as st
from agents.skill_search import contracts as sc
from agents.skill_search import modules as sm
from agents.skills import discover_skills, reset_skill_cache
from evoharness.knowledge.release import _canonical_bundle_hash, publish_skill_bundle

EVOLUTION_DIR_ENVS = ("EVOHARNESS_EVOLUTION_DIR", "EVOHARNESS_SKILLS_ROOT")


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    for env in EVOLUTION_DIR_ENVS:
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("EVOHARNESS_IR_MODE", "off")
    reset_skill_cache()
    st.reset_tier_cache()
    yield tmp_path
    reset_skill_cache()
    st.reset_tier_cache()


# ── 通用小工具 ──────────────────────────────────────────────────


def _make_sop_skill(
    root: Path,
    slug: str,
    *,
    name: str = "deploy-rollout",
    producer_kind: str = "executable_sop",
    instructions: str = "# 步骤\n\n1. 检查发布单。\n2. 灰度 5% 观察 10 分钟。\n3. 全量并回填结果。\n",
    allowed_tools: str = "",
    version: str = "0.3.0",
    aux_script: str | None = "helper.py",
    script_body: str = "print('verify rollout')\n",
) -> Path:
    skill_dir = root / ".evoharness" / "skills" / slug
    skill_dir.mkdir(parents=True, exist_ok=True)
    meta_lines = [
        "---",
        f"name: {name}",
        "description: 部署发布独立 SOP",
        f"version: {version}",
    ]
    if producer_kind:
        meta_lines.append(f"producer-kind: {producer_kind}")
    if allowed_tools:
        meta_lines.append(f"allowed-tools: {allowed_tools}")
    meta_lines.append("---")
    text = "\n".join(meta_lines) + "\n\n" + instructions
    (skill_dir / "SKILL.md").write_text(text, encoding="utf-8")
    if aux_script:
        (skill_dir / aux_script).write_text(script_body, encoding="utf-8")
    return skill_dir


def _manifest_kwargs(skill_dir: Path, **overrides) -> dict:
    kwargs = {
        "skill_dir": skill_dir,
        "tenant": "acme",
        "space": "sre",
        "protocol": sc.freeze_protocol(
            cases_ref="fixtures/rollout-cases@v3",
            cases_sha256="a" * 64,
            rubric_ref="rubrics/rollout@v2",
            rubric_sha256="b" * 64,
            verifier_sha256="c" * 64,
            models={"evaluator": "gpt-x", "params": {"temperature": 0}},
            algorithm_version="gepa-style/s1",
            splits={"S": {"ref": "S@v3", "access": "reflect"}, "V": {"ref": "V@v1", "access": "release-check"}, "H": {"ref": "H@v1", "access": "sealed"}},
        ),
    }
    kwargs.update(overrides)
    return kwargs


# ── 身份门：只有明确独立执行 SOP 可建立 SearchManifest ───────────────────


def test_executable_sop_manifest_freezes_full_bundle(sandbox):
    skill_dir = _make_sop_skill(sandbox, "deploy-rollout", allowed_tools="bash,git")
    out = sc.build_search_manifest(**_manifest_kwargs(skill_dir))
    assert out["ok"] is True, out
    manifest = out["manifest"]
    # 身份与 CAS 口径
    assert manifest.producer_kind == "executable_sop"
    assert manifest.expected_version == "0.3.0"
    raw = (skill_dir / "SKILL.md").read_bytes()
    # 原始字节口径（= publish_skill_bundle 的 expected_active_sha256）
    assert manifest.expected_active_sha256 == hashlib.sha256(raw).hexdigest()
    # 工作文本口径（read_text utf-8-sig，与 agents 全库一致；Windows 下与字节口径分离）
    working_text = (skill_dir / "SKILL.md").read_text(encoding="utf-8-sig")
    assert manifest.skill_text_sha256 == hashlib.sha256(working_text.encode("utf-8")).hexdigest()
    # DE-18 skill_content_hash 复用（四个内容字段口径；正文取 read_text 工作文本，
    # 与 report 侧 discover_skills 的 prompt_template 同源）
    from agents.frontmatter import parse_frontmatter

    body = parse_frontmatter(working_text).body
    assert manifest.skill_content_hash == se.skill_content_hash(
        {
            "name": "deploy-rollout",
            "description": "部署发布独立 SOP",
            "when_to_use": "",
            "instructions": body,
        }
    )
    # 完整 bundle：正文 + 辅助脚本 + 工具集都在冻结清单里
    paths = {f["path"]: f for f in manifest.files}
    assert set(paths) == {"SKILL.md", "helper.py"}
    assert paths["helper.py"]["kind"] == "script"
    assert manifest.toolset["allowed_tools"] == "bash,git"
    assert manifest.bundle_id.startswith("sbundle-")
    assert manifest.bundle_hash and manifest.eval_protocol_fingerprint
    # 协议（Case/rubric/模型环境/S-V-H 范围）原样冻结
    assert manifest.protocol["cases"]["ref"] == "fixtures/rollout-cases@v3"
    assert manifest.protocol["splits"]["H"]["access"] == "sealed"
    # module_spec 绑定：spec 来自冻结种子才被接受
    text = (skill_dir / "SKILL.md").read_text(encoding="utf-8-sig")
    spec = sm.build_module_spec(text)["spec"]
    out2 = sc.build_search_manifest(**_manifest_kwargs(skill_dir, module_spec=spec))
    assert out2["ok"] is True and out2["manifest"].module_spec_hash == spec.approved_spec_hash
    stale_spec = sm.spec_from_dict(sm.spec_to_dict(spec))
    stale_spec.source_text_sha256 = "0" * 64
    out3 = sc.build_search_manifest(**_manifest_kwargs(skill_dir, module_spec=stale_spec))
    assert out3["ok"] is False and out3["error"] == "module_spec_source_mismatch"


def test_projection_and_unknown_targets_rejected_with_reason(sandbox):
    # 托管投影：走 DE-06 源服务写入链，producer-kind=projection + .projection.json
    skills_root = sandbox / "projection-root"
    projection_state = sandbox / "projection-state"
    result = se.write_managed_projection(
        skills_root=skills_root,
        evolution_root=projection_state,
        name="feishu-hr",
        description="hr 领域的经验投影",
        instructions="# hr 常见问题\n\n年假在 OA 提交。\n",
        when_to_use="当用户询问 hr 领域的事务时使用",
        scope={"tenant": "t1", "owner": "o1"},
        source_revisions=[{"id": 1, "version": 1}],
        actor="sync-test",
    )
    assert result["ok"] is True, result
    out = sc.build_search_manifest(**_manifest_kwargs(skills_root / "feishu-hr"))
    assert out["ok"] is False
    assert out["error"] == "projection_not_searchable"
    assert "projection" in out["reason"] or "投影" in out["reason"]
    assert any("sidecar" in item or "producer-kind" in item for item in out["evidence"])

    # 身份不明：无 producer-kind 的存量文件拒绝自动认领
    plain_dir = _make_sop_skill(sandbox, "plain-skill", producer_kind="", name="plain-skill")
    out2 = sc.build_search_manifest(**_manifest_kwargs(plain_dir))
    assert out2["ok"] is False
    assert out2["error"] == "unknown_identity_not_searchable"

    # direct 不带责任人确认 → 拒绝；带确认标记 → 可建立
    direct_dir = _make_sop_skill(sandbox, "direct-skill", producer_kind="direct", name="direct-skill")
    out3 = sc.build_search_manifest(**_manifest_kwargs(direct_dir))
    assert out3["ok"] is False and out3["error"] == "owner_confirmation_required"
    out4 = sc.build_search_manifest(
        **_manifest_kwargs(
            direct_dir,
            owner_confirmation={"confirmed_by": "sre-owner", "ref": "APPROVAL-77"},
        )
    )
    assert out4["ok"] is True and out4["manifest"].owner_confirmation["confirmed_by"] == "sre-owner"
    # 责任人确认标记畸形（缺 confirmed_by）→ fail-closed
    out5 = sc.build_search_manifest(
        **_manifest_kwargs(direct_dir, owner_confirmation={"ref": "APPROVAL-77"})
    )
    assert out5["ok"] is False and out5["error"] == "owner_confirmation_invalid"


# ── S1：bundle 组成变化 → 新 bundle id + 新协议指纹，旧评测不可复用 ────────


def test_s1_aux_script_change_rejects_old_evaluation_reuse(sandbox):
    skill_dir = _make_sop_skill(sandbox, "deploy-rollout")
    first = sc.build_search_manifest(**_manifest_kwargs(skill_dir))
    assert first["ok"] is True
    manifest = first["manifest"]

    # 评测按 manifest 冻结身份入库
    evaluation = sc.attach_bundle_identity(manifest, {"evaluation_id": "e-1", "score": 0.8})
    assert evaluation["bundle_hash"] == manifest.bundle_hash
    assert evaluation["bundle_id"] == manifest.bundle_id
    # 冻结期内同 bundle/协议 → 允许复用（不重复烧评测）
    assert manifest.check_evaluation_reuse(evaluation)["ok"] is True

    # 正文不变，辅助脚本变化
    (skill_dir / "helper.py").write_text("print('verify rollout v2 with retry')\n", encoding="utf-8")
    drift = manifest.verify_integrity()
    assert drift["ok"] is False
    assert drift["reason"] == "bundle_composition_changed"
    assert {"path": "helper.py", "kind": "modified"} in drift["drifted"]
    assert drift["skill_source_changed"] is False

    # 新冻结：新 bundle id + 新协议指纹；正文内容 hash 不变（证明 bundle 覆盖面 > 正文）
    second = sc.build_search_manifest(**_manifest_kwargs(skill_dir))
    assert second["ok"] is True
    manifest2 = second["manifest"]
    assert manifest2.bundle_id != manifest.bundle_id
    assert manifest2.bundle_hash != manifest.bundle_hash
    assert manifest2.eval_protocol_fingerprint != manifest.eval_protocol_fingerprint
    assert manifest2.skill_content_hash == manifest.skill_content_hash

    # 旧评测对新 bundle 被拒绝复用（S1 契约核心断言）
    reuse = manifest2.check_evaluation_reuse(evaluation)
    assert reuse["ok"] is False
    assert reuse["error"] == "bundle_changed"
    # 未绑定 bundle 的评测记录 fail-closed 拒绝
    unbound = manifest2.check_evaluation_reuse({"evaluation_id": "legacy", "score": 0.9})
    assert unbound["ok"] is False and unbound["error"] == "evaluation_not_bound_to_bundle"


def test_s1_toolset_change_produces_new_bundle_with_same_body_hash(sandbox):
    skill_dir = _make_sop_skill(sandbox, "deploy-rollout", allowed_tools="bash")
    first = sc.build_search_manifest(**_manifest_kwargs(skill_dir))
    assert first["ok"] is True
    manifest = first["manifest"]

    # 只改 frontmatter 的工具声明（正文四字段不动）
    text = (skill_dir / "SKILL.md").read_text(encoding="utf-8-sig")
    (skill_dir / "SKILL.md").write_text(text.replace("allowed-tools: bash", "allowed-tools: bash,web"), encoding="utf-8")
    drift = manifest.verify_integrity()
    assert drift["ok"] is False and drift["reason"] == "blocked_source_changed"

    second = sc.build_search_manifest(**_manifest_kwargs(skill_dir))
    manifest2 = second["manifest"]
    assert manifest2.bundle_id != manifest.bundle_id
    assert manifest2.toolset["allowed_tools"] == "bash,web"
    # 正文内容 hash 不变（工具集独立进入 bundle 身份，不靠正文 hash 兜底）
    assert manifest2.skill_content_hash == manifest.skill_content_hash
    assert manifest2.eval_protocol_fingerprint != manifest.eval_protocol_fingerprint


def test_s1_protocol_refreeze_rejects_reuse_even_when_bundle_identical(sandbox):
    skill_dir = _make_sop_skill(sandbox, "deploy-rollout")
    first = sc.build_search_manifest(**_manifest_kwargs(skill_dir))
    manifest = first["manifest"]
    evaluation = sc.attach_bundle_identity(manifest, {"evaluation_id": "e-1"})
    # 同一 bundle 但协议（Case 集）更换 → 新协议指纹 → 旧评测不可复用
    second = sc.build_search_manifest(
        **_manifest_kwargs(skill_dir, protocol=sc.freeze_protocol(cases_ref="fixtures/rollout-cases@v4"))
    )
    manifest2 = second["manifest"]
    assert manifest2.bundle_hash == manifest.bundle_hash
    assert manifest2.eval_protocol_fingerprint != manifest.eval_protocol_fingerprint
    reuse = manifest2.check_evaluation_reuse(evaluation)
    assert reuse["ok"] is False and reuse["error"] == "protocol_changed"


# ── 冻结期只读：源目录不可写，候选产物只进搜索命名空间 ───────────────────


def test_frozen_source_readonly_and_candidate_artifacts_isolated(sandbox):
    skill_dir = _make_sop_skill(sandbox, "deploy-rollout")
    out = sc.build_search_manifest(**_manifest_kwargs(skill_dir))
    manifest = out["manifest"]
    assert sc.save_manifest(manifest, base_dir=sandbox / "search-root")["ok"] is True

    # SKILL.md / 辅助脚本 / 源目录内任意路径：写入一律拒绝
    for target in (skill_dir / "SKILL.md", skill_dir / "helper.py", skill_dir / "extra.txt"):
        guard = sc.check_frozen_write(manifest, target)
        assert guard["ok"] is False and guard["error"] == "frozen_source_readonly"
    # 候选目录与搜索命名空间可写
    candidate_dir = sandbox / "search-root" / "acme" / "sre" / "deploy-rollout" / manifest.search_id / "candidates" / "c-1"
    assert sc.check_frozen_write(manifest, candidate_dir)["ok"] is True

    before_skill_bytes = (skill_dir / "SKILL.md").read_bytes()
    before_dir_listing = sorted(p.name for p in skill_dir.iterdir())

    # 候选文本 + 重定位信息落候选目录
    text = (skill_dir / "SKILL.md").read_text(encoding="utf-8-sig")
    spec = sm.build_module_spec(text)["spec"]
    child = sm.relocate_modules(spec, text)["spec"]
    write1 = sc.write_candidate_artifact(manifest, candidate_dir, "candidate-text.md", text)
    assert write1["ok"] is True
    write2 = sc.save_candidate_module_spec(manifest, candidate_dir, child)
    assert write2["ok"] is True
    loaded = sc.load_candidate_module_spec(candidate_dir / "module-spec.json")
    assert loaded.approved_spec_hash == child.approved_spec_hash

    # 原技能目录零污染：字节不变、无新增文件（重定位信息没写回去）
    assert (skill_dir / "SKILL.md").read_bytes() == before_skill_bytes
    assert sorted(p.name for p in skill_dir.iterdir()) == before_dir_listing
    # 即便有人把重定位旁车指到源目录，守卫也拒绝
    assert sc.save_candidate_module_spec(manifest, skill_dir, child)["ok"] is False
    # 候选产物文件名穿越防护：纯文件名之外一律拒绝，产物不会逃出候选目录
    assert sc.write_candidate_artifact(manifest, candidate_dir, "../escape.md", "x")["ok"] is False
    assert sc.write_candidate_artifact(manifest, candidate_dir, "sub/evil.md", "x")["ok"] is False
    assert not (candidate_dir.parent / "escape.md").exists()

    # manifest 可从盘上读回且身份一致
    loaded_manifest = sc.load_manifest(sandbox / "search-root" / "acme" / "sre" / "deploy-rollout" / manifest.search_id / "manifest.json")
    assert loaded_manifest.bundle_hash == manifest.bundle_hash
    assert loaded_manifest.verify_integrity()["ok"] is True


def test_manifest_default_namespace_follows_evolution_dir(sandbox):
    skill_dir = _make_sop_skill(sandbox, "deploy-rollout")
    manifest = sc.build_search_manifest(**_manifest_kwargs(skill_dir))["manifest"]
    out = sc.save_manifest(manifest)  # 默认跟随 get_evolution_dir()/search
    expected = sandbox / ".evoharness" / "skill-evolution" / "search" / "acme" / "sre" / "deploy-rollout" / manifest.search_id / "manifest.json"
    assert Path(out["path"]) == expected
    assert expected.is_file()


# ── 与 DE-18 publish/CAS 的组合：同一 hash 口径 ─────────────────────────


def test_manifest_cas_composes_with_publish_skill_bundle(sandbox):
    skill_dir = _make_sop_skill(sandbox, "deploy-rollout")
    manifest = sc.build_search_manifest(**_manifest_kwargs(skill_dir))["manifest"]

    # 冻结期间无人改动 → publish 用 manifest 的 CAS 键直接成功（显式采用认证候选）
    bundle = {
        "name": "deploy-rollout",
        "description": "部署发布独立 SOP（认证候选）",
        "when_to_use": "发布窗口执行",
        "instructions": "# 步骤\n\n1. 检查发布单。\n2. 灰度 10% 观察 15 分钟。\n3. 全量并回填结果。\n",
        "producer_kind": "direct",
        "sha256": _canonical_bundle_hash(
            {
                "name": "deploy-rollout",
                "description": "部署发布独立 SOP（认证候选）",
                "when_to_use": "发布窗口执行",
                "instructions": "# 步骤\n\n1. 检查发布单。\n2. 灰度 10% 观察 15 分钟。\n3. 全量并回填结果。\n",
            }
        ),
    }
    out = publish_skill_bundle(
        skill_name="deploy-rollout",
        bundle=bundle,
        expected_version=manifest.expected_version,
        expected_active_sha256=manifest.expected_active_sha256,
        actor="search-adopt",
    )
    assert out.get("ok") is True, out
    # 实际加载回执与磁盘一致（发布事务口径）
    assert out["sha256"] == hashlib.sha256((skill_dir / "SKILL.md").read_bytes()).hexdigest()

    # 采用后源已前进：旧 manifest 的 CAS 键失效 → 再次采用（陈旧证书）被 CAS 拒绝
    out2 = publish_skill_bundle(
        skill_name="deploy-rollout",
        bundle=bundle,
        expected_version=manifest.expected_version,
        expected_active_sha256=manifest.expected_active_sha256,
        actor="search-adopt",
    )
    assert out2.get("ok") is False and out2["error"] == "source_changed"

    # 错误的 expected_active_sha256 → 同样拒绝（fail-closed CAS）
    out3 = publish_skill_bundle(
        skill_name="deploy-rollout",
        bundle=bundle,
        expected_active_sha256="f" * 64,
        actor="search-adopt",
    )
    assert out3.get("ok") is False and out3["error"] == "source_changed"


# ── 同名跨空间不混账；原字符串名 CLI 保持兼容；tier/champion 零写入 ────────


def test_same_name_across_spaces_separate_ledgers_and_cli_compat(sandbox):
    dir_a = _make_sop_skill(sandbox, "deploy-rollout", name="deploy-rollout")
    dir_b = sandbox / "other-root" / "deploy-rollout"
    dir_b.mkdir(parents=True)
    text_b = (
        "---\nname: deploy-rollout\ndescription: 另一空间的同名 SOP\nversion: 1.0.0\nproducer-kind: executable_sop\n---\n\n# 步骤\n\n不同空间的内容。\n"
    )
    (dir_b / "SKILL.md").write_text(text_b, encoding="utf-8")

    manifest_a = sc.build_search_manifest(**_manifest_kwargs(dir_a, space="sre"))["manifest"]
    manifest_b = sc.build_search_manifest(**_manifest_kwargs(dir_b, space="platform"))["manifest"]
    assert manifest_a.bundle_hash != manifest_b.bundle_hash  # 内容不同 → bundle 不同
    assert manifest_a.lineage == manifest_b.lineage == "deploy-rollout"  # 同名同 lineage

    sc.save_manifest(manifest_a, base_dir=sandbox / "search-root")
    sc.save_manifest(manifest_b, base_dir=sandbox / "search-root")
    ns_a = sandbox / "search-root" / "acme" / "sre" / "deploy-rollout" / manifest_a.search_id
    ns_b = sandbox / "search-root" / "acme" / "platform" / "deploy-rollout" / manifest_b.search_id
    assert ns_a != ns_b and (ns_a / "manifest.json").is_file() and (ns_b / "manifest.json").is_file()
    # 空间 B 的 manifest 不覆盖 A：各自读回，scope/seed 不串账
    back_a = sc.load_manifest(ns_a / "manifest.json")
    back_b = sc.load_manifest(ns_b / "manifest.json")
    assert back_a.space == "sre" and back_b.space == "platform"
    assert back_a.expected_active_sha256 != back_b.expected_active_sha256

    # 原字符串名 CLI 仍兼容：搜索建立不改任何名称索引行为
    found = se.resolve_skill_file("deploy-rollout", target="active")
    assert found is not None and Path(found) == dir_a / "SKILL.md"
    names = {s.name for s in discover_skills()}
    assert "deploy-rollout" in names
    # 搜索期间 champion/status/tier 台账零写入（S1 不碰生产状态）
    evolution_dir = sandbox / ".evoharness" / "skill-evolution"
    assert not (evolution_dir / "tier_audit.jsonl").exists()
    assert not (evolution_dir / "online-eval" / "champions.json").exists()
