"""Legacy imports resolve to exactly the root schema and task writer."""
from app.models import Base, Task, Experience, Outbox
from evoharness import models
from app.domain import tasks as legacy
from evoharness.work import service

def test_channel_models_share_one_metadata_and_identity():
    assert Base is models.Base
    assert Task is models.Task
    assert Experience is models.Experience
    assert Outbox is models.Outbox
    assert Task.metadata is models.Base.metadata

def test_status_and_queries_use_the_core_implementation():
    assert legacy.set_status is service.set_status
    assert legacy.open_tasks is service.open_tasks
    assert legacy.apply_completion_signal is service.apply_completion_signal
