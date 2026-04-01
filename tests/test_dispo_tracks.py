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


if __name__ == "__main__":
    unittest.main()
