"""Issue #35: unit tests for services.history_calc — pure functions."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import services.history_calc as history_calc
from services.history_calc import (
    _program_runs_on,
    calculate_actual_for_zone,
    calculate_plan_for_zone,
    calculate_summary,
    date_range,
    zone_has_active_program,
)

# Reference Monday 2026-05-04 (weekday 0).
MON = date(2026, 5, 4)
TUE = date(2026, 5, 5)
WED = date(2026, 5, 6)
SUN = date(2026, 5, 10)


@pytest.fixture(autouse=True)
def _isolate_history_timezone(monkeypatch):
    monkeypatch.setenv("WB_TZ", "UTC")
    monkeypatch.setenv("TZ", "UTC")


def _prog(**kw):
    """Build a program dict with sensible defaults."""
    base = {
        "id": 1,
        "name": "P",
        "time": "07:00",
        "extra_times": [],
        "days": [0],  # Mon
        "zones": [1],
        "schedule_type": "weekdays",
        "interval_days": None,
        "even_odd": None,
        "enabled": 1,
        "created_at": "2026-01-01 00:00:00",
    }
    base.update(kw)
    return base


class TestProgramRunsOn:
    def test_weekdays_match(self):
        assert _program_runs_on(_prog(days=[0, 2]), MON) is True
        assert _program_runs_on(_prog(days=[0, 2]), TUE) is False
        assert _program_runs_on(_prog(days=[0, 2]), WED) is True

    def test_disabled_never_fires(self):
        assert _program_runs_on(_prog(days=[0], enabled=0), MON) is False

    def test_even_odd_even(self):
        # 2026-05-04 is day 4 → even
        assert _program_runs_on(_prog(schedule_type="even-odd", even_odd="even"), MON) is True
        # 2026-05-05 day 5 → odd, even-odd='even' ⇒ no fire
        assert _program_runs_on(_prog(schedule_type="even-odd", even_odd="even"), TUE) is False

    def test_even_odd_odd(self):
        assert _program_runs_on(_prog(schedule_type="even-odd", even_odd="odd"), TUE) is True
        assert _program_runs_on(_prog(schedule_type="even-odd", even_odd="odd"), MON) is False

    def test_even_odd_null_matches_scheduler_odd_semantics(self):
        """A persisted NULL is distinct from an absent key in the scheduler."""
        assert _program_runs_on(_prog(schedule_type="even-odd", even_odd=None), TUE) is True
        assert _program_runs_on(_prog(schedule_type="even-odd", even_odd=None), MON) is False

    def test_interval_created_at_is_never_used_as_synthetic_anchor(self):
        p = _prog(
            schedule_type="interval",
            interval_days=3,
            created_at="2026-05-04 00:00:00",
        )
        assert _program_runs_on(p, date(2026, 5, 4)) is False
        assert _program_runs_on(p, date(2026, 5, 5)) is False


class TestCalculatePlan:
    def test_zone_with_one_weekdays_program(self):
        progs = [_prog(days=[0, 4], zones=[1])]  # Mon, Fri
        dates = [MON, TUE, WED, date(2026, 5, 8)]  # Mon, Tue, Wed, Fri
        plan = calculate_plan_for_zone(1, 15, dates, progs)
        assert plan[MON] == 15
        assert plan[TUE] == 0
        assert plan[WED] == 0
        assert plan[date(2026, 5, 8)] == 15

    def test_zone_with_two_programs(self):
        """Two programs that both fire on same day ⇒ minutes sum."""
        progs = [
            _prog(id=1, days=[0], zones=[1]),
            _prog(id=2, days=[0], zones=[1], time="18:00"),
        ]
        plan = calculate_plan_for_zone(1, 10, [MON], progs)
        assert plan[MON] == 20

    def test_zone_extra_times_doubles_minutes(self):
        progs = [_prog(days=[0], zones=[1], extra_times=["19:00"])]
        plan = calculate_plan_for_zone(1, 10, [MON], progs)
        # main 07:00 + extra 19:00 = 2 firings ⇒ 20 min
        assert plan[MON] == 20

    def test_future_slot_today_does_not_count_as_saved_water(self):
        progs = [_prog(days=[0], zones=[1], time="07:00", extra_times=["19:00"])]

        plan = calculate_plan_for_zone(
            1,
            10,
            [MON],
            progs,
            as_of_local=datetime(2026, 5, 4, 12, 0),
        )

        assert plan[MON] == 10

    def test_sequential_windows_are_closed_before_plan_becomes_eligible(self):
        p = _prog(time="23:50", days=[0], zones=[2, 1])
        durations = {1: 30, 2: 20}

        while_second_zone_open = calculate_plan_for_zone(
            2,
            20,
            [MON, TUE],
            [p],
            zone_durations=durations,
            as_of_local=datetime(2026, 5, 5, 0, 30, tzinfo=ZoneInfo("UTC")),
        )
        after_second_zone_closed = calculate_plan_for_zone(
            2,
            20,
            [MON, TUE],
            [p],
            zone_durations=durations,
            as_of_local=datetime(2026, 5, 5, 0, 41, tzinfo=ZoneInfo("UTC")),
        )

        # Scheduler sorts [2, 1] to [1, 2]: zone 2 starts Tuesday 00:20.
        assert while_second_zone_open == {MON: 0, TUE: 0}
        assert after_second_zone_closed == {MON: 0, TUE: 20}

    def test_cross_midnight_zone_window_is_split_between_local_dates(self):
        p = _prog(time="23:50", days=[0], zones=[1])

        plan = calculate_plan_for_zone(
            1,
            30,
            [MON, TUE],
            [p],
            zone_durations={1: 30},
            as_of_local=datetime(2026, 5, 5, 0, 21, tzinfo=ZoneInfo("UTC")),
        )

        assert plan == {MON: 10, TUE: 20}

    def test_interval_plan_requires_authoritative_occurrences(self):
        p = _prog(
            id=42,
            schedule_type="interval",
            interval_days=3,
            created_at="2026-05-04 00:00:00",
        )
        as_of = datetime(2026, 5, 4, 12, 0, tzinfo=ZoneInfo("UTC"))

        unavailable = calculate_plan_for_zone(
            1,
            15,
            [MON],
            [p],
            zone_durations={1: 15},
            interval_occurrences={},
            as_of_local=as_of,
        )
        authoritative = calculate_plan_for_zone(
            1,
            15,
            [MON],
            [p],
            zone_durations={1: 15},
            interval_occurrences={42: [datetime(2026, 5, 4, 7, 0, tzinfo=ZoneInfo("UTC"))]},
            as_of_local=as_of,
        )

        assert unavailable == {MON: 0}
        assert authoritative == {MON: 15}

    def test_zone_not_in_any_program(self):
        progs = [_prog(zones=[2])]  # zone 1 not in program
        plan = calculate_plan_for_zone(1, 10, [MON, TUE], progs)
        assert plan == {MON: 0, TUE: 0}

    def test_disabled_program_skipped(self):
        progs = [_prog(zones=[1], days=[0], enabled=0)]
        plan = calculate_plan_for_zone(1, 10, [MON], progs)
        assert plan[MON] == 0


class TestZoneHasActivePlan:
    def test_no_programs(self):
        assert zone_has_active_program(1, []) is False

    def test_enabled_program_for_zone(self):
        assert zone_has_active_program(1, [_prog(zones=[1])]) is True

    def test_disabled_program_doesnt_count(self):
        assert zone_has_active_program(1, [_prog(zones=[1], enabled=0)]) is False

    def test_program_for_other_zone(self):
        assert zone_has_active_program(1, [_prog(zones=[2])]) is False


class TestCalculateActual:
    def _run(self, start: str, end: str):
        return {"start_utc": start, "end_utc": end}

    def test_one_run_one_day(self):
        # Run from 2026-05-04 07:00 to 07:15 UTC (15 min)
        runs = [self._run("2026-05-04T07:00:00Z", "2026-05-04T07:15:00Z")]
        minutes, counts = calculate_actual_for_zone(
            runs,
            [MON, TUE],
            controller_tz=ZoneInfo("UTC"),
        )
        assert minutes[MON] == 15
        assert counts[MON] == 1

    def test_multiple_runs_same_day(self):
        runs = [
            self._run("2026-05-04T07:00:00Z", "2026-05-04T07:10:00Z"),
            self._run("2026-05-04T18:00:00Z", "2026-05-04T18:20:00Z"),
        ]
        minutes, counts = calculate_actual_for_zone(
            runs,
            [MON],
            controller_tz=ZoneInfo("UTC"),
        )
        assert minutes[MON] == 30
        assert counts[MON] == 2

    def test_cross_midnight_actual_minutes_split_by_controller_date(self):
        runs = [
            {
                "start_utc": "2026-05-04T23:50:00Z",
                "end_utc": "2026-05-05T00:20:00Z",
                "status": "ok",
            }
        ]

        minutes, counts = calculate_actual_for_zone(
            runs,
            [MON, TUE],
            controller_tz=ZoneInfo("UTC"),
        )

        assert minutes == {MON: 10, TUE: 20}
        assert counts == {MON: 1, TUE: 0}

    def test_unconfirmed_open_attempt_is_not_an_actual_run(self):
        runs = [
            {
                "start_utc": "2026-05-04T07:00:00Z",
                "end_utc": None,
                "status": None,
                "confirmed": 0,
            }
        ]
        minutes, counts = calculate_actual_for_zone(
            runs,
            [MON],
            controller_tz=ZoneInfo("UTC"),
        )
        assert minutes[MON] == 0
        assert counts[MON] == 0

    def test_elapsed_duration_uses_utc_timeline_across_dst_fold(self):
        controller_tz = ZoneInfo("America/New_York")
        runs = [
            {
                "start_utc": "2026-11-01T01:30:00-04:00",
                "end_utc": "2026-11-01T01:30:00-05:00",
                "status": "ok",
            }
        ]

        minutes, counts = calculate_actual_for_zone(
            runs,
            [date(2026, 11, 1)],
            controller_tz=controller_tz,
        )

        assert minutes[date(2026, 11, 1)] == 60
        assert counts[date(2026, 11, 1)] == 1

    def test_elapsed_duration_uses_utc_timeline_across_berlin_spring_forward(self):
        controller_tz = ZoneInfo("Europe/Berlin")
        runs = [
            {
                "start_utc": "2026-03-29T01:30:00+01:00",
                "end_utc": "2026-03-29T04:30:00+02:00",
                "status": "ok",
            }
        ]

        minutes, counts = calculate_actual_for_zone(
            runs,
            [date(2026, 3, 29)],
            controller_tz=controller_tz,
        )

        assert minutes[date(2026, 3, 29)] == 120
        assert counts[date(2026, 3, 29)] == 1

    def test_naive_fold_duration_uses_monotonic_timeline(self):
        controller_tz = ZoneInfo("America/New_York")
        runs = [
            {
                "start_utc": "2026-11-01 01:30:00",
                "end_utc": "2026-11-01 01:30:00",
                "start_monotonic": 100.0,
                "end_monotonic": 3700.0,
                "status": "ok",
            }
        ]

        minutes, counts = calculate_actual_for_zone(
            runs,
            [date(2026, 11, 1)],
            controller_tz=controller_tz,
        )

        assert minutes[date(2026, 11, 1)] == 60
        assert counts[date(2026, 11, 1)] == 1

    def test_unconfirmed_aborted_attempt_is_not_actual_watering(self):
        runs = [
            {
                "start_utc": "2026-05-04 07:00:00",
                "end_utc": "2026-05-04 07:10:00",
                "status": "aborted",
                "confirmed": 0,
            },
            {
                "start_utc": "2026-05-04 08:00:00",
                "end_utc": "2026-05-04 08:05:00",
                "status": "aborted",
                "confirmed": 1,
            },
        ]

        minutes, counts = calculate_actual_for_zone(runs, [MON], controller_tz=ZoneInfo("UTC"))

        assert minutes[MON] == 5
        assert counts[MON] == 1

    def test_naive_run_uses_explicit_controller_timezone(self):
        controller_tz = ZoneInfo("Asia/Bishkek")
        runs = [
            {
                "start_utc": "2026-05-04 00:05:00",
                "end_utc": "2026-05-04 00:20:00",
                "status": "ok",
            }
        ]

        minutes, counts = calculate_actual_for_zone(runs, [MON], controller_tz=controller_tz)

        assert minutes[MON] == 15
        assert counts[MON] == 1


class TestSummary:
    def test_saved_positive_when_actual_under_plan(self):
        s = calculate_summary(actual_minutes_total=100, plan_minutes_total=140, has_plan=True)
        assert s == {"plan_minutes": 140, "saved_minutes": 40, "has_plan": True}

    def test_saved_negative_when_actual_over_plan(self):
        s = calculate_summary(actual_minutes_total=200, plan_minutes_total=140, has_plan=True)
        assert s["saved_minutes"] == -60

    def test_zero_when_no_plan(self):
        s = calculate_summary(actual_minutes_total=50, plan_minutes_total=0, has_plan=False)
        assert s == {"plan_minutes": 0, "saved_minutes": 0, "has_plan": False}


class TestDateRange:
    def test_seven_days_inclusive(self):
        today = date(2026, 5, 11)
        rng = date_range(today, 7)
        assert rng[0] == date(2026, 5, 5)
        assert rng[-1] == date(2026, 5, 11)
        assert len(rng) == 7

    def test_thirty_days(self):
        today = date(2026, 5, 11)
        rng = date_range(today, 30)
        assert len(rng) == 30
        assert rng[-1] == today


def test_controller_timezone_localtime_fallback_keeps_dst_rules(monkeypatch):
    ny = ZoneInfo("America/New_York")
    monkeypatch.delenv("WB_TZ", raising=False)
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setattr(history_calc, "_read_etc_timezone_name", lambda: None)
    monkeypatch.setattr(history_calc, "_load_localtime_timezone", lambda: ny)

    fallback = history_calc.get_controller_timezone()

    assert datetime(2026, 1, 1, tzinfo=fallback).utcoffset() == timedelta(hours=-5)
    assert datetime(2026, 7, 1, tzinfo=fallback).utcoffset() == timedelta(hours=-4)
