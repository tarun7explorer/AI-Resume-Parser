"""Request-correlation and secret-configuration safety tests."""

import unittest

from app.security import new_request_id


class RequestIdTests(unittest.TestCase):
    def test_safe_client_id_is_preserved(self) -> None:
        self.assertEqual(new_request_id("request-123_abc"), "request-123_abc")

    def test_untrusted_or_oversized_id_is_replaced(self) -> None:
        generated = new_request_id("bad value\r\nInjected: yes")
        self.assertRegex(generated, r"^[0-9a-f-]{36}$")
        self.assertRegex(new_request_id("x" * 65), r"^[0-9a-f-]{36}$")


if __name__ == "__main__":
    unittest.main()
