"""Tests for cr_api.py — Clash Royale API client."""

from unittest.mock import MagicMock, patch

import pytest
import requests

import cr_api


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(json_data, status_code=200):
    """Create a mock requests.Response that behaves like a successful response."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status.return_value = None
    return resp


def _mock_response_http_error(status_code=404):
    """Create a mock response that raises HTTPError on raise_for_status."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.raise_for_status.side_effect = requests.HTTPError(
        response=resp,
    )
    return resp


# ---------------------------------------------------------------------------
# _request_json tests
# ---------------------------------------------------------------------------

@patch("cr_api.runtime_status.record_api_call")
@patch("cr_api.requests.get")
def test_request_json_success(mock_get, mock_record):
    """Successful request returns parsed JSON and records the call."""
    payload = {"name": "POAP KINGS", "tag": "#ABC123"}
    mock_get.return_value = _mock_response(payload)

    result = cr_api._request_json("/clans/%23ABC", endpoint_name="clan", entity_key="ABC")

    assert result == payload
    mock_get.assert_called_once()
    mock_record.assert_called_once()
    _, kwargs = mock_record.call_args
    assert kwargs["ok"] is True
    assert kwargs["status_code"] == 200


@patch("cr_api.time.sleep")
@patch("cr_api.runtime_status.record_api_call")
@patch("cr_api.requests.get")
def test_request_json_retries_on_connection_error(mock_get, mock_record, mock_sleep):
    """ConnectionError triggers retries up to _MAX_RETRIES, then re-raises."""
    mock_get.side_effect = requests.ConnectionError("connection refused")

    with pytest.raises(requests.ConnectionError):
        cr_api._request_json("/test", endpoint_name="test")

    # Initial attempt + 2 retries = 3 total calls
    assert mock_get.call_count == cr_api._MAX_RETRIES + 1
    # sleep is called before each retry (not before the first attempt)
    assert mock_sleep.call_count == cr_api._MAX_RETRIES


@patch("cr_api.time.sleep")
@patch("cr_api.runtime_status.record_api_call")
@patch("cr_api.requests.get")
def test_request_json_retries_on_timeout(mock_get, mock_record, mock_sleep):
    """Timeout triggers retries up to _MAX_RETRIES, then re-raises."""
    mock_get.side_effect = requests.Timeout("read timed out")

    with pytest.raises(requests.Timeout):
        cr_api._request_json("/test", endpoint_name="test")

    assert mock_get.call_count == cr_api._MAX_RETRIES + 1
    assert mock_sleep.call_count == cr_api._MAX_RETRIES


@patch("cr_api.time.sleep")
@patch("cr_api.runtime_status.record_api_call")
@patch("cr_api.requests.get")
def test_request_json_retry_then_succeed(mock_get, mock_record, mock_sleep):
    """Request succeeds on retry after initial ConnectionError."""
    success_resp = _mock_response({"ok": True})
    mock_get.side_effect = [requests.ConnectionError("blip"), success_resp]

    result = cr_api._request_json("/test", endpoint_name="test")

    assert result == {"ok": True}
    assert mock_get.call_count == 2
    assert mock_sleep.call_count == 1


@patch("cr_api.runtime_status.record_api_call")
@patch("cr_api.requests.get")
def test_request_json_raises_on_http_error(mock_get, mock_record):
    """HTTPError is raised immediately with no retry."""
    mock_get.return_value = _mock_response_http_error(404)

    with pytest.raises(requests.HTTPError):
        cr_api._request_json("/test", endpoint_name="test")

    # Only one attempt — no retry on HTTP errors
    mock_get.assert_called_once()
    mock_record.assert_called_once()
    _, kwargs = mock_record.call_args
    assert kwargs["ok"] is False
    assert kwargs["status_code"] == 404


# ---------------------------------------------------------------------------
# get_player tests
# ---------------------------------------------------------------------------

@patch("cr_api.runtime_status.record_api_call")
@patch("cr_api.requests.get")
def test_get_player_strips_hash(mock_get, mock_record):
    """get_player strips leading '#' from the tag."""
    mock_get.return_value = _mock_response({"name": "Jamie", "tag": "#ABC123"})

    result = cr_api.get_player("#ABC123")

    assert result == {"name": "Jamie", "tag": "#ABC123"}
    call_url = mock_get.call_args[0][0]
    assert "%23ABC123" in call_url
    assert "#" not in call_url


