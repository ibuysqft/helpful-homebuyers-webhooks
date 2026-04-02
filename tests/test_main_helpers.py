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


class TestBookAppointmentNotify(unittest.TestCase):
    """book_appointment must send Appointment Set SMS after a successful booking."""

    @patch("main._ghl_post")
    @patch("main._ghl_get")
    @patch("main._verify_contact")
    @patch("main._send_followup_sms")
    def test_sends_appointment_set_sms_on_success(
        self, mock_sms, mock_verify, mock_get, mock_post
    ):
        """Successful booking triggers _send_followup_sms with 'Appointment Set'."""
        from fastapi.testclient import TestClient
        import main

        mock_verify.return_value = True

        contact_resp = MagicMock()
        contact_resp.status_code = 200
        contact_resp.json.return_value = {
            "contact": {"firstName": "Alice", "address1": "123 Main St"}
        }
        mock_get.return_value = contact_resp

        booking_resp = MagicMock()
        booking_resp.status_code = 201
        booking_resp.json.return_value = {"appointment": {"id": "appt-abc"}}
        mock_post.return_value = booking_resp

        client = TestClient(main.app)
        resp = client.post("/shelby-book-appointment", json={
            "contact_id": "contact-xyz",
            "start_time": "2026-04-10T14:00:00Z",
        })

        self.assertEqual(resp.status_code, 200)
        mock_sms.assert_called_once()
        args = mock_sms.call_args[0]
        self.assertEqual(args[1], "Appointment Set")
        self.assertEqual(args[2], "Alice")
        self.assertEqual(args[3], "123 Main St")

    @patch("main._ghl_post")
    @patch("main._ghl_get")
    @patch("main._verify_contact")
    @patch("main._send_followup_sms")
    def test_booking_succeeds_even_if_sms_fails(
        self, mock_sms, mock_verify, mock_get, mock_post
    ):
        """SMS failure must not fail the booking response."""
        from fastapi.testclient import TestClient
        import main

        mock_verify.return_value = True
        mock_sms.return_value = False

        contact_resp = MagicMock()
        contact_resp.status_code = 200
        contact_resp.json.return_value = {"contact": {"firstName": "Bob", "address1": ""}}
        mock_get.return_value = contact_resp

        booking_resp = MagicMock()
        booking_resp.status_code = 201
        booking_resp.json.return_value = {"appointment": {"id": "appt-def"}}
        mock_post.return_value = booking_resp

        client = TestClient(main.app)
        resp = client.post("/alex-book-appointment", json={
            "contact_id": "contact-abc",
            "start_time": "2026-04-11T10:00:00Z",
        })

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["success"])


class TestHealthLiveness(unittest.TestCase):
    def _client(self):
        from fastapi.testclient import TestClient
        import main
        return TestClient(main.app)

    @patch("main.requests.get")
    @patch("main._get_sb_for_health")
    def test_health_ok_when_all_pass(self, mock_sb, mock_rget):
        mock_rget.return_value = MagicMock(status_code=200)
        mock_sb.return_value = None
        resp = self._client().get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "ok")
        self.assertEqual(resp.json()["checks"]["ghl"], "ok")
        self.assertEqual(resp.json()["checks"]["supabase"], "ok")

    @patch("main.requests.get")
    @patch("main._get_sb_for_health")
    def test_health_503_when_ghl_fails(self, mock_sb, mock_rget):
        mock_rget.return_value = MagicMock(status_code=503)
        mock_sb.return_value = None
        resp = self._client().get("/health")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["status"], "degraded")
        self.assertIn("error", resp.json()["checks"]["ghl"])

    @patch("main.requests.get")
    @patch("main._get_sb_for_health")
    def test_health_503_when_supabase_fails(self, mock_sb, mock_rget):
        mock_rget.return_value = MagicMock(status_code=200)
        mock_sb.side_effect = Exception("connection refused")
        resp = self._client().get("/health")
        self.assertEqual(resp.status_code, 503)
        self.assertIn("error", resp.json()["checks"]["supabase"])


if __name__ == "__main__":
    unittest.main()
