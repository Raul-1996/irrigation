"""Phase 2 regressions for program persistence and conflict validation."""

import sqlite3

import pytest

from db.programs import ProgramRepository


@pytest.fixture(autouse=True)
def _live_program_zone(test_db):
    zone = test_db.create_zone({"name": "Fixture zone", "duration": 15, "group_id": 1})
    assert zone is not None
    assert zone["id"] == 1


def _create_zone(test_db, *, group_id: int = 1, duration: int = 15) -> int:
    zone = test_db.create_zone(
        {
            "name": f"Zone {group_id}",
            "duration": duration,
            "group_id": group_id,
        }
    )
    assert zone is not None
    return zone["id"]


def _program_data(**overrides):
    data = {
        "name": "Program",
        "time": "06:00",
        "days": [0],
        "zones": [1],
    }
    data.update(overrides)
    return data


def test_create_program_preserves_zero_based_weekdays_without_monday(test_db):
    program = test_db.create_program(_program_data(days=[1, 2, 5, 6]))

    assert program is not None
    assert program["days"] == [1, 2, 5, 6]


def test_update_program_preserves_zero_based_weekdays_without_monday(test_db):
    program = test_db.create_program(_program_data(days=[0]))
    assert program is not None

    updated = test_db.update_program(program["id"], {"days": [2, 4, 6]})

    assert updated is not None
    assert updated["days"] == [2, 4, 6]


def test_duplicate_program_preserves_stored_zero_based_weekdays(test_db):
    with sqlite3.connect(test_db.db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO programs (name, time, days, zones) VALUES (?, ?, ?, ?)",
            ("Stored", "06:00", "[1, 3, 5]", "[1]"),
        )
        program_id = cursor.lastrowid

    duplicate = test_db.duplicate_program(program_id)

    assert duplicate is not None
    assert duplicate["days"] == [1, 3, 5]


@pytest.mark.parametrize("invalid_field", ["zones", "extra_times"])
def test_create_program_rejects_json_null_list_fields(test_db, invalid_field):
    result = test_db.create_program(_program_data(**{invalid_field: None}))

    assert result is None
    assert test_db.get_programs() == []


@pytest.mark.parametrize("invalid_field", ["zones", "extra_times"])
def test_update_program_rejects_json_null_list_fields_atomically(test_db, invalid_field):
    program = test_db.create_program(_program_data(extra_times=["18:00"]))
    assert program is not None

    result = test_db.update_program(program["id"], {"name": "Poisoned", invalid_field: None})

    assert result is None
    stored = test_db.get_program(program["id"])
    assert stored is not None
    assert stored["name"] == "Program"
    assert stored["zones"] == [1]
    assert stored["extra_times"] == ["18:00"]


def test_conflict_check_ignores_disabled_programs(test_db):
    zone_id = _create_zone(test_db)
    disabled = test_db.create_program(_program_data(name="Disabled", zones=[zone_id], enabled=False))
    assert disabled is not None

    conflicts = test_db.check_program_conflicts(
        time="06:05",
        zones=[zone_id],
        days=[0],
    )

    assert conflicts == []


def test_conflict_check_includes_stored_extra_times_for_group_999(test_db):
    zone_id = _create_zone(test_db, group_id=999)
    existing = test_db.create_program(
        _program_data(
            name="No-water slot",
            time="06:00",
            zones=[zone_id],
            extra_times=["20:00"],
        )
    )
    assert existing is not None

    conflicts = test_db.check_program_conflicts(
        time="20:05",
        zones=[zone_id],
        days=[0],
    )

    assert [conflict["program_id"] for conflict in conflicts] == [existing["id"]]
    assert conflicts[0]["program_time"] == "20:00"
    assert conflicts[0]["common_groups"] == [999]


def test_conflict_check_accepts_candidate_extra_times(test_db):
    zone_id = _create_zone(test_db)
    existing = test_db.create_program(_program_data(name="Morning", time="06:00", zones=[zone_id]))
    assert existing is not None

    conflicts = ProgramRepository(test_db.db_path).check_program_conflicts(
        time="21:00",
        zones=[zone_id],
        days=[0],
        extra_times=["06:05"],
    )

    assert [conflict["program_id"] for conflict in conflicts] == [existing["id"]]
    assert conflicts[0]["program_time"] == "06:00"
