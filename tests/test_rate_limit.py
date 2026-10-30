"""Rate-limit behavior without relying on wall-clock sleeps."""

import unittest
from unittest.mock import patch

from app.rate_limit import SlidingWindowLimiter


class SlidingWindowLimiterTests(unittest.TestCase):
    def test_allows_limit_then_returns_retry_delay(self) -> None:
        limiter = SlidingWindowLimiter(max_keys=2)
        with patch("app.rate_limit.time.monotonic", return_value=100.0):
            self.assertEqual(limiter.allow("user:a", limit=2, window_seconds=60), (True, 0))
        with patch("app.rate_limit.time.monotonic", return_value=101.0):
            self.assertEqual(limiter.allow("user:a", limit=2, window_seconds=60), (True, 0))
        with patch("app.rate_limit.time.monotonic", return_value=102.0):
            self.assertEqual(limiter.allow("user:a", limit=2, window_seconds=60), (False, 58))

    def test_expired_events_are_removed(self) -> None:
        limiter = SlidingWindowLimiter()
        with patch("app.rate_limit.time.monotonic", return_value=100.0):
            limiter.allow("account:hash", limit=1, window_seconds=60)
        with patch("app.rate_limit.time.monotonic", return_value=161.0):
            self.assertEqual(limiter.allow("account:hash", limit=1, window_seconds=60), (True, 0))

    def test_key_storage_is_bounded(self) -> None:
        limiter = SlidingWindowLimiter(max_keys=2)
        with patch("app.rate_limit.time.monotonic", return_value=100.0):
            limiter.allow("a", limit=1, window_seconds=60)
            limiter.allow("b", limit=1, window_seconds=60)
            limiter.allow("c", limit=1, window_seconds=60)
        self.assertLessEqual(len(limiter._events), 2)


if __name__ == "__main__":
    unittest.main()
