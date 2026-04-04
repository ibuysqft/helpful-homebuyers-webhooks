"""Tests for mls_tracks.dispatch_track."""
from unittest.mock import MagicMock, patch

import mls_tracks


def _running_scheduler():
    m = MagicMock()
    m.running = True
    return m


def test_dispatch_track_unknown_outcome_returns_none(monkeypatch):
    monkeypatch.setattr(mls_tracks, "_scheduler", _running_scheduler())
    result = mls_tracks.dispatch_track("c123", "unknown_outcome", "Bob", "123 Main", "+15550001111")
    assert result is None


def test_dispatch_track_interested_write_offer_returns_A(monkeypatch):
    monkeypatch.setattr(mls_tracks, "_scheduler", _running_scheduler())
    with patch.object(mls_tracks, "track_a_interested_write_offer") as mock_a:
        result = mls_tracks.dispatch_track(
            "c123", "interested_write_offer", "Bob", "123 Main", "+15550001111"
        )
    assert result == "A"
    mock_a.assert_called_once_with("c123", "Bob", "123 Main", "+15550001111", None)


def test_dispatch_track_in_escrow_backup_returns_B(monkeypatch):
    monkeypatch.setattr(mls_tracks, "_scheduler", _running_scheduler())
    with patch.object(mls_tracks, "track_b_in_escrow_backup") as mock_b:
        result = mls_tracks.dispatch_track(
            "c123", "in_escrow_backup", "Bob", "123 Main", "+15550001111"
        )
    assert result == "B"
    mock_b.assert_called_once()


def test_dispatch_track_not_interested_returns_C(monkeypatch):
    monkeypatch.setattr(mls_tracks, "_scheduler", _running_scheduler())
    with patch.object(mls_tracks, "track_c_not_interested") as mock_c:
        result = mls_tracks.dispatch_track(
            "c123", "not_interested", "Bob", "123 Main", "+15550001111"
        )
    assert result == "C"
    mock_c.assert_called_once()


def test_dispatch_track_starts_scheduler_if_stopped(monkeypatch):
    stopped = MagicMock()
    stopped.running = False
    monkeypatch.setattr(mls_tracks, "_scheduler", stopped)
    with patch.object(mls_tracks, "start_scheduler") as mock_start, \
         patch.object(mls_tracks, "track_a_interested_write_offer"):
        mls_tracks.dispatch_track("c123", "interested_write_offer", "Bob", "123 Main", "+15550001111")
    mock_start.assert_called_once()
