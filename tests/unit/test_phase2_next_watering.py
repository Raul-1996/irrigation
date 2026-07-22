"""Phase-2 scheduler-parity regressions for ``services.next_watering``."""

import json
import sqlite3
import time
from datetime import datetime
from unittest.mock import Mock

import pytest

import services.next_watering as nw


class _FrozenDateTime(datetime):
    current = datetime(2026, 7, 20, 12, 0, 0)

    @classmethod
    def now(cls, tz=None):
        value = cls.current
        if tz is not None:
            return value.astimezone(tz)
        return cls(
            value.year,
            value.month,
            value.day,
            value.hour,
            value.minute,
            value.second,
            value.microsecond,
        )


@pytest.fixture(autouse=True)
def _reset_clock():
    _FrozenDateTime.current = datetime(2026, 7, 20, 12, 0, 0)


def _zone(zone_id, *, duration=20, state="off", source=None, postpone_until=None):
    return {
        "id": zone_id,
        "group_id": 1,
        "duration": duration,
        "state": state,
        "watering_start_source": source,
        "postpone_until": postpone_until,
    }


def _program(program_id=1, **updates):
    program = {
        "id": program_id,
        "name": f"Program {program_id}",
        "time": "06:00",
        "extra_times": [],
        "days": [0, 1, 2, 3, 4, 5, 6],
        "zones": [1],
        "schedule_type": "weekdays",
        "enabled": True,
    }
    program.update(updates)
    return program


def _compute(monkeypatch, zones, programs, zone_ids=None, **kwargs):
    monkeypatch.setattr(nw, "datetime", _FrozenDateTime)
    return nw.compute_next_watering(
        zone_ids or [z["id"] for z in zones],
        all_zones=zones,
        programs=programs,
        skip_today=kwargs.pop("skip_today", False),
        **kwargs,
    )


def test_disabled_program_is_not_displayed(monkeypatch):
    result = _compute(monkeypatch, [_zone(1)], [_program(enabled=False)])

    assert result[1]["next_dt"] is None
    assert result[1]["has_programs"] is False


def test_extra_time_can_be_the_next_real_start(monkeypatch):
    result = _compute(monkeypatch, [_zone(1)], [_program(time="06:00", extra_times=["18:00"])])

    assert result[1]["next_dt"] == datetime(2026, 7, 20, 18, 0)


def test_interval_uses_live_scheduler_next_run_metadata(monkeypatch):
    _FrozenDateTime.current = datetime(2026, 7, 21, 10, 0)
    monkeypatch.setattr(
        nw,
        "_get_interval_next_runs",
        lambda _programs: {(1, "main"): datetime(2026, 7, 23, 6, 0)},
        raising=False,
    )
    program = _program(schedule_type="interval", interval_days=3, days=[])

    result = _compute(monkeypatch, [_zone(1)], [program])

    assert result[1]["next_dt"] == datetime(2026, 7, 23, 6, 0)


def test_cancelled_run_stays_cancelled_when_postpone_expires_before_zone_slot(monkeypatch):
    _FrozenDateTime.current = datetime(2026, 7, 20, 11, 0)
    zone = _zone(2, duration=30, postpone_until="2026-07-20 11:59:00")
    first = _zone(1, duration=30)
    program = _program(time="11:49", zones=[1, 2])
    fake_db = Mock()
    fake_db.is_program_run_cancelled_for_group.side_effect = lambda program_id, run_date, group_id: (
        run_date == "2026-07-20"
    )
    monkeypatch.setattr(nw, "db", fake_db)

    result = _compute(monkeypatch, [first, zone], [program], zone_ids=[2])

    assert result[2]["next_dt"] == datetime(2026, 7, 21, 12, 19)


