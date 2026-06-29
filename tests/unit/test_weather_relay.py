"""Tests for the GitHub relay weather channel and source-mode routing.

Covers:
- ``client.fetch_relay`` — auth/raw headers, retry semantics (mirrors fetch_api),
  error handling. ``requests.get`` is mocked directly (same approach as
  ``test_weather_client_retry``); ``time.sleep`` is patched for speed.
- ``WeatherService._fetch_api`` routing on the live ``weather.source_mode``
  setting, including the relay-without-env fallback.
- ``WeatherService._get_source_mode`` default/validation.
"""

import logging
from unittest.mock import MagicMock, patch

import pytest
import requests

from services.weather import client as wc
from services.weather.service import WeatherService


def _ok_response(payload=None):
    resp = MagicMock(spec=requests.Response)
    resp.status_code = 200
    resp.raise_for_status = MagicMock(return_value=None)
    resp.json = MagicMock(return_value=payload if payload is not None else {"ok": True})
    return resp


def _http_error_response(status):
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status
    err = requests.exceptions.HTTPError(f"{status} error")
    err.response = resp
    resp.raise_for_status = MagicMock(side_effect=err)
    return resp


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch.object(wc.time, "sleep") as m:
        yield m


# --------------------------------------------------------------------------
# fetch_relay — transport
# --------------------------------------------------------------------------


def test_fetch_relay_sends_bearer_and_raw_accept():
    with patch("requests.get") as get:
        get.return_value = _ok_response({"hourly": {"temperature_2m": [1]}})
        result = wc.fetch_relay("https://api.github.com/repos/o/r/contents/gub.json", "mytoken")
    assert result == {"hourly": {"temperature_2m": [1]}}
    _, kwargs = get.call_args
    headers = kwargs["headers"]
    assert headers["Authorization"] == "Bearer mytoken"
    assert headers["Accept"] == "application/vnd.github.raw"


def test_fetch_relay_public_omits_auth_header():
    with patch("requests.get") as get:
        get.return_value = _ok_response({"hourly": {"temperature_2m": [2]}})
        result = wc.fetch_relay("https://raw.githubusercontent.com/o/r/main/gub.json")
    assert result == {"hourly": {"temperature_2m": [2]}}
    _, kwargs = get.call_args
    headers = kwargs["headers"]
    assert "Authorization" not in headers
    assert "Accept" not in headers


def test_fetch_relay_retries_once_on_timeout():
    with patch("requests.get") as get:
        get.side_effect = [requests.exceptions.Timeout("boom"), _ok_response({"data": "fresh"})]
        result = wc.fetch_relay("https://api.github.com/x", "t")
    assert result == {"data": "fresh"}
    assert get.call_count == 2


def test_fetch_relay_retry_on_503():
    with patch("requests.get") as get:
        get.side_effect = [_http_error_response(503), _ok_response({"data": "fresh"})]
        result = wc.fetch_relay("https://api.github.com/x", "t")
    assert result == {"data": "fresh"}
    assert get.call_count == 2


def test_fetch_relay_no_retry_on_404():
    with patch("requests.get") as get:
        get.side_effect = [_http_error_response(404)]
        result = wc.fetch_relay("https://api.github.com/x", "t")
    assert result is None
    assert get.call_count == 1


def test_fetch_relay_returns_none_after_max_attempts():
    with patch("requests.get") as get:
        get.side_effect = [_http_error_response(503), _http_error_response(503)]
        result = wc.fetch_relay("https://api.github.com/x", "t")
    assert result is None
    assert get.call_count == 2


# --------------------------------------------------------------------------
# _fetch_api routing
# --------------------------------------------------------------------------


def test_routing_relay_calls_fetch_relay(monkeypatch):
    monkeypatch.setattr("config.Config.OPEN_METEO_RELAY_URL", "https://api.github.com/repos/o/r/contents/gub.json")
    monkeypatch.setattr("config.Config.OPEN_METEO_RELAY_TOKEN", "tok")
    svc = WeatherService(":memory:")
    monkeypatch.setattr(svc, "_get_source_mode", lambda: "relay")
    with (
        patch("services.weather.service._fetch_relay_impl", return_value={"r": 1}) as fr,
        patch("services.weather.service._fetch_api_impl", return_value={"d": 1}) as fa,
    ):
        result = svc._fetch_api(51.27, 58.53)
    assert result == {"r": 1}
    fr.assert_called_once_with("https://api.github.com/repos/o/r/contents/gub.json", "tok")
    fa.assert_not_called()


def test_routing_relay_public_no_token_calls_fetch_relay(monkeypatch):
    monkeypatch.setattr("config.Config.OPEN_METEO_RELAY_URL", "https://raw.githubusercontent.com/o/r/main/gub.json")
    monkeypatch.setattr("config.Config.OPEN_METEO_RELAY_TOKEN", "")
    svc = WeatherService(":memory:")
    monkeypatch.setattr(svc, "_get_source_mode", lambda: "relay")
    with (
        patch("services.weather.service._fetch_relay_impl", return_value={"r": 1}) as fr,
        patch("services.weather.service._fetch_api_impl", return_value={"d": 1}) as fa,
    ):
        result = svc._fetch_api(51.27, 58.53)
    assert result == {"r": 1}
    fr.assert_called_once_with("https://raw.githubusercontent.com/o/r/main/gub.json", "")
    fa.assert_not_called()


def test_routing_direct_calls_fetch_api(monkeypatch):
    svc = WeatherService(":memory:")
    monkeypatch.setattr(svc, "_get_source_mode", lambda: "direct")
    with (
        patch("services.weather.service._fetch_relay_impl") as fr,
        patch("services.weather.service._fetch_api_impl", return_value={"d": 1}) as fa,
    ):
        result = svc._fetch_api(51.27, 58.53)
    assert result == {"d": 1}
    fr.assert_not_called()
    fa.assert_called_once_with(51.27, 58.53)


def test_routing_relay_without_env_falls_back_to_direct(monkeypatch, caplog):
    monkeypatch.setattr("config.Config.OPEN_METEO_RELAY_URL", "")
    monkeypatch.setattr("config.Config.OPEN_METEO_RELAY_TOKEN", "")
    svc = WeatherService(":memory:")
    monkeypatch.setattr(svc, "_get_source_mode", lambda: "relay")
    with (
        patch("services.weather.service._fetch_relay_impl") as fr,
        patch("services.weather.service._fetch_api_impl", return_value={"d": 1}) as fa,
        caplog.at_level(logging.ERROR),
    ):
        result = svc._fetch_api(51.27, 58.53)
    assert result == {"d": 1}
    fr.assert_not_called()
    fa.assert_called_once_with(51.27, 58.53)
    assert any("source_mode=relay" in r.message for r in caplog.records)


# --------------------------------------------------------------------------
# _get_source_mode
# --------------------------------------------------------------------------


def test_get_source_mode_default_direct_when_unset():
    svc = WeatherService(":memory:")
    with patch("db.settings.SettingsRepository") as sr:
        sr.return_value.get_setting_value.return_value = None
        assert svc._get_source_mode() == "direct"


def test_get_source_mode_reads_relay():
    svc = WeatherService(":memory:")
    with patch("db.settings.SettingsRepository") as sr:
        sr.return_value.get_setting_value.return_value = "relay"
        assert svc._get_source_mode() == "relay"


def test_get_source_mode_invalid_defaults_direct():
    svc = WeatherService(":memory:")
    with patch("db.settings.SettingsRepository") as sr:
        sr.return_value.get_setting_value.return_value = "garbage"
        assert svc._get_source_mode() == "direct"
