"""Issue #35: pure functions for irrigation history plan / actual calculation.

Plan = what WOULD have run per the current programs (no weather skip).
Actual = what actually ran (sum of zone_runs durations per day).

These helpers are deliberately stateless and import-free w.r.t. Flask/DB —
they take pre-loaded ``programs`` and ``runs`` lists, so they're trivial to
unit-test in isolation.

Domain rules (decisions Q1, Q2):
  - For ``schedule_type='interval'`` programs the in-the-past anchor is
    ``programs.created_at`` (decision Q1=a). Dates before that contribute 0.
  - In the GLOBAL view, zones without any active program contribute 0 to
    plan_minutes (not NULL); ``summary.has_plan = True`` if at least one
    zone in the selection has an active program (decision Q2).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def get_controller_timezone() -> tzinfo:
    """Return the timezone that owns controller-local persisted timestamps.

    Runtime scheduling gives ``WB_TZ`` precedence over ``TZ``.  History must
    use that same domain even when the process-local timezone differs (for
    example in tests, maintenance shells, or after a service-manager change).
    """
    candidates = [os.getenv("WB_TZ"), os.getenv("TZ")]
    try:
        with open("/etc/timezone", encoding="utf-8") as timezone_file:
            candidates.append(timezone_file.read().strip())
    except OSError:
        pass

    for name in candidates:
        if not name:
            continue
        try:
            return ZoneInfo(name.removeprefix(":"))
        except (ZoneInfoNotFoundError, ValueError):
            continue

    return datetime.now().astimezone().tzinfo or UTC


def parse_controller_datetime(raw: Any, controller_tz: tzinfo | None = None) -> datetime | None:
    """Parse a run timestamp into the explicit controller timezone.

    Production writers persist naive controller-local wall time despite the
    legacy ``*_utc`` column names.  Aware legacy/test values are accepted and
    converted to the same controller domain rather than interpreted through
    the caller process's implicit timezone.
    """
    if raw is None or raw == "":
        return None
    tz = controller_tz or get_controller_timezone()
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


# ---- Plan: which dates does a single program fire on? ----


def _coerce_days(raw) -> list[int]:
    """Programs store ``days`` as JSON of int 0..6 (Mon..Sun)."""
    if raw is None:
        return []
    if isinstance(raw, list):
        try:
            return [int(d) for d in raw]
        except (TypeError, ValueError):
            return []
    try:
        return [int(d) for d in json.loads(raw)]
    except (TypeError, ValueError):
        return []


def _coerce_zones(raw) -> list[int]:
    if raw is None:
        return []
    if isinstance(raw, list):
        try:
            return [int(z) for z in raw]
        except (TypeError, ValueError):
            return []
    try:
        return [int(z) for z in json.loads(raw)]
    except (TypeError, ValueError):
        return []


def _coerce_times(prog: dict) -> list[str]:
    """Return ['HH:MM', ...] from program.time + program.extra_times."""
    out: list[str] = []
    t = prog.get("time")
    if t:
        out.append(str(t))
    extra = prog.get("extra_times")
    if isinstance(extra, list):
        out.extend(str(t) for t in extra if t)
    elif extra:
        try:
            for t in json.loads(extra):
                if t:
                    out.append(str(t))
        except (TypeError, ValueError):
            pass
    return out


def _parse_created_at(raw) -> date | None:
    """SQLite CURRENT_TIMESTAMP is 'YYYY-MM-DD HH:MM:SS' (UTC)."""
    if not raw:
        return None
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00").replace(" ", "T")).date()
    except (TypeError, ValueError):
        return None


def _program_runs_on(prog: dict, d: date) -> bool:
    """Does ``prog`` fire on calendar date ``d``?

    Recognises ``schedule_type`` ∈ {'weekdays', 'even-odd', 'interval'}.
    Disabled programs (``enabled`` == 0/False) never fire.
    """
    if not bool(prog.get("enabled", 1)):
        return False
    sched = prog.get("schedule_type") or "weekdays"

    if sched == "weekdays":
        days = _coerce_days(prog.get("days"))
        return d.weekday() in days

    if sched == "even-odd":
        is_even = d.day % 2 == 0
        # Keep exact scheduler semantics: an absent key defaults to even,
        # while a persisted SQL NULL compares unequal to "even" and means odd.
        want_even = prog.get("even_odd", "even") == "even"
        return is_even if want_even else not is_even

    if sched == "interval":
        # Decision Q1=a: anchor = created_at. Dates before anchor contribute 0.
        anchor = _parse_created_at(prog.get("created_at"))
        try:
            n = int(prog.get("interval_days") or 0)
        except (TypeError, ValueError):
            n = 0
        if anchor is None or n <= 0 or d < anchor:
            return False
        return ((d - anchor).days % n) == 0

    return False


def _parse_program_time(raw: str) -> time | None:
    try:
        return time.fromisoformat(str(raw)).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _program_firings_count(prog: dict, d: date, as_of_local: datetime | None = None) -> int:
    """How many times does ``prog`` fire on ``d`` (main time + extra_times)?"""
    if not _program_runs_on(prog, d):
        return 0
    firing_times = [parsed for raw in _coerce_times(prog) if (parsed := _parse_program_time(raw)) is not None]
    if as_of_local is None:
        return len(firing_times)
    if d > as_of_local.date():
        return 0
    if d < as_of_local.date():
        return len(firing_times)
    cutoff = as_of_local.timetz().replace(tzinfo=None)
    return sum(firing_time <= cutoff for firing_time in firing_times)


# ---- Plan per zone / date ----


def calculate_plan_for_zone(
    zone_id: int,
    zone_duration: int,
    dates: Iterable[date],
    programs: Iterable[dict],
    *,
    as_of_local: datetime | None = None,
) -> dict[date, int]:
    """Return ``{date: planned_minutes}`` over ``dates``.

    ``planned_minutes`` for a date = sum over (programs containing zone) of
    (firing_count_on_date * zone_duration).

    Caller is responsible for filtering ``programs`` to the relevant set; we
    don't filter here. ``zone_duration`` is the zone's default duration in
    minutes (we do not yet support per-program zone_duration overrides).
    """
    rel_progs = [p for p in programs if int(zone_id) in _coerce_zones(p.get("zones"))]
    out: dict[date, int] = {}
    for d in dates:
        total = 0
        for prog in rel_progs:
            firings = _program_firings_count(prog, d, as_of_local)
            if firings:
                total += firings * int(zone_duration)
        out[d] = total
    return out


def zone_has_active_program(zone_id: int, programs: Iterable[dict]) -> bool:
    """``has_plan`` per zone: any enabled program that contains ``zone_id``."""
    for p in programs:
        if not bool(p.get("enabled", 1)):
            continue
        if int(zone_id) in _coerce_zones(p.get("zones")):
            return True
    return False


# ---- Actual per zone / date ----


def _run_duration_min(run: dict, controller_tz: tzinfo | None = None) -> int:
    """Best-effort duration in whole minutes from a zone_runs row.

    Uses ``end_utc - start_utc`` when both present, else 0 for an open row.
    Rounds to nearest minute (UI shows whole-minute granularity).
    """
    sdt = parse_controller_datetime(run.get("start_utc"), controller_tz)
    edt = parse_controller_datetime(run.get("end_utc"), controller_tz)
    if sdt is None or edt is None:
        return 0
    delta = (edt - sdt).total_seconds()
    if delta <= 0:
        return 0
    return round(delta / 60.0)


def _run_local_date(run: dict, controller_tz: tzinfo | None = None) -> date | None:
    sdt = parse_controller_datetime(run.get("start_utc"), controller_tz)
    return sdt.date() if sdt is not None else None


def run_counts_as_actual(run: dict) -> bool:
    """Whether a history row proves that water physically flowed.

    Legacy successful rows predate the ``confirmed`` column and remain valid
    by their persisted ``ok`` status.  A boot-aborted/unconfirmed attempt is
    still returned in the detailed list for diagnostics, but it must not
    inflate actual minutes, run count, liters, or claimed savings.
    """
    status = str(run.get("status") or "").lower()
    if status == "failed":
        return False
    if status != "aborted":
        return True
    confirmed = run.get("confirmed")
    return confirmed is True or confirmed == 1 or str(confirmed).lower() in {"1", "true", "yes"}


def calculate_actual_for_zone(
    runs: Iterable[dict],
    dates: Iterable[date],
    *,
    controller_tz: tzinfo | None = None,
) -> tuple[dict[date, int], dict[date, int]]:
    """Return ``({date: actual_minutes}, {date: runs_count})``.

    ``runs`` should be the pre-filtered list of zone_runs rows for the zone
    over the period of interest. Only rows with a valid start_utc date count.
    """
    date_set = set(dates)
    minutes: dict[date, int] = {d: 0 for d in date_set}
    counts: dict[date, int] = {d: 0 for d in date_set}
    for run in runs:
        if not run_counts_as_actual(run):
            continue
        d = _run_local_date(run, controller_tz)
        if d is None or d not in date_set:
            continue
        minutes[d] += _run_duration_min(run, controller_tz)
        counts[d] += 1
    return minutes, counts


# ---- Summary ----


def calculate_summary(
    actual_minutes_total: int,
    plan_minutes_total: int,
    has_plan: bool,
) -> dict[str, int]:
    """Compute ``saved_minutes`` and friends from totals.

    saved = plan - actual (positive ⇒ smart algorithm saved water;
    negative ⇒ manually watered more than baseline plan).
    """
    saved = int(plan_minutes_total) - int(actual_minutes_total) if has_plan else 0
    return {
        "plan_minutes": int(plan_minutes_total) if has_plan else 0,
        "saved_minutes": saved,
        "has_plan": bool(has_plan),
    }


# ---- Date-range helper ----


def date_range(today_local: date, days: int) -> list[date]:
    """Return ``[today - (days-1), ..., today]`` (inclusive, ascending)."""
    return [today_local - timedelta(days=days - 1 - i) for i in range(days)]
