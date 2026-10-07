"""Single-process, expiring, one-use MCP approvals. Restarts invalidate outstanding reviews."""
from copy import deepcopy
import secrets
import time

PENDING: dict[str, dict] = {}
TTL_SECONDS = 1800
MAX_PENDING = 500


def register(response_id: str, requests: list[dict]) -> str:
    now = time.monotonic()
    for token in list(PENDING):
        if PENDING[token]["expires"] <= now:
            del PENDING[token]
    if len(PENDING) >= MAX_PENDING:
        raise ValueError("Too many pending approvals. Wait for older requests to expire.")
    token = secrets.token_urlsafe(32)
    PENDING[token] = {"response_id": response_id, "requests": deepcopy(requests),
                      "expires": now + TTL_SECONDS}
    return token


def consume(token: str) -> dict:
    # No await between lookup/pop: atomic within the single ASGI event loop.
    record = PENDING.pop(token, None)
    if not record or record["expires"] <= time.monotonic():
        raise ValueError("Approval expired, already used, or invalid after restart. Inspect CAD before retrying.")
    return record
