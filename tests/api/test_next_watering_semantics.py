"""Семантика «следующего полива» единого калькулятора services.next_watering.

Фиксирует контракт, согласованный с планировщиком (irrigation_scheduler
пропускает weekdays-программы с пустым days): программа без дней недели
НИКОГДА не запускается, поэтому и single-, и bulk-эндпоинты обязаны
возвращать «Никогда» — раньше single-эндпоинт трактовал пустой days как
«каждый день» и показывал несуществующий запуск.
"""

import json
import os
from datetime import datetime, timedelta
from unittest.mock import patch

os.environ["TESTING"] = "1"


def _bulk_item(client, zone_id):
    resp = client.post(
        "/api/zones/next-watering-bulk",
        data=json.dumps({"zone_ids": [zone_id]}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    items = resp.get_json().get("items") or []
    assert len(items) == 1
    return items[0]


class TestEmptyDaysMeansNever:
    def test_single_endpoint_empty_days_is_never(self, admin_client, app):
        z = app.db.create_zone({"name": "ED1", "duration": 10, "group_id": 1})
        app.db.create_program({"name": "NoDays", "time": "06:00", "days": [], "zones": [z["id"]]})

        resp = admin_client.get(f"/api/zones/{z['id']}/next-watering")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["next_watering"] == "Никогда"
        assert not data.get("next_datetime")

    def test_bulk_endpoint_empty_days_is_never(self, admin_client, app):
        z = app.db.create_zone({"name": "ED2", "duration": 10, "group_id": 1})
        app.db.create_program({"name": "NoDaysBulk", "time": "06:00", "days": [], "zones": [z["id"]]})

        item = _bulk_item(admin_client, z["id"])
        assert item["next_datetime"] is None
        assert item["next_watering"] == "Никогда"

    def test_single_and_bulk_agree_for_daily_program(self, admin_client, app):
        z = app.db.create_zone({"name": "ED3", "duration": 10, "group_id": 1})
        app.db.create_program({"name": "Daily", "time": "04:30", "days": [0, 1, 2, 3, 4, 5, 6], "zones": [z["id"]]})

        item = _bulk_item(admin_client, z["id"])
        assert item["next_datetime"] is not None
        bulk_dt = datetime.strptime(item["next_datetime"], "%Y-%m-%d %H:%M:%S")

        resp = admin_client.get(f"/api/zones/{z['id']}/next-watering")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data.get("next_datetime"), data
        single_dt = datetime.strptime(data["next_datetime"], "%Y-%m-%d %H:%M")
        assert single_dt == bulk_dt.replace(second=0)
        assert single_dt > datetime.now()
        assert single_dt <= datetime.now() + timedelta(days=1, minutes=1)


class TestScheduleTypesNextWatering:
    """interval/even-odd программы (days=[]) не должны давать «Никогда» —
    планировщик их реально запускает (семантика program_runs_on)."""

    def test_interval_program_gives_slot(self, admin_client, app):
        z = app.db.create_zone({"name": "IntNW", "duration": 10, "group_id": 1})
        program = app.db.create_program(
            {
                "name": "IntervalNW",
                "time": "06:00",
                "days": [],
                "zones": [z["id"]],
                "schedule_type": "interval",
                "interval_days": 2,
            }
        )
        # Interval cadence is anchored in the live APScheduler job, not in the
        # program row.  TESTING deliberately does not start that scheduler, so
        # inject the same next_run_time metadata production provides.
        next_run = (datetime.now() + timedelta(days=2)).replace(hour=6, minute=0, second=0, microsecond=0)
        with patch(
            "services.next_watering._get_interval_next_runs",
            return_value={(program["id"], "main"): next_run},
        ):
            item = _bulk_item(admin_client, z["id"])
        assert item["next_datetime"] is not None
        assert item["next_watering"] != "Никогда"

    def test_even_odd_program_gives_slot_within_two_days(self, admin_client, app):
        z = app.db.create_zone({"name": "EONW", "duration": 10, "group_id": 1})
        app.db.create_program(
            {
                "name": "EvenOddNW",
                "time": "06:00",
                "days": [],
                "zones": [z["id"]],
                "schedule_type": "even-odd",
                "even_odd": "even",
            }
        )
        item = _bulk_item(admin_client, z["id"])
        assert item["next_datetime"] is not None
        nd = datetime.strptime(item["next_datetime"], "%Y-%m-%d %H:%M:%S")
        assert nd.day % 2 == 0
        assert nd <= datetime.now() + timedelta(days=3)
