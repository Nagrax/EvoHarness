"""Knowledge writes preserve source quality and the complete ownership boundary."""
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evoharness.models import Base
from evoharness.knowledge.models import KnowledgeClaim
from evoharness.knowledge.service import KnowledgeError, record_claim


@pytest.fixture
def session():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def claim(session, **changes):
    fields = dict(org_id='org-a', space_id='space-a', owner_scope='alice',
                  subject='退款规则', value='7天', source_refs=['doc:policy@1'])
    fields.update(changes)
    return record_claim(session, **fields)


@pytest.mark.parametrize('refs', [[''], ['  '], ['doc:ok', ''], [None], 'doc:policy'])
def test_blank_or_malformed_source_cannot_be_active(session, refs):
    with pytest.raises(KnowledgeError, match='claim_source'):
        claim(session, source_refs=refs)
    assert session.query(KnowledgeClaim).count() == 0


@pytest.mark.parametrize('changes', [dict(org_id='org-b'), dict(space_id='space-b'),
                                    dict(owner_scope='bob')])
def test_supersede_cannot_cross_ownership(session, changes):
    old = claim(session)
    with pytest.raises(KnowledgeError, match='supersede_scope_mismatch'):
        claim(session, supersedes=old.claim_id, value='30天', **changes)
    session.refresh(old)
    assert old.state == 'active'
    assert session.query(KnowledgeClaim).count() == 1


def test_superseded_revision_cannot_gain_a_second_successor(session):
    old = claim(session)
    first = claim(session, supersedes=old.claim_id, value='15天')
    with pytest.raises(KnowledgeError, match='superseded_claim_not_current'):
        claim(session, supersedes=old.claim_id, value='30天')
    session.refresh(first)
    assert first.state == 'active'
    assert session.query(KnowledgeClaim).count() == 2


@pytest.mark.parametrize('changes', [dict(org_id='org-b'), dict(owner_scope='bob')])
def test_unrelated_owner_does_not_mutate_conflict_state(session, changes):
    old = claim(session)
    other = claim(session, value='30天', **changes)
    session.refresh(old)
    assert old.state == other.state == 'active'


def test_third_conflicting_source_remains_a_conflict(session):
    rows = [claim(session, value=value, source_refs=[f'doc:{i}'])
            for i, value in enumerate(['7天', '15天', '30天'])]
    for row in rows:
        session.refresh(row)
        assert row.state == 'conflicting'
