"""HTTP client for the Open-Meteo forecast API.

Single responsibility: perform a single GET request against Open-Meteo and
return the decoded JSON payload (``dict``) — or ``None`` on any error.
No caching, no parsing, no business logic.

Two transport paths, in order of preference:
    1. ``requests`` (if installed) — standard, pools connections.
       Retries once with 1s backoff on transient errors (timeout, connection
       reset, HTTP 429/5xx). Worst-case wall clock: ~21s
       (timeout + sleep + timeout), well under the 60s scheduler tick.
    2. ``urllib.request`` fallback — keeps the service usable on minimal
       Wirenboard deployments without the ``requests`` package. No retry
       (last-resort path; cache fallback handles failure).
"""

import json
import logging
import time
from typing import Any

from services.weather.models import (
    _OPEN_METEO_URL,
    _REQUEST_TIMEOUT,
)

logger = logging.getLogger(__name__)

_RETRY_BACKOFF_SEC = 1.0
_RETRY_MAX_ATTEMPTS = 2  # total attempts (1 retry)
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def fetch_api(lat: float, lon: float) -> dict[str, Any] | None:
    """Fetch raw weather data from Open-Meteo for the given coordinates.

    Args:
        lat: Latitude (decimal degrees).
        lon: Longitude (decimal degrees).

    Returns:
        Raw JSON payload as a dict, or ``None`` on any network / decode error.
        On ``None`` the caller is expected to fall back to cached data.
    """
    hourly_params = ",".join(
        [
            "temperature_2m",
            "relative_humidity_2m",
            "precipitation",
            "wind_speed_10m",
            "et0_fao_evapotranspiration",
            "weather_code",
        ]
    )
    daily_params = ",".join(
        [
            "precipitation_sum",
            "et0_fao_evapotranspiration",
            "temperature_2m_max",
            "temperature_2m_min",
            "weather_code",
            "sunrise",
            "sunset",
        ]
    )

    try:
        import requests
    except ImportError:
        try:
            import urllib.parse
            import urllib.request

            params = urllib.parse.urlencode(
                {
                    "latitude": lat,
                    "longitude": lon,
                    "hourly": hourly_params,
                    "daily": daily_params,
                    "timezone": "auto",
                    "forecast_days": 3,
                    "wind_speed_unit": "ms",
                }
            )
            url = f"{_OPEN_METEO_URL}?{params}"
            req = urllib.request.Request(url, headers={"User-Agent": "WB-Irrigation/2.0"})
            with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            logger.warning("Weather API fetch (urllib) failed: %s", e)
            return None

    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": hourly_params,
        "daily": daily_params,
        "timezone": "auto",
        "forecast_days": 3,
        "wind_speed_unit": "ms",
    }
    headers = {"User-Agent": "WB-Irrigation/2.0"}

    for attempt in range(1, _RETRY_MAX_ATTEMPTS + 1):
        try:
            resp = requests.get(
                _OPEN_METEO_URL,
                params=params,
                timeout=_REQUEST_TIMEOUT,
                headers=headers,
            )
            resp.raise_for_status()
            return resp.json()
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            logger.warning(
                "Weather API fetch attempt %d/%d failed (transient): %s",
                attempt,
                _RETRY_MAX_ATTEMPTS,
                e,
            )
            if attempt >= _RETRY_MAX_ATTEMPTS:
                return None
            time.sleep(_RETRY_BACKOFF_SEC)
        except requests.exceptions.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            if status in _RETRYABLE_STATUS:
                logger.warning(
                    "Weather API fetch attempt %d/%d failed (HTTP %s): %s",
                    attempt,
                    _RETRY_MAX_ATTEMPTS,
                    status,
                    e,
                )
                if attempt >= _RETRY_MAX_ATTEMPTS:
                    return None
                time.sleep(_RETRY_BACKOFF_SEC)
            else:
                logger.warning("Weather API fetch failed (HTTP %s): %s", status, e)
                return None
        except Exception as e:
            logger.warning("Weather API fetch failed: %s", e)
            return None
    return None


def fetch_history(lat: float, lon: float, past_days: int) -> dict[str, Any] | None:
    """Fetch *past* daily ET₀ / precipitation for the water-balance engine.

    Deliberately isolated from ``fetch_api`` and the whole H1 cache/parse path
    (review BLOCKER 1): the ``weather_cache`` is keyed only on (lat, lon), so if a
    ``past_days`` response were ever cached or parsed via ``WeatherData._parse``,
    the H1 forecast path — which reads ``daily[..., 0]`` as *today* — would
    silently break. This function therefore never touches the cache and returns
    the raw JSON for ``balance.py`` to read directly (a single daily request per
    night needs no caching).

    Only the ``daily`` block is requested (``et0_fao_evapotranspiration``,
    ``precipitation_sum``, ``time``). ``forecast_days=1`` keeps today present so
    the caller can identify and drop the current partial day.

    Args:
        lat: Latitude (decimal degrees).
        lon: Longitude (decimal degrees).
        past_days: Number of completed past days to include (Open-Meteo caps
            this at 92; the caller passes ~35).

    Returns:
        Raw JSON payload as a dict, or ``None`` on any network / decode error.
    """
    daily_params = ",".join(["et0_fao_evapotranspiration", "precipitation_sum"])
    query = {
        "latitude": lat,
        "longitude": lon,
        "daily": daily_params,
        "timezone": "auto",
        "past_days": int(past_days),
        "forecast_days": 1,
    }

    try:
        import requests
    except ImportError:
        try:
            import urllib.parse
            import urllib.request

            params = urllib.parse.urlencode(query)
            url = f"{_OPEN_METEO_URL}?{params}"
            req = urllib.request.Request(url, headers={"User-Agent": "WB-Irrigation/2.0"})
            with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            logger.warning("Weather history fetch (urllib) failed: %s", e)
            return None

    headers = {"User-Agent": "WB-Irrigation/2.0"}
    for attempt in range(1, _RETRY_MAX_ATTEMPTS + 1):
        try:
            resp = requests.get(_OPEN_METEO_URL, params=query, timeout=_REQUEST_TIMEOUT, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            logger.warning("Weather history fetch attempt %d/%d failed (transient): %s", attempt, _RETRY_MAX_ATTEMPTS, e)
            if attempt >= _RETRY_MAX_ATTEMPTS:
                return None
            time.sleep(_RETRY_BACKOFF_SEC)
        except requests.exceptions.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            if status in _RETRYABLE_STATUS and attempt < _RETRY_MAX_ATTEMPTS:
                logger.warning("Weather history fetch attempt %d/%d failed (HTTP %s)", attempt, _RETRY_MAX_ATTEMPTS, status)
                time.sleep(_RETRY_BACKOFF_SEC)
            else:
                logger.warning("Weather history fetch failed (HTTP %s): %s", status, e)
                return None
        except Exception as e:
            logger.warning("Weather history fetch failed: %s", e)
            return None
    return None
