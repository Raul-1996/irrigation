"""Regression tests for strict and atomic Programs API mutations."""

from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from routes import programs_api


def _zone(app) -> int:
    zone = app.db.create_zone({"name": "Z1", "duration": 20, "group_id": 1})
    assert zone is not None
    return int(zone["id"])


def _payload(zone_id: int, **changes):
    payload = {
        "name": "Morning",
        "time": "06:00",
        "days": [0, 2, 4],
        "zones": [zone_id],
        "extra_times": ["18:00"],
        "enabled": True,
    }
    payload.update(changes)
    return payload


class TestStrictProgramJson:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("time", "6:00"),
            ("time", "24:00"),
            ("time", "12:60"),
            ("time", 600),
            ("zones", "12"),
            ("zones", [1, "2"]),
            ("zones", [True]),
            ("days", "0,2"),
            ("days", [7]),
            ("days", [False]),
            ("extra_times", "18:00"),
            ("extra_times", [1800]),
            ("extra_times", ["6:00"]),
            ("extra_times", ["06:00"]),
            ("enabled", 1),
            ("enabled", "false"),
        ],
    )
    def test_post_rejects_noncanonical_program_fields(self, admin_client, app, field, value):
        zone_id = _zone(app)

        response = admin_client.post("/api/programs", json=_payload(zone_id, **{field: value}))

        assert response.status_code == 400
        assert response.get_json()["success"] is False
        assert app.db.get_programs() == []

    def test_post_canonicalizes_weekdays_and_even_odd_alias(self, admin_client, app):
        zone_id = _zone(app)

        response = admin_client.post(
            "/api/programs",
            json=_payload(
                zone_id,
                schedule_type="even_odd",
                even_odd="odd",
                days=[6, 0, 6],
            ),
        )

        assert response.status_code == 201
        assert response.get_json()["schedule_type"] == "even-odd"
        assert response.get_json()["days"] == [0, 6]

    def test_malformed_put_is_rejected_before_db_and_scheduler_mutation(self, admin_client, app, monkeypatch):
        zone_id = _zone(app)
        original = app.db.create_program(_payload(zone_id))
        assert original is not None
        scheduled = []
        fake_scheduler = SimpleNamespace(schedule_program=lambda *args: scheduled.append(args))
        monkeypatch.setattr(programs_api, "get_scheduler", lambda: fake_scheduler)

        response = admin_client.put(
            f"/api/programs/{original['id']}",
            json={"time": "25:99", "zones": "12"},
        )

        assert response.status_code == 400
        assert app.db.get_program(original["id"])["time"] == "06:00"
        assert app.db.get_program(original["id"])["zones"] == [zone_id]
        assert scheduled == []

    @pytest.mark.parametrize("enabled", [0, 1, "true", None])
    def test_patch_enabled_requires_json_boolean(self, admin_client, app, enabled):
        zone_id = _zone(app)
        original = app.db.create_program(_payload(zone_id, enabled=True))
        assert original is not None

        response = admin_client.patch(
            f"/api/programs/{original['id']}/enabled",
            json={"enabled": enabled},
        )

        assert response.status_code == 400
        assert app.db.get_program(original["id"])["enabled"] is True


class TestProgramMutationGuards:
    def test_patch_enabled_honors_password_change_requirement(self, admin_client, app):
        zone_id = _zone(app)
        original = app.db.create_program(_payload(zone_id))
        assert original is not None
        assert app.db.set_setting_value("password_must_change", "1") is True

        response = admin_client.patch(
            f"/api/programs/{original['id']}/enabled",
            json={"enabled": False},
        )

        assert response.status_code == 403
        assert response.get_json()["error_code"] == "PASSWORD_MUST_CHANGE"
        assert app.db.get_program(original["id"])["enabled"] is True

    def test_disabled_program_manual_run_is_rejected(self, admin_client, app, monkeypatch):
        zone_id = _zone(app)
        program = app.db.create_program(_payload(zone_id, enabled=False))
        assert program is not None
        started = []

        class ForbiddenThread:
            def __init__(self, *args, **kwargs):
                started.append((args, kwargs))

            def start(self):
                raise AssertionError("disabled program must not start a worker")

        monkeypatch.setattr(threading, "Thread", ForbiddenThread)

        response = admin_client.post(f"/api/programs/{program['id']}/run")

        assert response.status_code == 409
        assert response.get_json()["success"] is False
        assert started == []

    def test_post_serializes_conflict_check_with_insert(self, app, monkeypatch):
        zone_id = _zone(app)
        active_checks = 0
        max_active_checks = 0
        counter_lock = threading.Lock()
        start_barrier = threading.Barrier(2)

        def slow_no_conflict(*args, **kwargs):
            nonlocal active_checks, max_active_checks
            with counter_lock:
                active_checks += 1
                max_active_checks = max(max_active_checks, active_checks)
            time.sleep(0.08)
            with counter_lock:
                active_checks -= 1
            return []

        monkeypatch.setattr(programs_api.db.programs, "check_program_conflicts", slow_no_conflict)

        def create(index):
            start_barrier.wait()
            client = app.test_client()
            return client.post(
                "/api/programs",
                json=_payload(zone_id, name=f"Concurrent {index}", time=f"0{index + 6}:00"),
            ).status_code

        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses = list(executor.map(create, range(2)))

        assert statuses == [201, 201]
        assert max_active_checks == 1

    def test_concurrent_conflicting_posts_persist_only_one_program(self, app):
        zone_id = _zone(app)
        start_barrier = threading.Barrier(2)

        def create(index):
            start_barrier.wait()
            client = app.test_client()
            response = client.post(
                "/api/programs",
                json=_payload(zone_id, name=f"Same slot {index}", extra_times=[]),
            )
            return response.status_code, response.get_json()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(create, range(2)))

        assert sorted(status for status, _body in results) == [200, 201]
        conflict_body = next(body for status, body in results if status == 200)
        assert conflict_body["success"] is False
        assert conflict_body["has_conflicts"] is True
        assert len(app.db.get_programs()) == 1

    def test_conflict_check_failure_is_fail_closed_before_insert(self, admin_client, app, monkeypatch):
        zone_id = _zone(app)

        def fail_check(**kwargs):
            raise sqlite3.OperationalError("simulated conflict-check failure")

        monkeypatch.setattr(programs_api.db.programs, "check_program_conflicts", fail_check)

        response = admin_client.post("/api/programs", json=_payload(zone_id))

        assert response.status_code == 503
        assert response.get_json()["success"] is False
        assert app.db.get_programs() == []


