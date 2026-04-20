"""HTTP client for the Open-Meteo forecast API.

Single responsibility: perform a single GET request against Open-Meteo and
return the decoded JSON payload (``dict``) — or ``None`` on any error.
No caching, no parsing, no business logic.

Two transport paths, in order of preference:
    1. ``requests`` (if installed) — standard, pools connections.
    2. ``urllib.request`` fallback — keeps the service usable on minimal
       Wirenboard deployments without the ``requests`` package.
"""
import json
import logging
from typing import Any, Dict, Optional

from services.weather.models import (
    _OPEN_METEO_URL,
    _REQUEST_TIMEOUT,
)

logger = logging.getLogger(__name__)


def fetch_api(lat: float, lon: float) -> Optional[Dict[str, Any]]:
    """Fetch raw weather data from Open-Meteo for the given coordinates.

    Args:
        lat: Latitude (decimal degrees).
        lon: Longitude (decimal degrees).

    Returns:
        Raw JSON payload as a dict, or ``None`` on any network / decode error.
        On ``None`` the caller is expected to fall back to cached data.
    """
    hourly_params = ','.join([
        'temperature_2m',
        'relative_humidity_2m',
        'precipitation',
        'wind_speed_10m',
        'et0_fao_evapotranspiration',
        'weather_code',
    ])
    daily_params = ','.join([
        'precipitation_sum',
        'et0_fao_evapotranspiration',
        'temperature_2m_max',
        'temperature_2m_min',
        'weather_code',
        'sunrise',
        'sunset',
    ])

    try:
        import requests
    except ImportError:
        try:
            import urllib.request
            import urllib.parse
            params = urllib.parse.urlencode({
                'latitude': lat,
                'longitude': lon,
                'hourly': hourly_params,
                'daily': daily_params,
                'timezone': 'auto',
                'forecast_days': 3,
                'wind_speed_unit': 'ms',
            })
            url = '%s?%s' % (_OPEN_METEO_URL, params)
            req = urllib.request.Request(url, headers={'User-Agent': 'WB-Irrigation/2.0'})
            with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except Exception as e:
            logger.warning("Weather API fetch (urllib) failed: %s", e)
            return None

    try:
        resp = requests.get(
            _OPEN_METEO_URL,
            params={
                'latitude': lat,
                'longitude': lon,
                'hourly': hourly_params,
                'daily': daily_params,
                'timezone': 'auto',
                'forecast_days': 3,
                'wind_speed_unit': 'ms',
            },
            timeout=_REQUEST_TIMEOUT,
            headers={'User-Agent': 'WB-Irrigation/2.0'},
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning("Weather API fetch failed: %s", e)
        return None
