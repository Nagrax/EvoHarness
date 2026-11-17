"""UTC storage clock shared by all product entry points."""
from datetime import datetime, timezone

def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)
