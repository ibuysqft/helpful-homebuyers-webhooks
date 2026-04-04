"""Tests for jenni_tracks.trigger_jenni_call."""
from unittest.mock import MagicMock, patch

import jenni_tracks


def test_trigger_returns_false_when_retell_phone_missing(monkeypatch):
    monkeypatch.setattr(jenni_tracks, "JENNI_RETELL_PHONE", "")
    monkeypatch.setattr(jenni_tracks, "_add_note", lambda *a, **kw: None)
    result = jenni_tracks.trigger_jenni_call("c123", "Bob Smith", "123 Main St", "+15550001111")
    assert result is False


def test_trigger_returns_false_when_no_to_number(monkeypatch):
    monkeypatch.setattr(jenni_tracks, "JENNI_RETELL_PHONE", "+14158317712")
    result = jenni_tracks.trigger_jenni_call("c123", "Bob Smith", "123 Main St", "")
    assert result is False


def test_trigger_posts_to_retell_and_returns_true(monkeypatch):
    monkeypatch.setattr(jenni_tracks, "JENNI_RETELL_PHONE", "+14158317712")
    monkeypatch.setattr(jenni_tracks, "JENNI_AGENT_ID", "agent_abc123")

    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = {"call_id": "call_xyz"}

    with patch("jenni_tracks.requests.post", return_value=mock_resp) as mock_post:
        result = jenni_tracks.trigger_jenni_call(
            "c123", "Bob Smith", "123 Main St", "+15550001111",
            asking_price="$1.2M", property_type="multifamily",
        )

    assert result is True
    _, kwargs = mock_post.call_args
    payload = kwargs["json"]
    assert payload["agent_id"] == "agent_abc123"
    assert payload["from_number"] == "+14158317712"
    assert payload["to_number"] == "+15550001111"


def test_trigger_returns_false_on_retell_error(monkeypatch):
    monkeypatch.setattr(jenni_tracks, "JENNI_RETELL_PHONE", "+14158317712")
    monkeypatch.setattr(jenni_tracks, "JENNI_AGENT_ID", "agent_abc123")

    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.text = "bad request"

    with patch("jenni_tracks.requests.post", return_value=mock_resp):
        result = jenni_tracks.trigger_jenni_call("c123", "Bob Smith", "123 Main St", "+15550001111")

    assert result is False
