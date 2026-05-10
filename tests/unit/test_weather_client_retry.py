"""Tests for the Open-Meteo client retry behaviour.

Phase 3 (issue #29): retry once with 1s backoff on transient errors
(timeout, ConnectionError, HTTP 429/5xx). Other errors must not retry.

We mock ``requests.get`` directly via ``unittest.mock`` rather than
``respx`` (respx targets httpx, not requests). ``time.sleep`` is patched
so the test suite stays fast.
"""
import logging
from unittest.mock import MagicMock, patch

import pytest
import requests

from services.weather import client as wc


def _ok_response(payload=None):
    resp = MagicMock(spec=requests.Response)
    resp.status_code = 200
    resp.raise_for_status = MagicMock(return_value=None)
    resp.json = MagicMock(return_value=payload if payload is not None else {'ok': True})
    return resp


def _http_error_response(status):
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status
    err = requests.exceptions.HTTPError(f'{status} error')
    err.response = resp
    resp.raise_for_status = MagicMock(side_effect=err)
    return resp


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch.object(wc.time, 'sleep') as m:
        yield m


def test_retries_once_on_timeout():
    with patch('requests.get') as get:
        get.side_effect = [
            requests.exceptions.Timeout('boom'),
            _ok_response({'data': 'fresh'}),
        ]
        result = wc.fetch_api(55.7, 37.6)
    assert result == {'data': 'fresh'}
    assert get.call_count == 2


def test_retries_once_on_connection_error():
    with patch('requests.get') as get:
        get.side_effect = [
            requests.exceptions.ConnectionError('reset'),
            _ok_response({'data': 'fresh'}),
        ]
        result = wc.fetch_api(55.7, 37.6)
    assert result == {'data': 'fresh'}
    assert get.call_count == 2


def test_retry_on_429():
    with patch('requests.get') as get:
        get.side_effect = [
            _http_error_response(429),
            _ok_response({'data': 'fresh'}),
        ]
        result = wc.fetch_api(55.7, 37.6)
    assert result == {'data': 'fresh'}
    assert get.call_count == 2


def test_retry_on_503():
    with patch('requests.get') as get:
        get.side_effect = [
            _http_error_response(503),
            _ok_response({'data': 'fresh'}),
        ]
        result = wc.fetch_api(55.7, 37.6)
    assert result == {'data': 'fresh'}
    assert get.call_count == 2


def test_no_retry_on_404():
    with patch('requests.get') as get:
        get.side_effect = [_http_error_response(404)]
        result = wc.fetch_api(55.7, 37.6)
    assert result is None
    assert get.call_count == 1


def test_returns_none_after_max_attempts(caplog):
    caplog.set_level(logging.WARNING, logger=wc.logger.name)
    with patch('requests.get') as get:
        get.side_effect = [_http_error_response(503), _http_error_response(503)]
        result = wc.fetch_api(55.7, 37.6)
    assert result is None
    assert get.call_count == 2
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) >= 2


def test_no_retry_on_value_error():
    """JSON decode failure should not be retried."""
    bad_resp = MagicMock(spec=requests.Response)
    bad_resp.status_code = 200
    bad_resp.raise_for_status = MagicMock(return_value=None)
    bad_resp.json = MagicMock(side_effect=ValueError('bad json'))
    with patch('requests.get') as get:
        get.return_value = bad_resp
        result = wc.fetch_api(55.7, 37.6)
    assert result is None
    assert get.call_count == 1
