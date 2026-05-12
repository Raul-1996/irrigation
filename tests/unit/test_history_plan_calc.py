"""Issue #35: unit tests for services.history_calc — pure functions."""

from datetime import date, datetime, timedelta

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

    def test_interval_anchor_at_created_at(self):
        """interval_days=3 with anchor 2026-05-04 ⇒ fires every 3 days from anchor."""
        p = _prog(
            schedule_type="interval",
            interval_days=3,
            created_at="2026-05-04 00:00:00",
        )
        assert _program_runs_on(p, date(2026, 5, 4)) is True
        assert _program_runs_on(p, date(2026, 5, 5)) is False
        assert _program_runs_on(p, date(2026, 5, 7)) is True
        assert _program_runs_on(p, date(2026, 5, 10)) is True
        # Before anchor — never fires (decision Q1=a).
        assert _program_runs_on(p, date(2026, 5, 3)) is False


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
        # Use the local date that 07:00 UTC falls into for cross-TZ safety:
        local_d = datetime.fromisoformat("2026-05-04T07:00:00+00:00").astimezone().date()
        minutes, counts = calculate_actual_for_zone(runs, [local_d, local_d + timedelta(days=1)])
        assert minutes[local_d] == 15
        assert counts[local_d] == 1

    def test_multiple_runs_same_day(self):
        runs = [
            self._run("2026-05-04T07:00:00Z", "2026-05-04T07:10:00Z"),
            self._run("2026-05-04T18:00:00Z", "2026-05-04T18:20:00Z"),
        ]
        local_d = datetime.fromisoformat("2026-05-04T07:00:00+00:00").astimezone().date()
        # Second run might roll over to next day in some TZs; pick both.
        local_d2 = datetime.fromisoformat("2026-05-04T18:00:00+00:00").astimezone().date()
        minutes, counts = calculate_actual_for_zone(runs, [local_d, local_d2])
        if local_d == local_d2:
            assert minutes[local_d] == 30
            assert counts[local_d] == 2
        else:
            assert minutes[local_d] == 10
            assert minutes[local_d2] == 20

    def test_open_run_contributes_zero(self):
        runs = [{"start_utc": "2026-05-04T07:00:00Z", "end_utc": None}]
        local_d = datetime.fromisoformat("2026-05-04T07:00:00+00:00").astimezone().date()
        minutes, counts = calculate_actual_for_zone(runs, [local_d])
        assert minutes[local_d] == 0
        # Still counts as a "run started today" — UI lists it.
        assert counts[local_d] == 1


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
