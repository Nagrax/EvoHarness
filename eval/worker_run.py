"""Search evaluation port backed by the product Worker and its real receipts.

Every case runs in a separate process, database and skill namespace. Inputs are
literal frozen files, never caller-selected host paths. The model and the
checker use the same adapters as the daemon. This port does not adopt skills or
send messages; a successful Run still requires the product acceptance check.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _ok(envelope):
    if not envelope.get("ok"):
        raise ValueError(envelope.get("reason_code", "worker_eval_request_failed"))
    return envelope["data"]


def _validate_inputs(files):
    if not isinstance(files, list) or len(files) > 100:
        raise ValueError("eval_input_manifest_invalid")
    seen, total = set(), 0
    for row in files:
        if not isinstance(row, dict) or set(row) != {"path", "content", "sha256"}:
            raise ValueError("eval_input_manifest_invalid")
        name = str(row["path"])
        path = PurePosixPath(name)
        if (
            not name
            or "\\" in name
            or ":" in name
            or path.is_absolute()
            or any(
                part in {"", ".", "..", ".git", ".evoharness-sandbox"}
                for part in name.split("/")
            )
            or name in seen
        ):
            raise ValueError("eval_input_path_invalid")
        if not isinstance(row["content"], str):
            raise ValueError("eval_input_manifest_invalid")
        data = row["content"].encode("utf-8")
        total += len(data)
        if total > 2_000_000 or hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise ValueError("eval_input_digest_mismatch")
        seen.add(name)


def run_case(request, directory, *, provider=None):
    """Internal process entry; provider injection exists for offline regression."""
    from sqlalchemy import select
    from agents.frontmatter import format_frontmatter
    from agents.skill_evolution import skill_content_hash
    from agents.skills import _parse_skill_file
    from evoharness.__main__ import seed_organization
    from evoharness.api import WorkApi
    from evoharness.application import Application
    from evoharness.governance.models import BudgetReservation
    from evoharness.work.agent_adapter import RestrictedAgentAdapter
    from evoharness.work.checker import TrustedPytestRunner
    from evoharness.governance.budget_gateway import BudgetGateway
    from evoharness.work.models import Artifact, ExecutionRun
    from evoharness.work.worker import WorkWorker

    files = request.get("input_files", [])
    _validate_inputs(files)
    candidate = request.get("candidate") or {}
    snapshot = candidate.get("snapshot") or {}
    name = str(snapshot.get("name") or "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
        raise ValueError("eval_skill_name_invalid")
    if candidate.get("content_hash") != skill_content_hash(snapshot):
        raise ValueError("eval_candidate_digest_mismatch")
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    skill_file = directory / "skills" / name / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    scope = dict(
        tenant="local",
        space="space-support",
        owner="ai-support",
        producer="evaluation",
        source_revision=1,
    )
    skill_file.write_text(
        format_frontmatter(
            {
                "name": name,
                "description": str(snapshot.get("description") or ""),
                "when-to-use": str(
                    snapshot.get("when_to_use") or snapshot.get("when-to-use") or ""
                ),
                "version": "1.0.0",
                "scope-json": json.dumps(scope),
            },
            str(snapshot.get("instructions") or ""),
        ),
        encoding="utf-8",
    )
    expected_file_hash = hashlib.sha256(skill_file.read_bytes()).hexdigest()
    actual = _parse_skill_file(skill_file, "scoped", str(skill_file.parent))
    rendered_snapshot = dict(name=actual.name, description=actual.description,
                             when_to_use=actual.when_to_use or "",
                             instructions=actual.prompt_template)
    if skill_content_hash(rendered_snapshot) != candidate["content_hash"]:
        raise ValueError("eval_rendered_candidate_drift")
    kind = request.get("kind", "code")
    if kind not in {"code", "qa"}:
        raise ValueError("eval_task_kind_invalid")
    # The port is process-isolated. Restore these variables in direct unit calls.
    with patch.dict(
        os.environ,
        {
            "EVOHARNESS_SKILLS_ROOT": str(skill_file.parent.parent),
            "EVOHARNESS_EVOLUTION_DIR": str(directory / "evolution"),
            "EVOHARNESS_HOLDOUT_READONLY": "1",
        },
    ):
        app = Application.open("sqlite:///" + (directory / "org.db").as_posix())
        try:
            api = WorkApi(
                app, artifacts_dir=directory / "artifacts", acceptance_policy="strict"
            )
            with app.transaction() as session:
                seed_organization(session)
            for space in ("", "space-support"):
                _ok(
                    api.set_budget_limit(
                        org_id="org-demo",
                        space_id=space,
                        actor="founder",
                        limit_tokens=1_000_000,
                    )
                )
            task = _ok(
                api.create_work_item(
                    tenant="local",
                    owner="founder",
                    title=str(request["question"]),
                    goal=str(request["question"]),
                    action="execute",
                    acceptance=[{"kind": kind}],
                    workflow_kind="delegated_delivery",
                    org_id="org-demo",
                    space_id="space-support",
                )
            )
            task_id = task["id"]
            _ok(api.transition_work_item(task_id, to_state="queued", actor="founder"))
            payload = _ok(api.run_request_payload(task_id))
            _ok(
                api.submit_approval(
                    org_id="org-demo",
                    actor_id="founder",
                    target_type="work_item",
                    target_id=str(task_id),
                    target_revision=payload["target_revision"],
                    payload=payload["payload"],
                    actions=["work.execute"],
                    grantee_id="ai-support",
                )
            )
            run_id = _ok(api.start_run(task_id, actor_id="ai-support"))["operation_id"]
            with app.transaction() as session:
                run = session.get(ExecutionRun, run_id)
                workspace = Path(run.output_root) / ("run-" + run.id[:12])
            workspace.mkdir(parents=True)
            for item in files:
                path = workspace / item["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(item["content"].encode("utf-8"))
            runner = TrustedPytestRunner(timeout_seconds=180)
            adapter = RestrictedAgentAdapter(
                provider=provider,
                gateway=BudgetGateway(app.session_factory),
                runner=runner,
                checker="search-worker",
                max_turns=int(request.get("max_turns", 16)),
            )
            result = WorkWorker(
                app.session_factory, adapter, api._authorize_adapter()
            ).process_run(run_id)
            verified = False
            review = {}
            if result["ok"]:
                _ok(
                    api.request_acceptance(
                        task_id, run_id=run_id, requester="ai-support"
                    )
                )
                review = api.review_acceptance(
                    task_id, run_id=run_id, reviewer="founder", verdict="pass", notes=""
                )
                verified = bool(review.get("ok") and review["data"]["state"] == "done")
            with app.transaction() as session:
                run = session.get(ExecutionRun, run_id)
                checkpoint = json.loads(run.checkpoint_json or "{}")
                loaded = checkpoint.get("skills", {}).get("loaded", [])
                rows = list(
                    session.scalars(select(Artifact).where(Artifact.run_id == run_id))
                )
                artifacts = [
                    {"path": row.path, "sha256": row.sha256, "kind": row.kind}
                    for row in rows
                ]
                calls = list(
                    session.scalars(
                        select(BudgetReservation).where(
                            BudgetReservation.run_id == run_id,
                            BudgetReservation.kind == "call",
                        )
                    )
                )
                cost = {
                    "used_tokens": sum(row.used_tokens for row in calls),
                    "calls": len(calls),
                    "estimated_calls": sum(bool(row.estimated) for row in calls),
                    "usage_statuses": [row.usage_status for row in calls],
                    "call_ids": [row.call_id for row in calls],
                }
            matched = next(
                (
                    row
                    for row in loaded
                    if row["name"] == name
                    and row["file_sha256"] == expected_file_hash
                    and row["skill_sha256"] == actual.content_sha256
                ),
                None,
            )
            unchanged = (
                hashlib.sha256(skill_file.read_bytes()).hexdigest()
                == expected_file_hash
            )
            events = []
            for i, entry in enumerate(checkpoint.get("done", [])):
                parts = entry.split(":", 2)
                if len(parts) >= 2:
                    denied = any(
                        refusal.startswith(
                            (
                                parts[0] + ":",
                                "tool_not_allowed:" + parts[0],
                                "tool_unimplemented:" + parts[0],
                            )
                        )
                        for refusal in adapter.last_refusals
                    )
                    events.append(
                        {
                            "tool": parts[0],
                            "ok": parts[1] == "ok",
                            "authorized": not denied,
                            "event_id": f"{run_id}:{i}",
                        }
                    )
            answer = workspace / "answer.md"
            return {
                "run_id": run_id,
                "task_id": task_id,
                "result": result,
                "loaded_snapshot": rendered_snapshot if matched and unchanged else {},
                "response_text": (
                    answer.read_text(encoding="utf-8") if answer.is_file() else ""
                ),
                "response_ref": str(answer) if answer.is_file() else "",
                "tool_events": events,
                "artifacts": artifacts,
                "artifact_verdict": {
                    "verified": verified,
                    "reason": review.get("reason_code", result.get("error", "")),
                },
                "execution_level": "artifact_verified" if verified else "tool_replay",
                "defect_fixed": verified and kind == "code",
                "tokens": cost,
                "error": "" if result["ok"] else result.get("error", "worker_failed"),
                "evidence": [str(directory / "org.db"), run_id],
                "provider_kind": "synthetic" if provider is not None else "live",
                "injection_point": "task_scoped_skill_tool",
            }
        finally:
            app.engine.dispose()


class WorkerRunPort:
    """Callable for IsolatedRunExecutor; no production data or shared skill state."""

    def __init__(
        self, *, output_root, input_files_by_case, kind="code", timeout_seconds=900
    ):
        self.output_root = Path(output_root).resolve()
        self.input_files_by_case = input_files_by_case
        self.kind = kind
        self.timeout_seconds = timeout_seconds

    def __call__(self, *, candidate, case, frozen_protocol=None):
        self.output_root.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix="worker-", dir=self.output_root))
        request = {
            "candidate": candidate,
            "question": str(case.get("question") or ""),
            "input_files": self.input_files_by_case[str(case["case_id"])],
            "kind": self.kind,
            "protocol_fingerprint": str(
                (frozen_protocol or {}).get("fingerprint") or ""
            ),
        }
        path = directory / "request.json"
        path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        result_path = directory / "result.json"
        process = subprocess.run(
            [
                sys.executable,
                "-X",
                "utf8",
                str(Path(__file__)),
                "--request",
                str(path),
                "--result",
                str(result_path),
            ],
            cwd=ROOT,
            capture_output=True,
            timeout=self.timeout_seconds,
        )
        (directory / "stdout.log").write_bytes(process.stdout)
        (directory / "stderr.log").write_bytes(process.stderr)
        if process.returncode or not result_path.is_file():
            return {"error": "worker_lost", "evidence": [str(directory / "stderr.log")]}
        return json.loads(result_path.read_text(encoding="utf-8"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args(argv)
    result = run_case(
        json.loads(args.request.read_text(encoding="utf-8")),
        args.result.parent / "runtime",
    )
    args.result.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
