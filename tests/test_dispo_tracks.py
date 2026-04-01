"""Tests for dispo_tracks module."""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import dispo_tracks


class TestClassifyReply(unittest.TestCase):
    def test_yes_is_positive(self):
        self.assertEqual(dispo_tracks.classify_reply("YES"), "positive")

    def test_yes_lowercase(self):
        self.assertEqual(dispo_tracks.classify_reply("yes"), "positive")

    def test_interested_is_positive(self):
        self.assertEqual(dispo_tracks.classify_reply("I'm interested"), "positive")

    def test_sure_is_positive(self):
        self.assertEqual(dispo_tracks.classify_reply("sure"), "positive")

    def test_tell_me_more_is_positive(self):
        self.assertEqual(dispo_tracks.classify_reply("tell me more"), "positive")

    def test_send_it_is_positive(self):
        self.assertEqual(dispo_tracks.classify_reply("send it"), "positive")

    def test_call_me_is_positive(self):
        self.assertEqual(dispo_tracks.classify_reply("call me"), "positive")

    def test_details_is_positive(self):
        self.assertEqual(dispo_tracks.classify_reply("I want details"), "positive")

    def test_no_is_negative(self):
        self.assertEqual(dispo_tracks.classify_reply("no"), "negative")

    def test_not_interested_is_negative(self):
        self.assertEqual(dispo_tracks.classify_reply("not interested"), "negative")

    def test_pass_is_negative(self):
        self.assertEqual(dispo_tracks.classify_reply("pass"), "negative")

    def test_remove_is_negative(self):
        self.assertEqual(dispo_tracks.classify_reply("remove me"), "negative")

    def test_stop_is_negative(self):
        self.assertEqual(dispo_tracks.classify_reply("STOP"), "negative")

    def test_gibberish_is_unclear(self):
        self.assertEqual(dispo_tracks.classify_reply("what is this?"), "unclear")

    def test_empty_is_unclear(self):
        self.assertEqual(dispo_tracks.classify_reply(""), "unclear")

    def test_negative_takes_priority_over_positive(self):
        self.assertEqual(dispo_tracks.classify_reply("no not interested"), "negative")


class TestFormatPrice(unittest.TestCase):
    def test_millions(self):
        self.assertEqual(dispo_tracks._fmt_price(2_500_000), "$2.5M")

    def test_thousands(self):
        self.assertEqual(dispo_tracks._fmt_price(850_000), "$850K")

    def test_small(self):
        self.assertEqual(dispo_tracks._fmt_price(500), "$500")


class TestMatchBuyers(unittest.TestCase):
    def _make_buyer(self, **overrides):
        base = {
            "id": "buyer-uuid-1",
            "first_name": "Alice",
            "last_name": "Smith",
            "phone": "+15550001111",
            "email": "alice@example.com",
            "status": "active",
            "price_range_min": 500_000,
            "price_range_max": 3_000_000,
            "preferred_states": ["CA"],
            "buy_criteria": {"property_type": "multifamily|retail"},
            "notes": "",
        }
        base.update(overrides)
        return base

    @patch("dispo_tracks._get_sb")
    def test_returns_matching_buyers(self, mock_get_sb):
        alice = self._make_buyer()
        mock_sb = MagicMock()
        mock_get_sb.return_value = mock_sb
        (mock_sb.table.return_value
            .select.return_value
            .eq.return_value
            .lte.return_value
            .gte.return_value
            .contains.return_value
            .execute.return_value
            .data) = [alice]

        deal = {
            "asking_price": 1_200_000,
            "state": "CA",
            "property_type": "multifamily",
        }
        result = dispo_tracks.match_buyers(deal)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["first_name"], "Alice")

    @patch("dispo_tracks._get_sb")
    def test_filters_by_property_type(self, mock_get_sb):
        alice = self._make_buyer()
        mock_sb = MagicMock()
        mock_get_sb.return_value = mock_sb
        (mock_sb.table.return_value
            .select.return_value
            .eq.return_value
            .lte.return_value
            .gte.return_value
            .contains.return_value
            .execute.return_value
            .data) = [alice]

        deal = {
            "asking_price": 1_200_000,
            "state": "CA",
            "property_type": "industrial",
        }
        result = dispo_tracks.match_buyers(deal)
        self.assertEqual(result, [])

    @patch("dispo_tracks._get_sb")
    def test_empty_property_type_skips_filter(self, mock_get_sb):
        alice = self._make_buyer()
        mock_sb = MagicMock()
        mock_get_sb.return_value = mock_sb
        (mock_sb.table.return_value
            .select.return_value
            .eq.return_value
            .lte.return_value
            .gte.return_value
            .contains.return_value
            .execute.return_value
            .data) = [alice]

        deal = {"asking_price": 1_200_000, "state": "CA", "property_type": ""}
        result = dispo_tracks.match_buyers(deal)
        self.assertEqual(len(result), 1)


