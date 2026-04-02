"""Tests for main.py helper functions."""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Prevent import errors from missing env/DB at import time
os.environ.setdefault("SUPABASE_URL", "http://fake")
os.environ.setdefault("SUPABASE_KEY", "fake-key")


class TestGhlRequestRetryAfter(unittest.TestCase):
    def _make_response(self, status_code, retry_after=None):
        r = MagicMock()
        r.status_code = status_code
        r.headers = {"Retry-After": str(retry_after)} if retry_after else {}
        r.text = ""
        return r

    @patch("main.time.sleep")
    @patch("main.requests.request")
    def test_uses_retry_after_header_when_present(self, mock_req, mock_sleep):
        """429 with Retry-After: 5 → sleep(5), not sleep(1)."""
        import main
        mock_req.side_effect = [
            self._make_response(429, retry_after=5),
            self._make_response(200),
        ]
        result = main._ghl_request("GET", "/test")
        mock_sleep.assert_called_once_with(5)
        self.assertEqual(result.status_code, 200)

    @patch("main.time.sleep")
    @patch("main.requests.request")
    def test_falls_back_to_exponential_when_no_header(self, mock_req, mock_sleep):
        """429 without Retry-After header → sleep(1) = 2**0."""
        import main
        mock_req.side_effect = [
            self._make_response(429),
            self._make_response(200),
        ]
        result = main._ghl_request("GET", "/test")
        mock_sleep.assert_called_once_with(1)
        self.assertEqual(result.status_code, 200)


    @patch("main.time.sleep")
    @patch("main.requests.request")
    def test_falls_back_to_exponential_when_retry_after_is_date_string(self, mock_req, mock_sleep):
        """429 with Retry-After as HTTP-date → fall back to 2**attempt, no crash."""
        import main
        mock_req.side_effect = [
            self._make_response(429, retry_after="Wed, 01 Apr 2026 12:00:00 GMT"),
            self._make_response(200),
        ]
        result = main._ghl_request("GET", "/test")
        mock_sleep.assert_called_once_with(1)  # 2**0 = 1
        self.assertEqual(result.status_code, 200)


if __name__ == "__main__":
    unittest.main()
