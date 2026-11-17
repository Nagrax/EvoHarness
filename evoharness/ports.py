"""Channel-neutral transport result; receipts remain explicit facts."""
from dataclasses import dataclass
IDEMPOTENCY_WINDOW_SECONDS = 3600
@dataclass
class TransportResult:
    outcome: str
    receipt_id: str = ""
    error: str = ""
    retry_after: float | None = None
