"""F01 回归：群来源撤权对账（机器人被移出群/群解散 → revoked_at 写入与恢复）。"""
from datetime import datetime

from app.core.database import session_scope
from app.models import Source


def _seed(session, chat_id, source_type="group", revoked_at=None, thread_id=""):
    src = Source(
        tenant="default",
        owner="ou_owner",
        chat_id=chat_id,
        thread_id=thread_id,
        source_type=source_type,
        discovery="bot_list",
        read_identity="bot",
        reply_identity="bot",
        reply_allowed=True,
        revoked_at=revoked_at,
        enabled=True,
    )
    session.add(src)
    session.flush()
    return src


def _run(fn):
    with session_scope() as session:
        fn(session)
    with session_scope() as session:
        return session.query(Source).filter(Source.chat_id.like("oc_rev_%")).all()


def test_group_disappearing_from_bot_list_marks_revoked():
    from app.pipeline.daily import _reconcile_group_revocations

    def run(session):
        src = _seed(session, "oc_rev_gone")
        _reconcile_group_revocations(session, {}, [src], discovery_ok=True)

    rows = _run(run)
    assert len(rows) == 1 and rows[0].revoked_at is not None


def test_group_reappearing_clears_revocation():
    from app.pipeline.daily import _reconcile_group_revocations

    old = datetime(2026, 11, 6, 0, 0, 0)

    def run(session):
        src = _seed(session, "oc_rev_back", revoked_at=old)
        _reconcile_group_revocations(
            session,
            {"oc_rev_back": {"chat_id": "oc_rev_back", "chat_type": "group"}},
            [src],
            discovery_ok=True,
        )

    rows = _run(run)
    assert len(rows) == 1 and rows[0].revoked_at is None


def test_discovery_failure_does_not_revoke():
    from app.pipeline.daily import _reconcile_group_revocations

    def run(session):
        src = _seed(session, "oc_rev_flap")
        _reconcile_group_revocations(session, {}, [src], discovery_ok=False)

    rows = _run(run)
    assert len(rows) == 1 and rows[0].revoked_at is None


def test_p2p_source_never_revoked_by_discovery():
    from app.pipeline.daily import _reconcile_group_revocations

    def run(session):
        src = _seed(session, "oc_rev_p2p", source_type="p2p")
        _reconcile_group_revocations(session, {}, [src], discovery_ok=True)

    rows = _run(run)
    assert len(rows) == 1 and rows[0].revoked_at is None


def test_thread_source_not_revoked():
    from app.pipeline.daily import _reconcile_group_revocations

    def run(session):
        src = _seed(session, "oc_rev_g", thread_id="om_thread1")
        _reconcile_group_revocations(session, {}, [src], discovery_ok=True)

    rows = _run(run)
    assert len(rows) == 1 and rows[0].revoked_at is None
