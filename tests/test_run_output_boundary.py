from test_execution_knowledge import env


def test_hosted_run_cannot_create_an_arbitrary_host_directory(env, tmp_path):
    _, api, task_id, _, _ = env
    outside = tmp_path / "outside-artifacts"
    result = api.start_run(
        task_id,
        output_root=str(outside),
        principal=dict(subject="ai-support", org_ids=["org-demo"]),
    )
    assert not result["ok"] and result["reason_code"] == "output_root_outside_artifacts"
    assert not outside.exists()


def test_denied_local_request_has_no_filesystem_side_effect(env, tmp_path):
    _, api, *_ = env
    output = tmp_path / "denied"
    assert not api.start_run(999999, actor_id="founder", output_root=str(output))["ok"]
    assert not output.exists()
