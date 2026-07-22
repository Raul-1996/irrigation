"""Regression tests for strict and atomic Programs API mutations."""

from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
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

    @pytest.mark.parametrize(
        "color",
        [
            '" onmouseover="alert(1)',
            "red",
            "#fff",
            "#12345678",
            "#12GG34",
            123456,
            None,
        ],
    )
    def test_post_rejects_noncanonical_color_before_persist(self, admin_client, app, color):
        zone_id = _zone(app)

        response = admin_client.post("/api/programs", json=_payload(zone_id, color=color))

        assert response.status_code == 400
        assert response.get_json()["success"] is False
        assert app.db.get_programs() == []

    def test_post_normalizes_hex_color_to_lowercase(self, admin_client, app):
        zone_id = _zone(app)

        response = admin_client.post("/api/programs", json=_payload(zone_id, color="#AaBbCc"))

        assert response.status_code == 201
        assert response.get_json()["color"] == "#aabbcc"
        assert app.db.get_program(response.get_json()["id"])["color"] == "#aabbcc"

    def test_unsafe_put_color_is_rejected_before_db_and_scheduler_mutation(self, admin_client, app, monkeypatch):
        zone_id = _zone(app)
        original = app.db.create_program(_payload(zone_id, color="#112233"))
        assert original is not None
        scheduled = []
        monkeypatch.setattr(
            programs_api,
            "get_scheduler",
            lambda: SimpleNamespace(schedule_program=lambda *args: scheduled.append(args)),
        )

        response = admin_client.put(
            f"/api/programs/{original['id']}",
            json={"color": '" onmouseover="alert(1)'},
        )

        assert response.status_code == 400
        assert app.db.get_program(original["id"])["color"] == "#112233"
        assert scheduled == []

    def test_unsafe_color_on_enabled_patch_blocks_all_mutation(self, admin_client, app, monkeypatch):
        zone_id = _zone(app)
        original = app.db.create_program(_payload(zone_id, color="#112233", enabled=True))
        assert original is not None
        scheduled = []
        monkeypatch.setattr(
            programs_api,
            "get_scheduler",
            lambda: SimpleNamespace(schedule_program=lambda *args: scheduled.append(args)),
        )

        response = admin_client.patch(
            f"/api/programs/{original['id']}/enabled",
            json={"enabled": False, "color": '" onmouseover="alert(1)'},
        )

        assert response.status_code == 400
        assert app.db.get_program(original["id"])["enabled"] is True
        assert app.db.get_program(original["id"])["color"] == "#112233"
        assert scheduled == []

    def test_stale_put_zone_returns_structured_conflict_without_mutation(self, admin_client, app, monkeypatch):
        zone_id = _zone(app)
        original = app.db.create_program(_payload(zone_id))
        assert original is not None
        with sqlite3.connect(app.db.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE programs SET zones = '[]' WHERE id = ?", (original["id"],))
            conn.execute("DELETE FROM zones WHERE id = ?", (zone_id,))
            conn.commit()
        scheduled = []
        monkeypatch.setattr(
            programs_api,
            "get_scheduler",
            lambda: SimpleNamespace(schedule_program=lambda *args: scheduled.append(args)),
        )

        response = admin_client.put(
            f"/api/programs/{original['id']}",
            json={"name": "Stale editor", "zones": [zone_id]},
        )

        assert response.status_code == 409
        assert response.get_json() == {
            "success": False,
            "message": "One or more program zones no longer exist",
            "error_code": "PROGRAM_ZONES_NOT_FOUND",
            "missing_zone_ids": [zone_id],
        }
        stored = app.db.get_program(original["id"])
        assert stored is not None
        assert stored["name"] == "Morning"
        assert stored["zones"] == []
        assert scheduled == []

    def test_post_missing_zone_returns_structured_conflict_without_write(self, admin_client, app):
        response = admin_client.post("/api/programs", json=_payload(999999, extra_times=[]))

        assert response.status_code == 409
        assert response.get_json()["error_code"] == "PROGRAM_ZONES_NOT_FOUND"
        assert response.get_json()["missing_zone_ids"] == [999999]
        assert app.db.get_programs() == []

    def test_post_rejects_unbounded_extra_times(self, admin_client, app):
        zone_id = _zone(app)
        extra_times = [f"00:{minute:02d}" for minute in range(25)]

        response = admin_client.post("/api/programs", json=_payload(zone_id, extra_times=extra_times))

        assert response.status_code == 400
        assert "at most 24" in response.get_json()["message"]
        assert app.db.get_programs() == []

    def test_legacy_unsafe_color_is_never_returned_by_api(self, admin_client, app):
        zone_id = _zone(app)
        with sqlite3.connect(app.db.db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO programs (name, time, days, zones, color)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("Legacy unsafe", "06:00", "[0]", f"[{zone_id}]", '" onmouseover="alert(1)'),
            )
            program_id = int(cursor.lastrowid)

        response = admin_client.get(f"/api/programs/{program_id}")

        assert response.status_code == 200
        assert response.get_json()["color"] == "#42a5f5"


class TestProgramMutationGuards:
    def test_enabled_reconcile_without_scheduler_fails_outside_testing(self, app):
        previous_testing = app.config["TESTING"]
        with app.app_context():
            app.config["TESTING"] = False
            try:
                assert programs_api._reconcile_program_schedule(None, {"enabled": True}, None) is False
                assert programs_api._reconcile_program_schedule(None, {"enabled": False}, None) is True
            finally:
                app.config["TESTING"] = previous_testing

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

    def test_manual_run_rejects_when_scheduler_is_unavailable(self, admin_client, app, monkeypatch):
        zone_id = _zone(app)
        program = app.db.create_program(_payload(zone_id, extra_times=[]))
        assert program is not None
        dispatched = []

        class RecordingThread:
            def __init__(self, *args, **kwargs):
                dispatched.append((args, kwargs))

            def start(self):
                return None

        monkeypatch.setattr(programs_api, "get_scheduler", lambda: None)
        monkeypatch.setattr(programs_api.threading, "Thread", RecordingThread)

        response = admin_client.post(f"/api/programs/{program['id']}/run")

        assert response.status_code == 503
        assert response.get_json() == {
            "success": False,
            "message": "Scheduler unavailable",
            "error_code": "SCHEDULER_UNAVAILABLE",
        }
        assert dispatched == []

    def test_manual_run_rejects_program_with_only_fault_zones(self, admin_client, app, monkeypatch):
        zone_id = _zone(app)
        assert app.db.update_zone(zone_id, {"state": "fault"}) is not None
        program = app.db.create_program(_payload(zone_id, extra_times=[]))
        assert program is not None
        dispatched = []

        class RecordingThread:
            def __init__(self, *args, **kwargs):
                dispatched.append((args, kwargs))

            def start(self):
                return None

        monkeypatch.setattr(programs_api, "get_scheduler", lambda: object())
        monkeypatch.setattr(programs_api.threading, "Thread", RecordingThread)

        response = admin_client.post(f"/api/programs/{program['id']}/run")

        assert response.status_code == 409
        assert response.get_json() == {
            "success": False,
            "message": "Program has no runnable zones",
            "error_code": "PROGRAM_NO_RUNNABLE_ZONES",
        }
        assert dispatched == []

    def test_manual_run_dispatch_returns_202_without_claiming_physical_start(self, admin_client, app, monkeypatch):
        zone_id = _zone(app)
        program = app.db.create_program(_payload(zone_id, extra_times=[]))
        assert program is not None
        dispatched = []

        class DeferredThread:
            def __init__(self, *args, **kwargs):
                dispatched.append((args, kwargs))

            def start(self):
                return None

        monkeypatch.setattr(programs_api, "get_scheduler", lambda: object())
        monkeypatch.setattr(programs_api.threading, "Thread", DeferredThread)

        response = admin_client.post(f"/api/programs/{program['id']}/run")

        assert response.status_code == 202
        assert response.get_json() == {
            "success": True,
            "accepted": True,
            "started": False,
            "status": "accepted",
            "message": "Программа Morning: запрос на запуск принят",
        }
        assert len(dispatched) == 1
        assert dispatched[0][1]["target"].__name__ == "job_run_program"
        assert dispatched[0][1]["kwargs"] == {"manual": True}

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

    def test_reenable_runs_conflict_admission_before_mutation(self, admin_client, app, monkeypatch):
        zone_id = _zone(app)
        disabled = app.db.create_program(_payload(zone_id, name="Disabled", enabled=False, extra_times=[]))
        active = app.db.create_program(_payload(zone_id, name="Active", enabled=True, extra_times=[]))
        assert disabled is not None and active is not None
        scheduled = []
        monkeypatch.setattr(
            programs_api,
            "get_scheduler",
            lambda: SimpleNamespace(schedule_program=lambda *args, **kwargs: scheduled.append((args, kwargs))),
        )

        response = admin_client.patch(f"/api/programs/{disabled['id']}/enabled", json={"enabled": True})

        assert response.status_code == 409
        assert response.get_json()["has_conflicts"] is True
        assert response.get_json()["conflicts"][0]["program_id"] == active["id"]
        assert app.db.get_program(disabled["id"])["enabled"] is False
        assert scheduled == []

    def test_duplicate_is_created_disabled_without_scheduling(self, admin_client, app, monkeypatch):
        zone_id = _zone(app)
        original = app.db.create_program(_payload(zone_id, name="Original", enabled=True, extra_times=[]))
        assert original is not None
        scheduled = []
        monkeypatch.setattr(
            programs_api,
            "get_scheduler",
            lambda: SimpleNamespace(schedule_program=lambda *args, **kwargs: scheduled.append((args, kwargs))),
        )

        response = admin_client.post(f"/api/programs/{original['id']}/duplicate")

        assert response.status_code == 201
        assert response.get_json()["program"]["enabled"] is False
        assert scheduled == []

    def test_put_keeps_db_commit_and_scheduler_replacement_in_one_serial_order(self, app, monkeypatch):
        zone_id = _zone(app)
        original = app.db.create_program(
            _payload(
                zone_id,
                schedule_type="interval",
                interval_days=3,
                days=[],
                extra_times=[],
            )
        )
        assert original is not None

        a_schedule_entered = threading.Event()
        release_a_schedule = threading.Event()
        b_request_started = threading.Event()
        b_db_read = threading.Event()
        schedule_order = []
        responses = {}
        active_anchor = {"main": datetime(2030, 1, 1, 6, 0, tzinfo=UTC)}
        active_job = {}

        original_get_program = programs_api.db.get_program

        def observed_get_program(program_id):
            if threading.current_thread().name == "program-put-b":
                b_db_read.set()
            return original_get_program(program_id)

        class BlockingScheduler:
            scheduler = SimpleNamespace(timezone=UTC)

            def get_program_interval_anchors(self, _program_id):
                return dict(active_anchor)

            def program_schedule_fingerprint(self, _program_id, program):
                return f"{program['name']}:{program['time']}"

            def schedule_program(self, _program_id, program, **kwargs):
                if program["name"] == "A":
                    a_schedule_entered.set()
                    assert release_a_schedule.wait(timeout=3)
                schedule_order.append((program["name"], kwargs))
                active_anchor.update(kwargs["interval_anchors"])
                active_job.update(
                    {
                        "name": program["name"],
                        "anchor": kwargs["interval_anchors"]["main"],
                        "fingerprint": kwargs["expected_fingerprint"],
                    }
                )
                return True

        monkeypatch.setattr(programs_api.db, "get_program", observed_get_program)
        scheduler = BlockingScheduler()
        monkeypatch.setattr(programs_api, "get_scheduler", lambda: scheduler)

        def update(name, time_value):
            if name == "B":
                b_request_started.set()
            client = app.test_client()
            responses[name] = client.put(
                f"/api/programs/{original['id']}",
                json={"name": name, "time": time_value},
            )

        thread_a = threading.Thread(target=update, args=("A", "07:00"), name="program-put-a")
        thread_b = threading.Thread(target=update, args=("B", "08:00"), name="program-put-b")
        thread_a.start()
        assert a_schedule_entered.wait(timeout=3)
        thread_b.start()
        assert b_request_started.wait(timeout=3)
        try:
            assert not b_db_read.wait(timeout=0.2), "B reached the DB while A's schedule replacement was pending"
            assert original_get_program(original["id"])["name"] == "A"
        finally:
            release_a_schedule.set()
            thread_a.join(timeout=3)
            thread_b.join(timeout=3)

        assert not thread_a.is_alive()
        assert not thread_b.is_alive()
        assert responses["A"].status_code == 200
        assert responses["B"].status_code == 200
        assert [name for name, _kwargs in schedule_order] == ["A", "B"]
        assert schedule_order[0][1]["interval_anchors"]["main"].strftime("%H:%M") == "07:00"
        assert schedule_order[0][1]["expected_fingerprint"] == "A:07:00"
        assert schedule_order[1][1]["interval_anchors"]["main"].strftime("%H:%M") == "08:00"
        assert schedule_order[1][1]["expected_fingerprint"] == "B:08:00"
        assert original_get_program(original["id"])["name"] == "B"
        assert active_job["name"] == "B"
        assert active_job["anchor"].strftime("%H:%M") == "08:00"
        assert active_job["fingerprint"] == "B:08:00"

    def test_delete_waits_for_pending_put_scheduler_replacement(self, app, monkeypatch):
        zone_id = _zone(app)
        original = app.db.create_program(_payload(zone_id, extra_times=[]))
        assert original is not None
        schedule_entered = threading.Event()
        release_schedule = threading.Event()
        delete_started = threading.Event()
        delete_reached_db = threading.Event()
        responses = {}
        real_delete_program = programs_api.db.delete_program

        def observed_delete_program(program_id):
            if threading.current_thread().name == "program-delete":
                delete_reached_db.set()
            return real_delete_program(program_id)

        class BlockingScheduler:
            def schedule_program(self, *_args, **_kwargs):
                schedule_entered.set()
                assert release_schedule.wait(timeout=3)
                return True

            def cancel_program(self, _program_id):
                return True

        scheduler = BlockingScheduler()
        monkeypatch.setattr(programs_api.db, "delete_program", observed_delete_program)
        monkeypatch.setattr(programs_api, "get_scheduler", lambda: scheduler)

        def update():
            client = app.test_client()
            responses["put"] = client.put(f"/api/programs/{original['id']}", json={"name": "Updated"})

        def delete():
            delete_started.set()
            client = app.test_client()
            responses["delete"] = client.delete(f"/api/programs/{original['id']}")

        update_thread = threading.Thread(target=update, name="program-put")
        delete_thread = threading.Thread(target=delete, name="program-delete")
        update_thread.start()
        assert schedule_entered.wait(timeout=3)
        delete_thread.start()
        assert delete_started.wait(timeout=3)
        try:
            assert not delete_reached_db.wait(timeout=0.2), "DELETE bypassed pending PUT schedule replacement"
            assert app.db.get_program(original["id"])["name"] == "Updated"
        finally:
            release_schedule.set()
            update_thread.join(timeout=3)
            delete_thread.join(timeout=3)

        assert not update_thread.is_alive()
        assert not delete_thread.is_alive()
        assert responses["put"].status_code == 200
        assert responses["delete"].status_code == 204
        assert app.db.get_program(original["id"]) is None

    def test_delete_cancel_failure_returns_503_instead_of_false_success(self, admin_client, app, monkeypatch):
        zone_id = _zone(app)
        original = app.db.create_program(_payload(zone_id, extra_times=[]))
        assert original is not None

        class RejectingScheduler:
            def cancel_program(self, _program_id):
                return False

        monkeypatch.setattr(programs_api, "get_scheduler", lambda: RejectingScheduler())

        response = admin_client.delete(f"/api/programs/{original['id']}")

        assert response.status_code == 503
        assert response.get_json()["error_code"] == "PROGRAM_SCHEDULE_FAILED"
        assert app.db.get_program(original["id"]) is None

    def test_post_scheduler_failure_returns_503_and_removes_created_program(self, admin_client, app, monkeypatch):
        zone_id = _zone(app)

        class RejectingScheduler:
            def schedule_program(self, *_args, **_kwargs):
                return False

            def cancel_program(self, _program_id):
                return None

        monkeypatch.setattr(programs_api, "get_scheduler", lambda: RejectingScheduler())

        response = admin_client.post("/api/programs", json=_payload(zone_id, extra_times=[]))

        assert response.status_code == 503
        assert response.get_json()["error_code"] == "PROGRAM_SCHEDULE_FAILED"
        assert app.db.get_programs() == []

    def test_put_scheduler_failure_returns_503_and_restores_previous_program(self, admin_client, app, monkeypatch):
        zone_id = _zone(app)
        original = app.db.create_program(
            _payload(
                zone_id,
                name="Before",
                time="06:00",
                schedule_type="interval",
                interval_days=3,
                days=[],
                extra_times=[],
            )
        )
        assert original is not None
        old_anchor = datetime(2030, 1, 1, 6, 0, tzinfo=UTC)
        calls = []

        class RejectingScheduler:
            scheduler = SimpleNamespace(timezone=UTC)

            def get_program_interval_anchors(self, _program_id):
                return {"main": old_anchor}

            def program_schedule_fingerprint(self, _program_id, program):
                return f"{program['name']}:{program['time']}"

            def schedule_program(self, _program_id, program, **kwargs):
                calls.append((program, kwargs))
                return program["time"] == "06:00"

        scheduler = RejectingScheduler()
        monkeypatch.setattr(programs_api, "get_scheduler", lambda: scheduler)

        response = admin_client.put(
            f"/api/programs/{original['id']}",
            json={"name": "After", "time": "07:00"},
        )

        assert response.status_code == 503
        assert response.get_json()["error_code"] == "PROGRAM_SCHEDULE_FAILED"
        assert response.get_json()["rollback_succeeded"] is True
        assert response.get_json()["schedule_restored"] is True
        restored = app.db.get_program(original["id"])
        assert restored is not None
        assert restored["name"] == "Before"
        assert restored["time"] == "06:00"
        assert [program["time"] for program, _kwargs in calls] == ["07:00", "06:00"]
        assert calls[0][1]["expected_fingerprint"] == "After:07:00"
        assert calls[1][1]["expected_fingerprint"] == "Before:06:00"
        assert calls[1][1]["interval_anchors"] == {"main": old_anchor}

    def test_put_reports_failed_rollback_and_cancels_on_scheduler_failure(self, admin_client, app, monkeypatch):
        zone_id = _zone(app)
        original = app.db.create_program(_payload(zone_id, name="Before", extra_times=[]))
        assert original is not None
        real_update_program = programs_api.db.update_program
        update_calls = 0
        cancelled = []

        def fail_only_rollback(program_id, changes):
            nonlocal update_calls
            update_calls += 1
            if update_calls == 1:
                return real_update_program(program_id, changes)
            return None

        class RejectingScheduler:
            def schedule_program(self, *_args, **_kwargs):
                return False

            def cancel_program(self, program_id):
                cancelled.append(program_id)
                return True

        monkeypatch.setattr(programs_api.db, "update_program", fail_only_rollback)
        monkeypatch.setattr(programs_api, "get_scheduler", lambda: RejectingScheduler())

        response = admin_client.put(f"/api/programs/{original['id']}", json={"name": "After"})

        assert response.status_code == 503
        assert response.get_json()["rollback_succeeded"] is False
        assert cancelled == [original["id"]]
        assert app.db.get_program(original["id"])["name"] == "After"

    def test_put_disable_failure_keeps_fail_safe_disabled_state(self, admin_client, app, monkeypatch):
        zone_id = _zone(app)
        original = app.db.create_program(_payload(zone_id, enabled=True, extra_times=[]))
        assert original is not None

        class RejectingScheduler:
            def schedule_program(self, *_args, **_kwargs):
                return False

        monkeypatch.setattr(programs_api, "get_scheduler", lambda: RejectingScheduler())

        response = admin_client.put(
            f"/api/programs/{original['id']}",
            json={"enabled": False},
        )

        assert response.status_code == 503
        assert app.db.get_program(original["id"])["enabled"] is False

    def test_enable_scheduler_failure_returns_503_and_restores_disabled_state(self, admin_client, app, monkeypatch):
        zone_id = _zone(app)
        original = app.db.create_program(_payload(zone_id, enabled=False, extra_times=[]))
        assert original is not None

        class RejectingScheduler:
            def schedule_program(self, *_args, **_kwargs):
                return False

        monkeypatch.setattr(programs_api, "get_scheduler", lambda: RejectingScheduler())

        response = admin_client.patch(f"/api/programs/{original['id']}/enabled", json={"enabled": True})

        assert response.status_code == 503
        assert response.get_json()["error_code"] == "PROGRAM_SCHEDULE_FAILED"
        assert app.db.get_program(original["id"])["enabled"] is False

    def test_disable_scheduler_failure_returns_503_but_keeps_fail_safe_disabled_state(
        self,
        admin_client,
        app,
        monkeypatch,
    ):
        zone_id = _zone(app)
        original = app.db.create_program(_payload(zone_id, enabled=True, extra_times=[]))
        assert original is not None

        class RejectingScheduler:
            def schedule_program(self, *_args, **_kwargs):
                return False

        monkeypatch.setattr(programs_api, "get_scheduler", lambda: RejectingScheduler())

        response = admin_client.patch(f"/api/programs/{original['id']}/enabled", json={"enabled": False})

        assert response.status_code == 503
        assert response.get_json()["error_code"] == "PROGRAM_SCHEDULE_FAILED"
        assert app.db.get_program(original["id"])["enabled"] is False


class TestIntervalAnchorContracts:
    def test_missing_interval_anchor_does_not_block_put_disable(self, admin_client, app, monkeypatch):
        zone_id = _zone(app)
        program = app.db.create_program(
            _payload(
                zone_id,
                schedule_type="interval",
                interval_days=3,
                days=[],
                extra_times=[],
            )
        )
        assert program is not None
        scheduled = []

        class FakeScheduler:
            scheduler = SimpleNamespace(timezone=UTC)

            def get_program_interval_anchors(self, _program_id):
                return {}

            def schedule_program(self, *args, **kwargs):
                scheduled.append((args, kwargs))
                return True

        monkeypatch.setattr(programs_api, "get_scheduler", lambda: FakeScheduler())

        response = admin_client.put(f"/api/programs/{program['id']}", json={"enabled": False})

        assert response.status_code == 200
        assert response.get_json()["enabled"] is False
        assert len(scheduled) == 1
        assert scheduled[0][1]["interval_anchors"] is None

    def test_missing_old_interval_anchor_does_not_block_repair_to_weekdays(self, admin_client, app, monkeypatch):
        zone_id = _zone(app)
        program = app.db.create_program(
            _payload(
                zone_id,
                schedule_type="interval",
                interval_days=3,
                days=[],
                extra_times=[],
            )
        )
        assert program is not None
        scheduled = []

        class FakeScheduler:
            scheduler = SimpleNamespace(timezone=UTC)

            def get_program_interval_anchors(self, _program_id):
                return {}

            def schedule_program(self, *args, **kwargs):
                scheduled.append((args, kwargs))
                return True

        monkeypatch.setattr(programs_api, "get_scheduler", lambda: FakeScheduler())

        response = admin_client.put(
            f"/api/programs/{program['id']}",
            json={"schedule_type": "weekdays", "days": [0]},
        )

        assert response.status_code == 200
        assert response.get_json()["schedule_type"] == "weekdays"
        assert len(scheduled) == 1
        assert "interval_anchors" not in scheduled[0][1]

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
        monday_date = today + timedelta(days=(7 - today.weekday()) % 7)
        monday = datetime(monday_date.year, monday_date.month, monday_date.day, 6, tzinfo=UTC)

        class FakeScheduler:
            scheduler = SimpleNamespace(timezone=UTC)

            def get_program_interval_anchors(self, program_id):
                assert program_id == stored["id"]
                return {"main": monday}

            def schedule_program(self, *args, **kwargs):
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

    def test_admitted_interval_anchor_is_passed_unchanged_to_scheduler(self, admin_client, app, monkeypatch):
        zone_id = _zone(app)
        anchor = datetime(2030, 1, 2, 6, 0, tzinfo=UTC)
        candidate_anchors = {"06:00": anchor}
        checked = []
        scheduled = []

        class FakeScheduler:
            scheduler = SimpleNamespace(timezone=UTC)

            def schedule_program(self, *args, **kwargs):
                scheduled.append((args, kwargs))
                return True

        monkeypatch.setattr(programs_api, "get_scheduler", lambda: FakeScheduler())
        monkeypatch.setattr(
            programs_api,
            "_interval_anchor_context",
            lambda *args, **kwargs: (candidate_anchors, {}),
        )

        def capture_conflicts(**kwargs):
            checked.append(kwargs["candidate_interval_anchors"])
            return []

        monkeypatch.setattr(programs_api, "_check_candidate_conflicts", capture_conflicts)

        response = admin_client.post(
            "/api/programs",
            json=_payload(
                zone_id,
                schedule_type="interval",
                interval_days=3,
                days=[],
                extra_times=[],
            ),
        )

        assert response.status_code == 201
        assert checked == [candidate_anchors]
        assert scheduled[0][1]["interval_anchors"] == {"main": anchor}
        assert scheduled[0][1]["interval_anchors"]["main"] is anchor

    def test_unchanged_interval_put_fails_closed_without_authoritative_anchor(self, admin_client, app, monkeypatch):
        zone_id = _zone(app)
        program = app.db.create_program(
            _payload(
                zone_id,
                name="Stored interval",
                schedule_type="interval",
                interval_days=3,
                days=[],
                extra_times=[],
            )
        )
        assert program is not None

        class FakeScheduler:
            scheduler = SimpleNamespace(timezone=UTC)

            def get_program_interval_anchors(self, _program_id):
                return {}

            def schedule_program(self, *args, **kwargs):
                raise AssertionError("program without authoritative anchors must not be scheduled")

        monkeypatch.setattr(programs_api, "get_scheduler", lambda: FakeScheduler())

        response = admin_client.put(f"/api/programs/{program['id']}", json={"name": "Renamed"})

        assert response.status_code == 503
        assert response.get_json()["error_code"] == "INTERVAL_ANCHOR_UNAVAILABLE"
        assert app.db.get_program(program["id"])["name"] == "Stored interval"


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

    def test_conflict_preview_preserves_current_weather_coefficient(self, admin_client, app):
        zone_id = _zone(app)
        with sqlite3.connect(app.db.db_path) as conn:
            conn.execute(
                """
                INSERT INTO weather_decisions (date, time, coefficient, decision, mode)
                VALUES ('2030-01-01', '06:00:00', 137, 'adjust', 'auto')
                """
            )

        response = admin_client.post(
            "/api/programs/check-conflicts",
            json={"time": "06:00", "days": [0], "zones": [zone_id]},
        )

        assert response.status_code == 200
        assert response.get_json()["current_weather_coefficient"] == 137

    def test_conflict_preview_rejects_unbounded_extra_times(self, admin_client, app):
        zone_id = _zone(app)

        response = admin_client.post(
            "/api/programs/check-conflicts",
            json={
                "time": "06:00",
                "days": [0],
                "zones": [zone_id],
                "extra_times": [f"00:{minute:02d}" for minute in range(25)],
            },
        )

        assert response.status_code == 400
        assert "at most 24" in response.get_json()["message"]

    def test_ordinary_create_and_update_do_not_fetch_weather_under_mutation_lock(self, admin_client, app, monkeypatch):
        zone_id = _zone(app)
        calls = []

        def forbidden_weather_lookup(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("ordinary mutation admission must not fetch weather")

        import services.weather_adjustment as weather_adjustment

        monkeypatch.setattr(weather_adjustment, "get_weather_adjustment", forbidden_weather_lookup)

        create_response = admin_client.post("/api/programs", json=_payload(zone_id, extra_times=[]))

        assert create_response.status_code == 201
        update_response = admin_client.put(
            f"/api/programs/{create_response.get_json()['id']}",
            json={"name": "Updated without weather lookup"},
        )

        assert update_response.status_code == 200
        assert calls == []


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
