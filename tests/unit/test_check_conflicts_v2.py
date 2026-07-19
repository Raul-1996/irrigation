"""Tests for check_program_conflicts v2 — TDD spec section 3.6.

Расширенная проверка конфликтов с учётом погодного коэффициента.

Контракт v2:
    check_program_conflicts(program_id, time_str, zone_ids, days,
                            weather_factor=None, include_weather=True) -> dict
    Returns:
        {
            "has_conflicts": bool,
            "conflicts": [{"program_id", "program_name", "level", "overlap_minutes",
                           "weather_factor", "group_id", "group_name", "message"}],
            "current_weather_coefficient": int,
        }
"""

import json
import os
import sqlite3

import pytest

from db.programs import ProgramRepository

os.environ["TESTING"] = "1"


# ---------------------------------------------------------------------------
# Helpers: создаём тестовую БД со всеми нужными таблицами и данными
# ---------------------------------------------------------------------------


def _create_test_db(tmp_path, groups=None, zones=None, programs=None, settings=None):
    """Создаёт SQLite БД с таблицами groups, zones, programs, settings.

    Returns db_path.
    """
    db_path = str(tmp_path / "test_conflicts.db")
    conn = sqlite3.connect(db_path)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS zones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL DEFAULT '',
            duration INTEGER NOT NULL DEFAULT 0,
            group_id INTEGER NOT NULL DEFAULT 0,
            topic TEXT DEFAULT '',
            mqtt_server_id INTEGER DEFAULT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS programs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL DEFAULT '',
            time TEXT NOT NULL DEFAULT '00:00',
            days TEXT NOT NULL DEFAULT '[]',
            zones TEXT NOT NULL DEFAULT '[]',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS weather_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            coefficient INTEGER
        )
    """)

    for g in groups or []:
        conn.execute("INSERT INTO groups (id, name) VALUES (?, ?)", (g["id"], g["name"]))

    for z in zones or []:
        conn.execute(
            "INSERT INTO zones (id, name, duration, group_id) VALUES (?, ?, ?, ?)",
            (z["id"], z.get("name", "zone_%d" % z["id"]), z["duration"], z["group_id"]),
        )

    for p in programs or []:
        conn.execute(
            "INSERT INTO programs (id, name, time, days, zones) VALUES (?, ?, ?, ?, ?)",
            (p["id"], p["name"], p["time"], json.dumps(p["days"]), json.dumps(p["zones"])),
        )

    for key, value in (settings or {}).items():
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))

    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# Базовая фикстура с двумя группами, зонами и одной программой
# ---------------------------------------------------------------------------


@pytest.fixture
def conflict_db(tmp_path):
    """БД с:
    - Группа 1 "Насос-1": зоны 1,2,3 (duration 15 мин каждая = 45 мин суммарно)
    - Группа 2 "Насос-2": зоны 10,11 (duration 20 мин каждая)
    - Программа A (id=1): 06:00, зоны [1,2,3], дни [0,2,4]
    """
    db_path = _create_test_db(
        tmp_path,
        groups=[
            {"id": 1, "name": "Насос-1"},
            {"id": 2, "name": "Насос-2"},
        ],
        zones=[
            {"id": 1, "name": "Газон", "duration": 15, "group_id": 1},
            {"id": 2, "name": "Клумба", "duration": 15, "group_id": 1},
            {"id": 3, "name": "Огород", "duration": 15, "group_id": 1},
            {"id": 10, "name": "Теплица", "duration": 20, "group_id": 2},
            {"id": 11, "name": "Сад", "duration": 20, "group_id": 2},
        ],
        programs=[
            {"id": 1, "name": "Утро", "time": "06:00", "days": [0, 2, 4], "zones": [1, 2, 3]},
        ],
        settings={
            "max_weather_coefficient": "200",
        },
    )
    return db_path


def _get_check_fn(db_path):
    """Return the required v2 conflict API without optional/xfail fallbacks."""
    return ProgramRepository(db_path).check_program_conflicts


class TestCheckConflictsV2:
    """check_program_conflicts v2 — 8 тестов по спеке 3.6."""

    # === Test 1: Конфликт при base durations → level=error ===

    def test_conflict_at_base_durations_error(self, conflict_db):
        """ProgA 06:00, 45 мин (base). ProgB 06:30, та же группа → level='error'."""
        check = _get_check_fn(conflict_db)

        # Проверяем конфликт для новой программы B: 06:30, зоны [1,2] (группа 1), дни [0,2,4]
        result = check(
            program_id=None,  # новая программа
            time="06:30",
            zones=[1, 2],
            days=[0, 2, 4],
            weather_factor=100,  # base
        )

        assert isinstance(result, dict)
        assert result["has_conflicts"] is True
        conflicts = result["conflicts"]
        assert len(conflicts) >= 1

        error_conflicts = [c for c in conflicts if c.get("level") == "error"]
        assert len(error_conflicts) >= 1, "Конфликт при base → level='error'"

        c = error_conflicts[0]
        assert c["program_id"] == 1
        assert c["overlap_minutes"] > 0

    # === Test 2: Конфликт только при weather 150% → level=warning ===

    def test_conflict_only_at_weather_150_warning(self, conflict_db):
        """ProgA 06:00, 45 мин base. ProgB 07:00. weather=150% → A до 07:07 → warning."""
        check = _get_check_fn(conflict_db)

        result = check(
            program_id=None,
            time="07:00",
            zones=[1, 2],
            days=[0, 2, 4],
            weather_factor=150,
        )

        assert result["has_conflicts"] is True
        assert result["conflicts"] == [
            {
                "program_id": 1,
                "program_name": "Утро",
                "level": "warning",
                "overlap_minutes": 7.5,
                "weather_factor": 150,
                "group_id": 1,
                "group_name": "Насос-1",
                "anchor_unknown": False,
                "message": 'Конфликт при погодном коэфф. 150% с программой "Утро"',
            }
        ]

    # === Test 3: Нет конфликта даже при 200% ===

    def test_no_conflict_even_at_200(self, tmp_path):
        """ProgA 06:00, 20 мин base (зоны с коротким duration). ProgB 07:00. 200% → 40 мин → до 06:40."""
        db_path = _create_test_db(
            tmp_path,
            groups=[{"id": 1, "name": "G1"}],
            zones=[
                {"id": 1, "name": "z1", "duration": 10, "group_id": 1},
                {"id": 2, "name": "z2", "duration": 10, "group_id": 1},
            ],
            programs=[
                {"id": 1, "name": "A", "time": "06:00", "days": [0, 1, 2, 3, 4], "zones": [1, 2]},
            ],
            settings={"max_weather_coefficient": "200"},
        )
        check = _get_check_fn(db_path)

        result = check(
            program_id=None,
            time="07:00",
            zones=[1, 2],
            days=[0, 1, 2, 3, 4],
            weather_factor=200,
        )

        assert result["has_conflicts"] is False, "Нет конфликта: 20 мин * 200% = 40 мин, до 06:40 < 07:00"
        assert len(result.get("conflicts", [])) == 0

    # === Test 4: Разные группы, одно время → нет конфликта ===

    def test_different_groups_no_conflict(self, conflict_db):
        """ProgA 06:00, гр.1. ProgB 06:00, гр.2 → нет конфликта (параллельно)."""
        check = _get_check_fn(conflict_db)

        result = check(
            program_id=None,
            time="06:00",
            zones=[10, 11],  # группа 2
            days=[0, 2, 4],
            weather_factor=100,
        )

        assert result["has_conflicts"] is False, "Разные группы работают параллельно — нет конфликта"

    # === Test 5: Одна группа, зазор 5 мин, base 60 → error при weather 120% ===

    def test_same_group_small_gap_conflict(self, tmp_path):
        """ProgA 06:00, 60 мин base, гр.1. ProgB 07:05. Зазор 5 мин.
        base: нет конфликта. weather=120% → 72 мин → 06:00-07:12 → overlap 7 мин → warning.
        """
        db_path = _create_test_db(
            tmp_path,
            groups=[{"id": 1, "name": "G1"}],
            zones=[
                {"id": 1, "name": "z1", "duration": 30, "group_id": 1},
                {"id": 2, "name": "z2", "duration": 30, "group_id": 1},
            ],
            programs=[
                {"id": 1, "name": "A", "time": "06:00", "days": [0], "zones": [1, 2]},
            ],
            settings={"max_weather_coefficient": "200"},
        )
        check = _get_check_fn(db_path)

        # При base (100%) нет конфликта: A заканчивает в 07:00, B стартует в 07:05
        result_base = check(
            program_id=None,
            time="07:05",
            zones=[1, 2],
            days=[0],
            weather_factor=100,
        )
        assert result_base["has_conflicts"] is False, "При base нет конфликта"

        # При weather=120%: 60*1.2=72 мин → A до 07:12 → overlap с B (07:05)
        result_weather = check(
            program_id=None,
            time="07:05",
            zones=[1, 2],
            days=[0],
            weather_factor=120,
        )
        assert result_weather["has_conflicts"] is True
        assert result_weather["conflicts"] == [
            {
                "program_id": 1,
                "program_name": "A",
                "level": "warning",
                "overlap_minutes": 7.0,
                "weather_factor": 120,
                "group_id": 1,
                "group_name": "G1",
                "anchor_unknown": False,
                "message": 'Конфликт при погодном коэфф. 120% с программой "A"',
            }
        ]

    # === Test 6: include_weather=True uses settings ===

    def test_include_weather_uses_settings(self, conflict_db):
        """include_weather=True → weather_factor берётся из settings.max_weather_coefficient."""
        check = _get_check_fn(conflict_db)

        # settings.max_weather_coefficient = 200 (установлено в фикстуре)
        result = check(
            program_id=None,
            time="07:00",
            zones=[1, 2],
            days=[0, 2, 4],
            include_weather=True,
        )

        assert result == {
            "has_conflicts": True,
            "conflicts": [
                {
                    "program_id": 1,
                    "program_name": "Утро",
                    "level": "warning",
                    "overlap_minutes": 30.0,
                    "weather_factor": 200,
                    "group_id": 1,
                    "group_name": "Насос-1",
                    "anchor_unknown": False,
                    "message": 'Конфликт при погодном коэфф. 200% с программой "Утро"',
                }
            ],
            "current_weather_coefficient": 100,
        }

    # === Test 7: Текущий коэффициент в ответе ===

    def test_current_coefficient_in_response(self, conflict_db):
        """Response содержит current_weather_coefficient."""
        check = _get_check_fn(conflict_db)

        with sqlite3.connect(conflict_db) as conn:
            conn.execute("INSERT INTO weather_decisions (coefficient) VALUES (120)")

        result = check(
            program_id=None,
            time="06:30",
            zones=[1, 2],
            days=[0, 2, 4],
            weather_factor=100,
        )

        assert result["current_weather_coefficient"] == 120

    # === Test 8: Пустой zones → нет конфликта ===

    def test_empty_zones_no_conflict(self, conflict_db):
        """zones=[] → has_conflicts=False, нет ошибок."""
        check = _get_check_fn(conflict_db)

        result = check(
            program_id=None,
            time="06:00",
            zones=[],
            days=[0, 2, 4],
            weather_factor=100,
        )

        assert result["has_conflicts"] is False
        assert len(result.get("conflicts", [])) == 0


class TestCheckConflictsCurrentBehavior:
    """Проверяем текущее поведение check_program_conflicts (без v2).

    Эти тесты работают с существующей реализацией и не требуют расширения.
    """

    def test_current_returns_list(self, conflict_db):
        """Текущая реализация возвращает list конфликтов."""
        repo = ProgramRepository(conflict_db)

        result = repo.check_program_conflicts(
            program_id=None,
            time="06:30",
            zones=[1, 2],
            days=[0, 2, 4],
        )

        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["program_id"] == 1
        assert result[0]["program_name"] == "Утро"

    def test_current_no_conflict_different_days(self, conflict_db):
        """Разные дни → нет конфликта."""
        repo = ProgramRepository(conflict_db)

        result = repo.check_program_conflicts(
            program_id=None,
            time="06:00",
            zones=[1, 2, 3],
            days=[1, 3, 5],  # Вт, Чт, Сб — не пересекается с [0, 2, 4]
        )

        assert result == []

    def test_current_empty_zones_no_error(self, conflict_db):
        """Пустые zones → нет crash."""
        repo = ProgramRepository(conflict_db)

        result = repo.check_program_conflicts(
            program_id=None,
            time="06:00",
            zones=[],
            days=[0, 2, 4],
        )

        assert result == []