class TestIntervalAnchorContracts:
    def test_missing_stored_interval_anchor_blocks_candidate_visibly(self, admin_client, app, monkeypatch):
        zone_id = _zone(app)
        stored = app.db.create_program(
            _payload(
                zone_id,
                name="Stored interval",
                schedule_type="interval",
                interval_days=7,
                days=[],
                extra_times=[],
            )
        )
        assert stored is not None
        monkeypatch.setattr(programs_api, "get_scheduler", lambda: None)

        response = admin_client.post(
            "/api/programs",
            json=_payload(
                zone_id,
                name="Candidate weekday",
                days=[0, 1, 2, 3, 4, 5, 6],
                extra_times=[],
            ),
        )

        assert response.status_code == 200
        body = response.get_json()
        assert body["success"] is False
        assert body["conflicts"][0]["program_id"] == stored["id"]
        assert body["conflicts"][0]["anchor_unknown"] is True

    def test_authoritative_interval_anchors_allow_opposite_weekly_phases(self, admin_client, app, monkeypatch):
        zone_id = _zone(app)
        stored = app.db.create_program(
            _payload(
                zone_id,
                name="Stored Monday",
                schedule_type="interval",
                interval_days=7,
                days=[],
                extra_times=[],
            )
        )
        assert stored is not None
        today = date.today()
        monday = today + timedelta(days=(7 - today.weekday()) % 7)

        class FakeScheduler:
            scheduler = SimpleNamespace(timezone=None)

            def get_program_interval_anchors(self, program_id):
                assert program_id == stored["id"]
                return {"main": monday}

            def schedule_program(self, *args):
                return None

        monkeypatch.setattr(programs_api, "get_scheduler", lambda: FakeScheduler())
        monkeypatch.setattr(
            programs_api,
            "_next_candidate_interval_anchors",
            lambda program, scheduler: {program["time"]: monday + timedelta(days=1)},
        )

        response = admin_client.post(
            "/api/programs",
            json=_payload(
                zone_id,
                name="Candidate Tuesday",
                schedule_type="interval",
                interval_days=7,
                days=[],
                extra_times=[],
            ),
        )

        assert response.status_code == 201
        assert response.get_json()["name"] == "Candidate Tuesday"


class TestWeatherConflictContract:
    def test_conflict_probe_expands_both_candidate_and_stored_windows(self, admin_client, app):
        stored_zone = app.db.create_zone({"name": "Stored", "duration": 10, "group_id": 1})
        candidate_zone = app.db.create_zone({"name": "Candidate", "duration": 20, "group_id": 1})
        assert stored_zone is not None and candidate_zone is not None
        stored = app.db.create_program(
            {
                "name": "Stored",
                "time": "06:00",
                "days": [0],
                "zones": [stored_zone["id"]],
            }
        )
        assert stored is not None

        response = admin_client.post(
            "/api/programs/check-conflicts",
            json={
                "time": "05:30",
                "days": [0],
                "zones": [candidate_zone["id"]],
                "weather_factor": 200,
            },
        )

        assert response.status_code == 200
        body = response.get_json()
        assert body["has_conflicts"] is True
        assert body["conflicts"][0]["program_id"] == stored["id"]
        assert body["conflicts"][0]["level"] == "warning"


class TestMutationRowcountContracts:
    def test_delete_missing_program_returns_404(self, admin_client):
        response = admin_client.delete("/api/programs/999999")

        assert response.status_code == 404

    def test_put_lost_row_returns_404_without_scheduling(self, admin_client, app, monkeypatch):
        zone_id = _zone(app)
        program = app.db.create_program(_payload(zone_id))
        assert program is not None
        scheduled = []
        monkeypatch.setattr(programs_api.db, "update_program", lambda *args, **kwargs: None)
        monkeypatch.setattr(
            programs_api,
            "get_scheduler",
            lambda: SimpleNamespace(schedule_program=lambda *args: scheduled.append(args)),
        )

        response = admin_client.put(
            f"/api/programs/{program['id']}",
            json={"name": "Lost update"},
        )

        assert response.status_code == 404
        assert scheduled == []