@patch("cr_api.runtime_status.record_api_call")
@patch("cr_api.requests.get")
def test_get_player_no_hash(mock_get, mock_record):
    """get_player works when tag has no '#' prefix."""
    mock_get.return_value = _mock_response({"name": "Jamie"})

    result = cr_api.get_player("ABC123")

    assert result is not None
    call_url = mock_get.call_args[0][0]
    assert "%23ABC123" in call_url


@patch("cr_api.time.sleep")
@patch("cr_api.runtime_status.record_api_call")
@patch("cr_api.requests.get")
def test_get_player_returns_none_on_error(mock_get, mock_record, mock_sleep):
    """get_player returns None when the API raises RequestException."""
    mock_get.side_effect = requests.ConnectionError("down")

    result = cr_api.get_player("#XYZ")

    assert result is None


# ---------------------------------------------------------------------------
# get_player_chests tests
# ---------------------------------------------------------------------------

@patch("cr_api.runtime_status.record_api_call")
@patch("cr_api.requests.get")
def test_get_player_chests_extracts_items(mock_get, mock_record):
    """get_player_chests returns the 'items' list from the response."""
    chests = [{"index": 0, "name": "Silver Chest"}, {"index": 1, "name": "Gold Chest"}]
    mock_get.return_value = _mock_response({"items": chests})

    result = cr_api.get_player_chests("#ABC")

    assert result == chests


@patch("cr_api.runtime_status.record_api_call")
@patch("cr_api.requests.get")
def test_get_player_chests_empty_items(mock_get, mock_record):
    """get_player_chests returns empty list when 'items' key is missing."""
    mock_get.return_value = _mock_response({})

    result = cr_api.get_player_chests("#ABC")

    assert result == []


@patch("cr_api.time.sleep")
@patch("cr_api.runtime_status.record_api_call")
@patch("cr_api.requests.get")
def test_get_player_chests_returns_none_on_error(mock_get, mock_record, mock_sleep):
    """get_player_chests returns None on RequestException."""
    mock_get.side_effect = requests.ConnectionError("down")

    result = cr_api.get_player_chests("#ABC")

    assert result is None


# ---------------------------------------------------------------------------
# get_current_war tests
# ---------------------------------------------------------------------------

@patch("cr_api.time.sleep")
@patch("cr_api.runtime_status.record_api_call")
@patch("cr_api.requests.get")
def test_get_current_war_returns_none_on_error(mock_get, mock_record, mock_sleep):
    """get_current_war returns None when the API is unreachable."""
    mock_get.side_effect = requests.ConnectionError("timeout")

    result = cr_api.get_current_war()

    assert result is None


@patch("cr_api.runtime_status.record_api_call")
@patch("cr_api.requests.get")
def test_get_current_war_success(mock_get, mock_record):
    """get_current_war returns war data on success."""
    war_data = {"state": "warDay", "clan": {"tag": "#ABC"}}
    mock_get.return_value = _mock_response(war_data)

    result = cr_api.get_current_war()

    assert result == war_data


# ---------------------------------------------------------------------------
# get_cards tests
# ---------------------------------------------------------------------------

@patch("cr_api.runtime_status.record_api_call")
@patch("cr_api.requests.get")
def test_get_cards_success(mock_get, mock_record):
    """get_cards returns the full card catalog."""
    cards_data = {
        "items": [{"name": "Knight", "id": 26000000}],
        "supportItems": [{"name": "Archer Queen", "id": 29000000}],
    }
    mock_get.return_value = _mock_response(cards_data)

    result = cr_api.get_cards()

    assert result == cards_data
    call_url = mock_get.call_args[0][0]
    assert call_url.endswith("/cards")


@patch("cr_api.time.sleep")
@patch("cr_api.runtime_status.record_api_call")
@patch("cr_api.requests.get")
def test_get_cards_returns_none_on_error(mock_get, mock_record, mock_sleep):
    """get_cards returns None on RequestException."""
    mock_get.side_effect = requests.Timeout("slow")

    result = cr_api.get_cards()

    assert result is None
