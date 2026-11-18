"""Live search proposals settle their receipts, rather than reserve estimates."""
from pathlib import Path
import runpy

import pytest

from agents.skill_search.controller import MeteredProposal, SearchController


@pytest.mark.parametrize("bad", [-1, True, 1.5, float("nan"), "12"])
def test_invalid_reported_proposal_usage_is_rejected(bad):
    with pytest.raises(ValueError, match="proposal_usage_invalid"):
        MeteredProposal({}, bad)


@pytest.mark.parametrize("judge_as_receipt", [False, True])
def test_controller_uses_actual_proposal_usage_and_keeps_zero(tmp_path, judge_as_receipt):
    # Reuse the existing independent controller scenario unchanged: a real
    # SQLite budget, source manifest, two search cases and heldout validation.
    fixture = runpy.run_path(str(Path(__file__).with_name("test_skill_search_controller.py")))
    world = fixture["_world"](tmp_path)
    budget, engine = fixture["_make_budget"](tmp_path)
    attribution = fixture["Attributor"]()
    patch = fixture["PatchProvider"]()
    executor = fixture["ScriptedExecutor"]({
        "seed": fixture["SEED_SCORES"], "cand-r1": fixture["CHILD_SCORES"]})
    controller = SearchController(
        manifest=world["manifest"], cases=world["cases"], freeze=world["freeze"],
        module_spec=world["spec"], executor=executor, budget=budget,
        config=fixture["ControllerConfig"](prerequisites=dict(fixture["READY"]),
            trace_refs={cid: f"trace://{cid}" for cid in ("case-1", "case-2", "case-v")},
            max_iterations=1, max_case_workers=1),
        base_dir=world["base_dir"], trace_store=world["trace_store"],
        attributor=lambda ctx: MeteredProposal(attribution(ctx), 37),
        patch_provider=lambda brief: MeteredProposal(patch(brief), 19),
        judge=lambda ctx: MeteredProposal({}, 0) if judge_as_receipt else {"used_tokens": 0},
        isolation=fixture["FakeIsolation"](), clock=lambda: 1000.0,
    )
    try:
        assert controller.start()["ok"]
        controller.run()
        summary = controller.status()
        assert summary["costs"]["reflection"]["used_tokens"] == 37
        assert summary["costs"]["mutation"]["used_tokens"] == 19
        assert summary["costs"]["judge"]["used_tokens"] == 0
        assert budget.usage_summary()["used_total"] == 2 * 5 + 37 + 19 + 2 * 5 + 5
        assert budget.usage_summary()["reserved_open"] == 0
    finally:
        engine.dispose()
