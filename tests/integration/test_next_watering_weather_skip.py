"""Issue #34: /api/zones/<id>/next-watering must skip today when weather
adjustment is in skip state. Cards previously displayed today's slot even when
the scheduler was going to skip it — user saw 'next watering 21:00' on a day
with a 'rain skip' decision."""

import json
import os
from datetime import datetime
from unittest.mock import patch

os.environ["TESTING"] = "1"

_NOW = datetime(2026, 7, 18, 12, 0)


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return _NOW
        return _NOW.astimezone(tz)


def _make_program_for_today_at(hour: int, minute: int, zone_id: int, app):
    """Create a daily program that fires today at HH:MM with one zone."""
    return app.db.create_program(
        {
            "name": "WeatherSkipTest",
            "time": f"{hour:02d}:{minute:02d}",
            "days": [0, 1, 2, 3, 4, 5, 6],
            "zones": [zone_id],
        }
    )


def test_next_watering_pushes_past_today_when_weather_skip(admin_client, app):
    zone = app.db.create_zone({"name": "WS-1", "duration": 5, "group_id": 1})
    _make_program_for_today_at(23, 55, zone["id"], app)

    with (
        patch("services.next_watering.datetime", _FrozenDateTime),
        patch("services.next_watering.weather_skip_today", return_value=True),
    ):
        resp = admin_client.get(f"/api/zones/{zone['id']}/next-watering")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data.get("next_datetime"), data
    next_dt = datetime.strptime(data["next_datetime"], "%Y-%m-%d %H:%M")
    assert next_dt.date() > _NOW.date(), f"weather_skip=True must push next-watering past today; got {next_dt}"


def test_next_watering_returns_today_when_no_weather_skip(admin_client, app):
    """Regression guard: without skip the slot stays on today."""
    zone = app.db.create_zone({"name": "WS-2", "duration": 5, "group_id": 1})
    _make_program_for_today_at(23, 55, zone["id"], app)

    with (
        patch("services.next_watering.datetime", _FrozenDateTime),
        patch("services.next_watering.weather_skip_today", return_value=False),
    ):
        resp = admin_client.get(f"/api/zones/{zone['id']}/next-watering")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data.get("next_datetime"), data
    next_dt = datetime.strptime(data["next_datetime"], "%Y-%m-%d %H:%M")
    assert next_dt == _NOW.replace(hour=23, minute=55), f"without skip the same-day slot must remain; got {next_dt}"


def test_next_watering_bulk_pushes_past_today_when_weather_skip(admin_client, app):
    zone = app.db.create_zone({"name": "WS-3", "duration": 5, "group_id": 1})
    _make_program_for_today_at(23, 55, zone["id"], app)

    with (
        patch("services.next_watering.datetime", _FrozenDateTime),
        patch("services.next_watering.weather_skip_today", return_value=True),
    ):
        resp = admin_client.post(
            "/api/zones/next-watering-bulk",
            data=json.dumps({"zone_ids": [zone["id"]]}),
            content_type="application/json",
        )
    assert resp.status_code == 200
    items = resp.get_json().get("items") or []
    assert items, resp.get_json()
    next_str = items[0].get("next_datetime")
    assert next_str, items[0]
    next_dt = datetime.strptime(next_str, "%Y-%m-%d %H:%M:%S")
    assert next_dt.date() > _NOW.date(), f"bulk weather_skip=True must push past today; got {next_dt}"
