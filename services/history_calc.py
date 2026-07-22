"""Issue #35: pure functions for irrigation history plan / actual calculation.

Plan = what WOULD have run per the current programs (no weather skip).
Actual = what actually ran (sum of zone_runs durations per day).

These helpers are deliberately stateless and import-free w.r.t. Flask/DB —
they take pre-loaded ``programs`` and ``runs`` lists, so they're trivial to
unit-test in isolation.

Domain rules (decisions Q1, Q2):
  - Interval programs are projected only from authoritative live scheduler
    occurrences. A database ``created_at`` value is never an interval anchor.
  - In the GLOBAL view, zones without any active program contribute 0 to
    plan_minutes (not NULL); ``summary.has_plan = True`` if at least one
    zone in the selection has an active program (decision Q2).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _read_etc_timezone_name() -> str | None:
    try:
        with open("/etc/timezone", encoding="utf-8") as timezone_file:
            return timezone_file.read().strip() or None
    except OSError:
        return None


def _load_localtime_timezone() -> tzinfo | None:
    """Load /etc/localtime as a rules-aware zone rather than a fixed offset."""
    try:
        with open("/etc/localtime", "rb") as localtime_file:
            return ZoneInfo.from_file(localtime_file, key="localtime")
    except (OSError, ValueError):
        return None


def get_controller_timezone() -> tzinfo:
    """Return the timezone that owns controller-local persisted timestamps.

    Runtime scheduling gives ``WB_TZ`` precedence over ``TZ``.  History must
    use that same domain even when the process-local timezone differs (for
    example in tests, maintenance shells, or after a service-manager change).
    """
    candidates = [os.getenv("WB_TZ"), os.getenv("TZ"), _read_etc_timezone_name()]

    for name in candidates:
        if not name:
            continue
        try:
            return ZoneInfo(name.removeprefix(":"))
        except (ZoneInfoNotFoundError, ValueError):
            continue

    localtime_tz = _load_localtime_timezone()
    if localtime_tz is not None:
        return localtime_tz
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
        # The live APScheduler trigger owns this anchor. Callers inject its
        # occurrences into calculate_plan_for_zone; DB created_at is unrelated.
        return False

    return False


def _parse_program_time(raw: str) -> time | None:
    try:
        return time.fromisoformat(str(raw)).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


# ---- Plan per zone / date ----


def _controller_aware(value: datetime, controller_tz: tzinfo) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=controller_tz)
    return value.astimezone(controller_tz)


def _timeline_add(value: datetime, minutes: int, controller_tz: tzinfo) -> datetime:
    """Add real elapsed minutes, preserving controller-local display rules."""
    return (value.astimezone(UTC) + timedelta(minutes=int(minutes))).astimezone(controller_tz)


def _split_window_minutes(
    start_local: datetime,
    end_local: datetime,
    controller_tz: tzinfo,
    date_set: set[date],
) -> dict[date, int]:
    """Split one real-time window at controller-local calendar midnights."""
    result: dict[date, int] = {}
    cursor_utc = start_local.astimezone(UTC)
    end_utc = end_local.astimezone(UTC)
    while cursor_utc < end_utc:
        cursor_local = cursor_utc.astimezone(controller_tz)
        boundary_utc = None
        # Some rule sets skip a local calendar date. Find the first midnight
        # whose UTC instant is actually after the cursor.
        for day_offset in range(1, 4):
            boundary_local = datetime.combine(
                cursor_local.date() + timedelta(days=day_offset),
                time.min,
                tzinfo=controller_tz,
            )
            candidate = boundary_local.astimezone(UTC)
            if candidate > cursor_utc:
                boundary_utc = candidate
                break
        segment_end = min(end_utc, boundary_utc or end_utc)
        if cursor_local.date() in date_set:
            minutes = round((segment_end - cursor_utc).total_seconds() / 60.0)
            result[cursor_local.date()] = result.get(cursor_local.date(), 0) + minutes
        cursor_utc = segment_end
    return result


def _program_zone_window(
    prog: dict,
    zone_id: int,
    zone_duration: int,
    zone_durations: Mapping[int, int] | None,
) -> tuple[int, int] | None:
    """Return nominal ``(offset, duration)`` matching scheduler zone order."""
    zones = sorted(set(_coerce_zones(prog.get("zones"))))
    if int(zone_id) not in zones:
        return None
    duration_lookup: dict[int, int] = {}
    if zone_durations is not None:
        for raw_id, raw_duration in zone_durations.items():
            try:
                duration_lookup[int(raw_id)] = max(0, int(raw_duration))
            except (TypeError, ValueError):
                continue
    duration_lookup.setdefault(int(zone_id), max(0, int(zone_duration)))
    position = zones.index(int(zone_id))
    preceding = zones[:position]
    if any(preceding_id not in duration_lookup for preceding_id in preceding):
        return None
    return sum(duration_lookup[preceding_id] for preceding_id in preceding), duration_lookup[int(zone_id)]


def zone_plan_is_available(
    zone_id: int,
    zone_duration: int,
    programs: Iterable[dict],
    *,
    zone_durations: Mapping[int, int] | None = None,
    interval_occurrences: Mapping[int, Iterable[datetime]] | None = None,
) -> bool:
    """Whether every active plan component has enough authoritative data."""
    occurrence_map = interval_occurrences or {}
    for prog in programs:
        if not bool(prog.get("enabled", 1)) or int(zone_id) not in _coerce_zones(prog.get("zones")):
            continue
        if _program_zone_window(prog, zone_id, zone_duration, zone_durations) is None:
            return False
        schedule_type = prog.get("schedule_type") or "weekdays"
        if schedule_type not in {"weekdays", "even-odd", "interval"}:
            return False
        if schedule_type == "interval":
            try:
                program_id = int(prog["id"])
            except (KeyError, TypeError, ValueError):
                return False
            if program_id not in occurrence_map:
                return False
    return True


def calculate_plan_for_zone(
    zone_id: int,
    zone_duration: int,
    dates: Iterable[date],
    programs: Iterable[dict],
    *,
    zone_durations: Mapping[int, int] | None = None,
    interval_occurrences: Mapping[int, Iterable[datetime]] | None = None,
    as_of_local: datetime | None = None,
) -> dict[date, int]:
    """Return completed nominal zone-window minutes by local calendar date."""
    dates_list = list(dates)
    if not dates_list:
        return {}
    date_set = set(dates_list)
    out: dict[date, int] = {d: 0 for d in dates_list}
    controller_tz = (
        as_of_local.tzinfo if as_of_local is not None and as_of_local.tzinfo is not None else get_controller_timezone()
    )
    as_of = _controller_aware(as_of_local, controller_tz) if as_of_local is not None else None
    occurrence_map = interval_occurrences or {}

    for prog in programs:
        if not bool(prog.get("enabled", 1)):
            continue
        window = _program_zone_window(prog, zone_id, zone_duration, zone_durations)
        if window is None:
            continue
        offset_minutes, duration_minutes = window
        if duration_minutes <= 0:
            continue

        schedule_type = prog.get("schedule_type") or "weekdays"
        occurrences: list[datetime] = []
        if schedule_type == "interval":
            try:
                raw_occurrences = occurrence_map.get(int(prog["id"]), [])
            except (KeyError, TypeError, ValueError):
                raw_occurrences = []
            for raw_occurrence in raw_occurrences:
                if isinstance(raw_occurrence, datetime):
                    occurrences.append(_controller_aware(raw_occurrence, controller_tz))
        elif schedule_type in {"weekdays", "even-odd"}:
            lookback_days = (offset_minutes + duration_minutes) // (24 * 60) + 1
            source_date = min(date_set) - timedelta(days=lookback_days)
            last_source_date = max(date_set)
            while source_date <= last_source_date:
                if _program_runs_on(prog, source_date):
                    for raw_time in _coerce_times(prog):
                        parsed_time = _parse_program_time(raw_time)
                        if parsed_time is not None:
                            occurrences.append(datetime.combine(source_date, parsed_time, tzinfo=controller_tz))
                source_date += timedelta(days=1)

        for occurrence in occurrences:
            zone_start = _timeline_add(occurrence, offset_minutes, controller_tz)
            zone_end = _timeline_add(zone_start, duration_minutes, controller_tz)
            # An open or future nominal window is not yet evidence of savings.
            if as_of is not None and zone_end.astimezone(UTC) > as_of.astimezone(UTC):
                continue
            for bucket_date, minutes in _split_window_minutes(zone_start, zone_end, controller_tz, date_set).items():
                out[bucket_date] += minutes
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


def _monotonic_duration_seconds(run: dict) -> float | None:
    try:
        delta = float(run.get("end_monotonic")) - float(run.get("start_monotonic"))
    except (TypeError, ValueError):
        return None
    return delta if delta > 0 else None


def run_duration_minutes(run: dict, controller_tz: tzinfo | None = None) -> int:
    """Best-effort duration in whole minutes from a zone_runs row.

    Prefer the persisted monotonic interval, which remains valid through wall
    clock corrections and DST folds. Otherwise subtract parsed timestamps on
    the UTC timeline. Open rows have duration 0. The UI reports whole minutes.
    """
    monotonic_delta = _monotonic_duration_seconds(run)
    if monotonic_delta is not None:
        return round(monotonic_delta / 60.0)

    sdt = parse_controller_datetime(run.get("start_utc"), controller_tz)
    edt = parse_controller_datetime(run.get("end_utc"), controller_tz)
    if sdt is None or edt is None:
        return 0
    delta = (edt.astimezone(UTC) - sdt.astimezone(UTC)).total_seconds()
    if delta <= 0:
        return 0
    return round(delta / 60.0)


def _run_local_date(run: dict, controller_tz: tzinfo | None = None) -> date | None:
    sdt = parse_controller_datetime(run.get("start_utc"), controller_tz)
    return sdt.date() if sdt is not None else None


def run_was_confirmed(run: dict) -> bool:
    """Normalize SQLite/JSON confirmation representations to one boolean."""
    confirmed = run.get("confirmed")
    return confirmed is True or confirmed == 1 or str(confirmed).lower() in {"1", "true", "yes"}


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
    if run.get("end_utc") in (None, ""):
        return run_was_confirmed(run)
    if status != "aborted":
        return True
    return run_was_confirmed(run)


def calculate_actual_for_zone(
    runs: Iterable[dict],
    dates: Iterable[date],
    *,
    controller_tz: tzinfo | None = None,
) -> tuple[dict[date, int], dict[date, int]]:
    """Return ``({date: actual_minutes}, {date: runs_count})``.

    ``runs`` may overlap either edge of the period. Counts belong to the run's
    controller-local start date; elapsed minutes are split at local midnight.
    """
    tz = controller_tz or get_controller_timezone()
    date_set = set(dates)
    minutes: dict[date, int] = {d: 0 for d in date_set}
    counts: dict[date, int] = {d: 0 for d in date_set}
    for run in runs:
        if not run_counts_as_actual(run):
            continue
        d = _run_local_date(run, tz)
        if d is None:
            continue
        if d in date_set:
            counts[d] += 1
        start_local = parse_controller_datetime(run.get("start_utc"), tz)
        if start_local is None:
            continue
        monotonic_delta = _monotonic_duration_seconds(run)
        if monotonic_delta is not None:
            end_local = (start_local.astimezone(UTC) + timedelta(seconds=monotonic_delta)).astimezone(tz)
        else:
            end_local = parse_controller_datetime(run.get("end_utc"), tz)
        if end_local is None or end_local.astimezone(UTC) <= start_local.astimezone(UTC):
            continue
        for bucket_date, elapsed in _split_window_minutes(
            start_local,
            end_local,
            tz,
            date_set,
        ).items():
            minutes[bucket_date] += elapsed
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
