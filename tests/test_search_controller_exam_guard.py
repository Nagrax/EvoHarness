"""The controller must supply exam protection to the bounded editor."""
from pathlib import Path
import runpy

import pytest
from agents.skill_search.mutator import REJ_CASE_ID, REJ_EXAM_ANSWER


@pytest.mark.parametrize("material", ["case_id", "question", "expected"])
def test_model_patch_cannot_copy_exam_through_controller(tmp_path, material):
    fixture = runpy.run_path(str(Path(__file__).with_name("test_skill_search_controller.py")))
    world = fixture["_world"](tmp_path)
    budget, engine = fixture["_make_budget"](tmp_path)
    original = fixture["PatchProvider"]()
    def contaminated(brief):
        patch = original(brief)
        patch["ops"][0]["replacement"] = str(world["cases"][0][material])
        return patch
    executor = fixture["ScriptedExecutor"]({"seed": fixture["SEED_SCORES"]})
    controller = fixture["_make_controller"](
        world, executor, budget, patch_provider=contaminated,
        config=fixture["ControllerConfig"](prerequisites=dict(fixture["READY"]),
            trace_refs={cid: f"trace://{cid}" for cid in ("case-1", "case-2", "case-v")},
            max_iterations=1, max_case_workers=1),
    )
    try:
        assert controller.start()["ok"]
        controller.run()
        assert all(candidate == "seed" for candidate, _ in executor.calls)
        assert controller.pool.get_candidate("cand-r1") is None
        reasons = {entry.reason for entry in controller._rejections_history().entries}
        assert reasons & {REJ_CASE_ID, REJ_EXAM_ANSWER}, reasons
    finally:
        engine.dispose()
