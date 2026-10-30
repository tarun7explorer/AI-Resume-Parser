"""Small security helpers for request identifiers and safe API operations."""

from __future__ import annotations

import re
import uuid

_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def new_request_id(candidate: str | None = None) -> str:
    """Accept only a short safe request ID; otherwise generate a UUID."""

    if candidate and _REQUEST_ID_PATTERN.fullmatch(candidate):
        return candidate
    return str(uuid.uuid4())
