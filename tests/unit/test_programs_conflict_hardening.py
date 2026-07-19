"""Regression tests for safe ProgramRepository storage and conflict models."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

from db.programs import ProgramRepository


def _zone(test_db, *, name: str, duration: int, group_id: int = 1) -> int:
    zone = test_db.create_zone({"name": name, "duration": duration, "group_id": group_id})
    assert zone is not None
    return int(zone["id"])


def _program(zone_id: int, **changes):
    data = {
        "name": "Program",
        "time": "06:00",
        "days": [0],
        "zones": [zone_id],
        "enabled": True,
    }
    data.update(changes)
    return data


def _next_monday() -> date:
    today = date.today()
    return today + timedelta(days=(7 - today.weekday()) % 7)


class TestProgramStorageHardening:
    def test_repository_rejects_scalar_zones(self, test_db):
        assert test_db.create_program(_program(1, zones="12")) is None
        assert test_db.get_programs() == []

    def test_repository_rejects_scalar_extra_times(self, test_db):
        assert test_db.create_program(_program(1, extra_times="18:00")) is None
        assert test_db.get_programs() == []

    def test_reads_poisoned_scalar_lists_as_safe_empty_lists(self, test_db):
        with sqlite3.connect(test_db.db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO programs(name, time, days, zones, extra_times)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("Poisoned", "06:00", "[0]", json.dumps("12"), json.dumps("18:00")),
            )
            program_id = int(cursor.lastrowid)

        program = test_db.get_program(program_id)

        assert program is not None
        assert program["zones"] == []
        assert program["extra_times"] == []
        startup_program = next(item for item in test_db.get_programs() if item["id"] == program_id)
        assert startup_program["zones"] == []
        assert startup_program["extra_times"] == []

    def test_update_missing_program_returns_none(self, test_db):
        assert test_db.update_program(999999, {"name": "Missing"}) is None

    def test_repository_rejects_partial_switch_to_invalid_interval_atomically(self, test_db):
        program = test_db.create_program(_program(1))
        assert program is not None

        result = test_db.update_program(program["id"], {"schedule_type": "interval"})

        assert result is None
        assert test_db.get_program(program["id"])["schedule_type"] == "weekdays"

    def test_delete_missing_program_returns_false(self, test_db):
        assert test_db.delete_program(999999) is False

    def test_delete_clears_cancellations_before_id_reuse(self, test_db):
        program = test_db.create_program(_program(1))
        assert program is not None
        assert test_db.cancel_program_run_for_group(program["id"], "2026-07-19", 1) is True

        assert test_db.delete_program(program["id"]) is True
        replacement = test_db.create_program(_program(1, name="Replacement"))

        assert replacement is not None
        assert replacement["id"] == program["id"]
        assert test_db.is_program_run_cancelled_for_group(replacement["id"], "2026-07-19", 1) is False


class TestScheduleAwareConflicts:
    def test_stored_interval_program_with_empty_days_conflicts(self, test_db):
        zone_id = _zone(test_db, name="Interval", duration=30)
        stored = test_db.create_program(
            _program(
                zone_id,
                schedule_type="interval",
                interval_days=3,
                days=[],
            )
        )
        assert stored is not None

        conflicts = ProgramRepository(test_db.db_path).check_program_conflicts(
            time="06:10",
            zones=[zone_id],
            days=[0],
            schedule_type="weekdays",
        )

        assert [conflict["program_id"] for conflict in conflicts] == [stored["id"]]
        assert conflicts[0]["anchor_unknown"] is True

    def test_interval_anchors_prevent_false_conflict_between_opposite_weekly_phases(self, test_db):
        zone_id = _zone(test_db, name="Anchored interval", duration=30)
        stored = test_db.create_program(
            _program(
                zone_id,
                schedule_type="interval",
                interval_days=7,
                days=[],
            )
        )
        assert stored is not None
        monday = _next_monday()

        conflicts = ProgramRepository(test_db.db_path).check_program_conflicts(
            time="06:10",
            zones=[zone_id],
            days=[],
            schedule_type="interval",
            interval_days=7,
            candidate_interval_anchors={"06:10": monday + timedelta(days=1)},
            stored_interval_anchors={stored["id"]: {"06:00": monday}},
        )

        assert conflicts == []

    def test_matching_interval_anchors_report_exact_conflict(self, test_db):
        zone_id = _zone(test_db, name="Matching interval", duration=30)
        stored = test_db.create_program(
            _program(
                zone_id,
                schedule_type="interval",
                interval_days=7,
                days=[],
            )
        )
        assert stored is not None
        monday = _next_monday()

        conflicts = ProgramRepository(test_db.db_path).check_program_conflicts(
            time="06:10",
            zones=[zone_id],
            days=[],
            schedule_type="interval",
            interval_days=7,
            candidate_interval_anchors={"06:10": monday},
            stored_interval_anchors={stored["id"]: {"06:00": monday}},
        )

        assert [conflict["program_id"] for conflict in conflicts] == [stored["id"]]
        assert conflicts[0]["anchor_unknown"] is False

    def test_even_odd_candidate_conflicts_without_weekday_days(self, test_db):
        zone_id = _zone(test_db, name="Calendar", duration=30)
        stored = test_db.create_program(_program(zone_id, time="06:00", days=[0, 1, 2, 3, 4, 5, 6]))
        assert stored is not None

        conflicts = ProgramRepository(test_db.db_path).check_program_conflicts(
            time="06:10",
            zones=[zone_id],
            days=[],
            schedule_type="even-odd",
            even_odd="odd",
        )

        assert [conflict["program_id"] for conflict in conflicts] == [stored["id"]]

    def test_adjacent_weekdays_conflict_across_midnight(self, test_db):
        zone_id = _zone(test_db, name="Night", duration=30)
        stored = test_db.create_program(_program(zone_id, time="23:50", days=[0]))
        assert stored is not None

        conflicts = ProgramRepository(test_db.db_path).check_program_conflicts(
            time="00:05",
            zones=[zone_id],
            days=[1],
            schedule_type="weekdays",
        )

        assert [conflict["program_id"] for conflict in conflicts] == [stored["id"]]

    def test_candidate_primary_conflicts_with_own_extra_time(self, test_db):
        zone_id = _zone(test_db, name="Own slots", duration=30)

        conflicts = ProgramRepository(test_db.db_path).check_program_conflicts(
            time="06:00",
            extra_times=["06:10"],
            zones=[zone_id],
            days=[0],
            schedule_type="weekdays",
        )

        assert len(conflicts) == 1
        assert conflicts[0]["program_id"] is None
        assert conflicts[0]["candidate_self_conflict"] is True

    def test_weather_factor_expands_candidate_and_stored_windows(self, test_db):
        stored_zone = _zone(test_db, name="Stored", duration=10)
        candidate_zone = _zone(test_db, name="Candidate", duration=20)
        stored = test_db.create_program(_program(stored_zone, time="06:00"))
        assert stored is not None

        result = ProgramRepository(test_db.db_path).check_program_conflicts(
            time="05:30",
            zones=[candidate_zone],
            days=[0],
            weather_factor=200,
            schedule_type="weekdays",
        )

        assert result["has_conflicts"] is True
        assert result["conflicts"][0]["program_id"] == stored["id"]
        assert result["conflicts"][0]["level"] == "warning"
        assert result["conflicts"][0]["weather_factor"] == 200

    def test_weather_factor_expands_candidate_own_slots(self, test_db):
        zone_id = _zone(test_db, name="Own weather slots", duration=20)

        result = ProgramRepository(test_db.db_path).check_program_conflicts(
            time="06:00",
            extra_times=["06:30"],
            zones=[zone_id],
            days=[0],
            weather_factor=200,
            schedule_type="weekdays",
        )

        assert result["has_conflicts"] is True
        assert result["conflicts"][0]["candidate_self_conflict"] is True
        assert result["conflicts"][0]["level"] == "warning"
