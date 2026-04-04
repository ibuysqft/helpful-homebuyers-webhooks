"""Tests for surplus_tracks module."""
import os
import sys
import unittest
from unittest.mock import MagicMock, call, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import surplus_tracks


def _payload(outcome: str, contact_id: str = "cid_123", call_id: str = "call_abc") -> dict:
    """Build a minimal Retell call-ended payload."""
    return {
        "call_id":    call_id,
        "duration_ms": 45000,
        "transcript": "Hello...",
        "call_analysis": {
            "call_summary": "Test summary",
            "custom_analysis_data": {
                "call_outcome": outcome,
                "contact_id":   contact_id,
            },
        },
        "retell_llm_dynamic_variables": {
            "contact_id": contact_id,
        },
    }


class TestHandleCallOutcome(unittest.TestCase):

    def setUp(self):
        # Patch all GHL I/O so no real HTTP calls are made
        self.mock_add_note  = patch("surplus_tracks._add_note").start()
        self.mock_add_task  = patch("surplus_tracks._add_task").start()
        self.mock_apply_tags = patch("surplus_tracks._apply_tags", return_value=True).start()
        self.mock_find_opp  = patch("surplus_tracks._find_surplus_opp", return_value="opp_999").start()
        self.mock_move_opp  = patch("surplus_tracks._move_opp", return_value=True).start()
        self.mock_stage_id  = patch("surplus_tracks._stage_id", side_effect=lambda n: f"sid_{n[:8]}").start()

    def tearDown(self):
        patch.stopall()

    # ── interested ────────────────────────────────────────────────────────────

    def test_interested_adds_note_no_stage_move(self):
        result = surplus_tracks.handle_call_outcome(_payload("interested"))
        self.assertTrue(result["success"])
        self.mock_add_note.assert_called_once()
        self.mock_move_opp.assert_not_called()

    def test_interested_returns_correct_fields(self):
        result = surplus_tracks.handle_call_outcome(_payload("interested"))
        self.assertEqual(result["outcome"], "interested")
        self.assertEqual(result["contact_id"], "cid_123")
        self.assertFalse(result["stage_moved"])
        self.assertFalse(result["tag_applied"])
        self.assertFalse(result["task_added"])

    # ── agreement_sent ────────────────────────────────────────────────────────

    def test_agreement_sent_moves_stage(self):
        result = surplus_tracks.handle_call_outcome(_payload("agreement_sent"))
        self.mock_move_opp.assert_called_once_with("opp_999", surplus_tracks.STAGE_AGREEMENT_SENT)
        self.assertTrue(result["stage_moved"])

    def test_agreement_sent_no_tag(self):
        surplus_tracks.handle_call_outcome(_payload("agreement_sent"))
        self.mock_apply_tags.assert_not_called()

    # ── agreement_signed ─────────────────────────────────────────────────────

    def test_agreement_signed_moves_stage_and_tags(self):
        result = surplus_tracks.handle_call_outcome(_payload("agreement_signed"))
        self.mock_move_opp.assert_called_once_with("opp_999", surplus_tracks.STAGE_AGREEMENT_SENT)
        self.mock_apply_tags.assert_called_once_with("cid_123", [surplus_tracks.TAG_AGREEMENT_SIGNED])
        self.assertTrue(result["stage_moved"])
        self.assertTrue(result["tag_applied"])

    # ── not_interested ────────────────────────────────────────────────────────

    def test_not_interested_moves_to_nurture(self):
        result = surplus_tracks.handle_call_outcome(_payload("not_interested"))
        self.mock_move_opp.assert_called_once_with("opp_999", surplus_tracks.STAGE_NOT_INTERESTED)
        self.assertTrue(result["stage_moved"])

    # ── no_answer ─────────────────────────────────────────────────────────────

    def test_no_answer_adds_task_no_stage_move(self):
        result = surplus_tracks.handle_call_outcome(_payload("no_answer"))
        self.mock_add_task.assert_called_once()
        self.mock_move_opp.assert_not_called()
        self.assertTrue(result["task_added"])
        self.assertFalse(result["stage_moved"])

    def test_no_answer_task_title_contains_reschedule(self):
        surplus_tracks.handle_call_outcome(_payload("no_answer"))
        task_title = self.mock_add_task.call_args[0][1]
        self.assertIn("reschedule", task_title.lower())

    # ── dnc ───────────────────────────────────────────────────────────────────

    def test_dnc_moves_to_dead(self):
        result = surplus_tracks.handle_call_outcome(_payload("dnc"))
        self.mock_move_opp.assert_called_once_with("opp_999", surplus_tracks.STAGE_DNC)
        self.assertTrue(result["stage_moved"])

    # ── missing contact_id ────────────────────────────────────────────────────

    def test_no_contact_id_returns_error(self):
        payload = _payload("interested", contact_id="")
        payload["retell_llm_dynamic_variables"]["contact_id"] = ""
        payload["call_analysis"]["custom_analysis_data"]["contact_id"] = ""
        result = surplus_tracks.handle_call_outcome(payload)
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "no contact_id")
        self.mock_add_note.assert_not_called()

    # ── note always written ───────────────────────────────────────────────────

    def test_note_written_for_every_outcome(self):
        for outcome in ("interested", "agreement_sent", "agreement_signed",
                        "not_interested", "no_answer", "dnc"):
            with self.subTest(outcome=outcome):
                self.mock_add_note.reset_mock()
                surplus_tracks.handle_call_outcome(_payload(outcome))
                self.mock_add_note.assert_called_once()

    # ── unknown outcome ───────────────────────────────────────────────────────

    def test_unknown_outcome_adds_note_no_move(self):
        result = surplus_tracks.handle_call_outcome(_payload("some_unknown_thing"))
        self.mock_add_note.assert_called_once()
        self.mock_move_opp.assert_not_called()
        self.assertTrue(result["success"])

    # ── no opp found ─────────────────────────────────────────────────────────

    def test_no_opp_does_not_crash(self):
        self.mock_find_opp.return_value = None
        result = surplus_tracks.handle_call_outcome(_payload("dnc"))
        self.assertTrue(result["success"])
        self.mock_move_opp.assert_not_called()
        self.assertIsNone(result["opp_id"])


if __name__ == "__main__":
    unittest.main()