class TestBlastBuyers(unittest.TestCase):
    def _make_buyer(self, buyer_id="buyer-uuid-1"):
        return {
            "id": buyer_id,
            "first_name": "Bob",
            "last_name": "Jones",
            "phone": "+15550002222",
            "email": "bob@example.com",
            "buy_criteria": {"property_type": "multifamily"},
        }

    def _make_deal(self):
        return {
            "address": "123 Main St",
            "city": "Fresno",
            "state": "CA",
            "asking_price": 1_500_000,
            "asking_price_formatted": "$1.5M",
            "property_type": "multifamily",
            "cap_rate": "6.5%",
            "noi": "$97,500",
            "unit_count": "12",
        }

    @patch("dispo_tracks.create_dispo_opp", return_value="opp-abc")
    @patch("dispo_tracks._send_sms", return_value=True)
    @patch("dispo_tracks.find_or_create_ghl_contact", return_value="contact-123")
    @patch("dispo_tracks._get_sb")
    def test_blasts_new_buyer(self, mock_sb, mock_contact, mock_sms, mock_opp):
        """New buyer (no existing dispo_blasts row) should be blasted."""
        mock_supabase = MagicMock()
        mock_sb.return_value = mock_supabase
        # No existing blast row
        (mock_supabase.table.return_value
            .select.return_value.eq.return_value.eq.return_value
            .execute.return_value.data) = []
        # insert succeeds
        mock_supabase.table.return_value.insert.return_value.execute.return_value = MagicMock()

        count = dispo_tracks.blast_buyers("deal-1", self._make_deal(), [self._make_buyer()])

        self.assertEqual(count, 1)
        mock_sms.assert_called_once()
        mock_opp.assert_called_once()

    @patch("dispo_tracks._get_sb")
    def test_skips_already_blasted_buyer(self, mock_sb):
        """Buyer already in dispo_blasts for this deal should be skipped."""
        mock_supabase = MagicMock()
        mock_sb.return_value = mock_supabase
        # Existing blast row present
        (mock_supabase.table.return_value
            .select.return_value.eq.return_value.eq.return_value
            .execute.return_value.data) = [{"id": "existing"}]

        count = dispo_tracks.blast_buyers("deal-1", self._make_deal(), [self._make_buyer()])
        self.assertEqual(count, 0)

    @patch("dispo_tracks.blast_buyers", return_value=2)
    @patch("dispo_tracks.match_buyers", return_value=[{"id": "b1"}, {"id": "b2"}])
    @patch("dispo_tracks._add_note")
    def test_match_and_blast_returns_summary(self, mock_note, mock_match, mock_blast):
        result = dispo_tracks.match_and_blast("deal-1", {"asking_price": 1_000_000}, "contact-x")
        self.assertEqual(result["matched"], 2)
        self.assertEqual(result["blasted"], 2)

    @patch("dispo_tracks.match_buyers", return_value=[])
    @patch("dispo_tracks._add_note")
    def test_match_and_blast_no_buyers_adds_note(self, mock_note, mock_match):
        result = dispo_tracks.match_and_blast("deal-1", {}, "contact-x")
        self.assertEqual(result["matched"], 0)
        mock_note.assert_called_once()


if __name__ == "__main__":
    unittest.main()
