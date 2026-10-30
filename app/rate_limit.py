"""Small bounded process-local rate limiter for the single-instance pilot."""

from __future__ import annotations

import math
import time
from collections import OrderedDict, deque
from threading import Lock

from fastapi import Request

from app.exceptions import ApiError


class SlidingWindowLimiter:
    """Track request timestamps without retaining raw account identifiers."""

    def __init__(self, *, max_keys: int = 8192) -> None:
        self._max_keys = max_keys
        self._events: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = Lock()

    def allow(self, key: str, *, limit: int, window_seconds: int) -> tuple[bool, int]:
        """Return whether a request is allowed and the retry delay in seconds."""

        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            events = self._events.get(key)
            if events is None:
                if len(self._events) >= self._max_keys:
                    self._events.popitem(last=False)
                events = deque()
                self._events[key] = events
            else:
                self._events.move_to_end(key)

            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= limit:
                return False, max(1, math.ceil(events[0] + window_seconds - now))
            events.append(now)
            return True, 0


def enforce_request_limit(request: Request, key: str, *, limit: int, window_seconds: int) -> None:
    """Raise a standard API response when a request bucket is exhausted."""

    limiter: SlidingWindowLimiter = request.app.state.rate_limiter
    allowed, retry_after = limiter.allow(key, limit=limit, window_seconds=window_seconds)
    if not allowed:
        raise ApiError(429, "rate_limited", "Too many requests. Wait before trying again.", retry_after=retry_after)
