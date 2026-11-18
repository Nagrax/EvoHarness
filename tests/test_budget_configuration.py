"""Public configuration writes the same budget rows used by Worker admission."""

import io
import json

import pytest
from sqlalchemy import select

from test_execution_knowledge import env
from evoharness.__main__ import main
from evoharness.governance import budget
from evoharness.governance.models import BudgetLimit
from evoharness.organization.models import Membership


def test_cli_configures_both_required_layers_and_preserves_usage(tmp_path):
    url = "sqlite:///" + (tmp_path / "预算.db").as_posix()

    def call(*args):
        out = io.StringIO()
        code = main(["--db", url, "--json", *args], stdout=out)
        return code, json.loads(out.getvalue())

    assert call("init")[0] == 0
    for scope in ([], ["--space", "space-support"]):
        code, result = call(
            "budget",
            "set",
            "--org",
            "org-demo",
            "--actor",
            "founder",
            "--tokens",
            "50000",
            *scope
        )
        assert code == 0 and result["data"]["limit_tokens"] == 50000, result
    code, result = call("budget", "view")
    assert code == 0 and len(result["data"]["limits"]) == 2


def test_setter_requires_current_manager_and_trusted_namespace(env):
    app, api, *_ = env
    principal = dict(
        subject="founder", org_ids=["org-demo"], tenant_id="local", app_id="root"
    )
    assert api.set_budget_limit(
        org_id="org-demo", actor="founder", limit_tokens=0, principal=principal
    )["ok"]
    assert (
        api.set_budget_limit(
            org_id="org-demo", actor="ai-support", limit_tokens=1, principal=principal
        )["reason_code"]
        == "principal_actor_mismatch"
    )
    assert not api.set_budget_limit(
        org_id="org-demo", limit_tokens=1, principal={**principal, "app_id": "other"}
    )["ok"]
    with app.transaction() as session:
        member = session.scalar(
            select(Membership).where(Membership.principal_id == "founder")
        )
        member.status = "revoked"
    assert not api.set_budget_limit(
        org_id="org-demo", actor="founder", limit_tokens=100
    )["ok"]


@pytest.mark.parametrize("tokens", [-1, True, 1.5, "100"])
def test_invalid_limits_do_not_change_admission(env, tokens):
    app, api, *_ = env
    assert not api.set_budget_limit(
        org_id="org-demo", actor="founder", limit_tokens=tokens
    )["ok"]
    with app.transaction() as session:
        row = session.scalar(select(BudgetLimit).where(BudgetLimit.scope_kind == "org"))
        assert row.limit_tokens == 100000


def test_cost_view_hides_other_organization_limits(env):
    app, api, *_ = env
    with app.transaction() as session:
        budget.set_limit(
            session, scope_kind="org", scope_id="secret-org", limit_tokens=123
        )
        budget.set_limit(
            session, scope_kind="principal", scope_id="other-person", limit_tokens=456
        )
    result = api.view_cost(
        org_id="org-demo", principal=dict(subject="founder", org_ids=["org-demo"])
    )
    assert result["ok"], result
    assert {r["scope_id"] for r in result["data"]["limits"]} == {
        "org-demo",
        "space-support",
    }