def test_cross_midnight_active_program_keeps_upcoming_zone_visible(monkeypatch):
    _FrozenDateTime.current = datetime(2026, 7, 21, 0, 10)
    zones = [
        _zone(1, duration=30, state="on", source="schedule"),
        _zone(2, duration=30),
    ]
    program = _program(time="23:50", days=[0], zones=[1, 2])

    result = _compute(monkeypatch, zones, [program], zone_ids=[2])

    assert result[2]["next_dt"] == datetime(2026, 7, 21, 0, 20)


def test_active_program_offsets_use_current_h1_weather_coefficient(monkeypatch):
    _FrozenDateTime.current = datetime(2026, 7, 20, 6, 5)
    zones = [
        _zone(1, state="on", source="schedule"),
        _zone(2),
        _zone(3),
    ]
    program = _program(zones=[1, 2, 3])
    monkeypatch.setattr(nw, "_weather_duration_coefficient", lambda: 50, raising=False)

    result = _compute(monkeypatch, zones, [program], zone_ids=[2, 3])

    assert result[2]["next_dt"] == datetime(2026, 7, 20, 6, 10)
    assert result[3]["next_dt"] == datetime(2026, 7, 20, 6, 20)


def test_finished_weather_shortened_program_has_no_phantom_nominal_slot(monkeypatch):
    _FrozenDateTime.current = datetime(2026, 7, 20, 6, 35)
    zones = [_zone(1), _zone(2), _zone(3)]
    program = _program(zones=[1, 2, 3])
    monkeypatch.setattr(nw, "_weather_duration_coefficient", lambda: 50, raising=False)

    result = _compute(monkeypatch, zones, [program], zone_ids=[3])

    assert result[3]["next_dt"] == datetime(2026, 7, 21, 6, 40)


def test_weather_skip_allows_tomorrow_midnight(monkeypatch):
    _FrozenDateTime.current = datetime(2026, 7, 20, 12, 0)

    result = _compute(monkeypatch, [_zone(1)], [_program(time="00:00")], skip_today=True)

    assert result[1]["next_dt"] == datetime(2026, 7, 21, 0, 0)


def test_malformed_main_time_is_not_invented_as_midnight(monkeypatch):
    result = _compute(monkeypatch, [_zone(1)], [_program(time="not-a-time")])

    assert result[1]["next_dt"] is None
    assert result[1]["has_programs"] is False


def test_malformed_main_does_not_hide_valid_extra_time(monkeypatch):
    result = _compute(
        monkeypatch,
        [_zone(1)],
        [_program(time="not-a-time", extra_times=["18:00"])],
    )

    assert result[1]["next_dt"] == datetime(2026, 7, 20, 18, 0)


def test_cancellation_lookup_is_memoized_per_program_date_group(monkeypatch):
    zones = [_zone(zone_id) for zone_id in range(1, 33)]
    programs = [_program(program_id, time="18:00", zones=list(range(1, 33))) for program_id in range(1, 17)]
    fake_db = Mock()
    fake_db.is_program_run_cancelled_for_group.return_value = False
    monkeypatch.setattr(nw, "db", fake_db)

    _compute(monkeypatch, zones, programs)

    assert fake_db.is_program_run_cancelled_for_group.call_count == 16


def test_cache_only_weather_rejects_stale_row_without_network(tmp_path):
    from services.weather import WeatherService

    db_path = str(tmp_path / "weather.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "CREATE TABLE weather_cache ("
            "id INTEGER PRIMARY KEY, latitude REAL, longitude REAL, data TEXT, fetched_at REAL)"
        )
        conn.executemany(
            "INSERT INTO settings(key, value) VALUES (?, ?)",
            [("weather.latitude", "42.87"), ("weather.longitude", "74.59")],
        )
        conn.execute(
            "INSERT INTO weather_cache(latitude, longitude, data, fetched_at) VALUES (?, ?, ?, ?)",
            (42.87, 74.59, json.dumps({"hourly": {}, "daily": {}}), time.time() - 10 * 86400),
        )
    service = WeatherService(db_path)
    service._fetch_api = Mock()

    assert service.get_weather(cache_only=True) is None
    service._fetch_api.assert_not_called()
