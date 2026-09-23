"""Telegram-gated approval queue for the Hermes bridge.

The bridge server queues any non-default-allow action here as a JSON file
under ~/.hermes/bridge/approvals/pending/, posts a request to the Home
Telegram thread, and waits. A separate cron (`hermes-ops bridge-approvals`)
watches the same directory and resolves when the user replies
"yes <uuid>" or "no <uuid>" in chat. Every decision lands in
~/.hermes/bridge/approvals/audit/<date>.jsonl.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path

BRIDGE_HOME = Path(os.environ.get("HERMES_BRIDGE_HOME", "~/.hermes/bridge"))
PENDING_DIR = BRIDGE_HOME / "approvals" / "pending"
AUDIT_DIR = BRIDGE_HOME / "approvals" / "audit"
DEFAULT_TTL_SECONDS = 1800  # 30 min — accommodates mobile/distraction gaps

PENDING_DIR.mkdir(parents=True, exist_ok=True)
AUDIT_DIR.mkdir(parents=True, exist_ok=True)


def request_approval(
    *,
    tool: str,
    args: dict,
    requested_by: str,
    summary: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> tuple[str, Path]:
    """Create a pending approval entry. Returns (request_id, path).

    The caller is responsible for sending the Telegram message and for
    blocking on resolution. `wait_for()` is provided for that.
    """
    request_id = uuid.uuid4().hex[:12]
    expires_at = time.time() + ttl_seconds
    record = {
        "id": request_id,
        "tool": tool,
        "args": args,
        "requested_by": requested_by,
        "summary": summary,
        "created_at": time.time(),
        "expires_at": expires_at,
        "status": "pending",
    }
    path = PENDING_DIR / f"{request_id}.json"
    path.write_text(json.dumps(record, indent=2))
    return request_id, path


def wait_for(request_id: str, timeout: float = DEFAULT_TTL_SECONDS, poll: float = 1.0) -> dict:
    """Block until the pending record resolves. Returns the final record.

    The cron resolver (bridge-approvals.yaml) flips status to
    "approved" or "denied". On TTL expiry the record flips to "expired"
    on the next cron tick.
    """
    path = PENDING_DIR / f"{request_id}.json"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            record = json.loads(path.read_text())
            if record.get("status") != "pending":
                return record
        time.sleep(poll)
    record = json.loads(path.read_text())
    if record.get("status") == "pending":
        record["status"] = "expired"
        path.write_text(json.dumps(record, indent=2))
        audit(record)
    return record


def audit(record: dict) -> None:
    """Append a resolved record to today's audit JSONL."""
    day = time.strftime("%Y-%m-%d")
    path = AUDIT_DIR / f"{day}.jsonl"
    with path.open("a") as f:
        f.write(json.dumps(record) + "\n")


def is_expired(record: dict) -> bool:
    return time.time() > record.get("expires_at", 0)