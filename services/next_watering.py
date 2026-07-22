"""Authoritative display-side projection of scheduled watering runs.

The calculator mirrors the scheduler contract instead of inventing a second
schedule:

* disabled programs and malformed start times create no candidates;
* every valid ``time``/``extra_times`` slot is considered;
* weekdays and even/odd dates follow the same calendar rules as APScheduler;
* interval dates come from the live APScheduler job's ``next_run_time`` (the
  interval anchor is process/job metadata and cannot be reconstructed from the
  program row after a restart);
* a run is considered in progress only while one of its zones is actually in a
  scheduler-owned active state; current H1 weather duration is used for the
  remaining offsets, while future-run offsets stay nominal because future
  weather is unknowable;
* weather skip, per-zone postpone, and per-group cancellation are applied to
  the program start that the scheduler would evaluate.

Used by both next-watering endpoints and by group ``next_start`` in
``/api/status``.
"""

import json
import logging
import math
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from database import db
from services.helpers import parse_dt

logger = logging.getLogger(__name__)

_INTERVAL_JOB_RE = re.compile(r"^program:(\d+):(main|extra:\d+)$")
_ACTIVE_SCHEDULE_STATES = {"starting", "on", "stopping"}
MAX_NEXT_WATERING_ZONE_IDS = 512


class NextWateringLimitError(ValueError):
    """An explicit projection request exceeds the controller work budget."""


def normalize_requested_zone_ids(
    zone_ids: list[int] | tuple[int, ...],
    *,
    enforce_limit: bool = True,
) -> list[int]:
    """Validate strict positive IDs and deduplicate them in request order."""
    if not isinstance(zone_ids, (list, tuple)):
        raise TypeError("zone_ids must be a list of integers")
    if enforce_limit and len(zone_ids) > MAX_NEXT_WATERING_ZONE_IDS:
        raise NextWateringLimitError("too many zone ids")
    result: list[int] = []
    seen: set[int] = set()
    for zone_id in zone_ids:
        if type(zone_id) is not int or zone_id <= 0:
            raise ValueError("zone ids must be positive canonical integers")
        if zone_id not in seen:
            seen.add(zone_id)
            result.append(zone_id)
    return result


def weather_skip_today() -> bool:
    """Return a fresh-cache-only weather skip decision for the display path.

    ``/api/status`` is polled frequently, so this path must never fetch the
    network.  If the cache is absent/stale, return ``False`` rather than let an
    old forecast hide a run that the live-capable scheduler may execute.
    """
    try:
        from services.weather_adjustment import get_weather_adjustment

        adj = get_weather_adjustment(db.db_path)
        if not adj.is_enabled():
            return False
        return bool(adj.should_skip(cache_only=True).get("skip"))
    except (ImportError, OSError, sqlite3.Error, ValueError, TypeError) as e:
        logger.debug("Weather-skip check failed in next-watering: %s", e)
        return False


def _parse_time(value: Any) -> tuple[int, int] | None:
    """Parse exactly the hour/minute values accepted by scheduler jobs."""
    try:
        parts = str(value).split(":")
        if len(parts) != 2:
            return None
        hour, minute = map(int, parts)
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            return None
        return hour, minute
    except (ValueError, TypeError):
        return None


def _program_start_slots(prog: dict[str, Any]) -> list[tuple[str, int, int]]:
    """Return valid ``(scheduler suffix, hour, minute)`` start slots."""
    raw_extra = prog.get("extra_times") or []
    if isinstance(raw_extra, str):
        try:
            raw_extra = json.loads(raw_extra)
        except (json.JSONDecodeError, TypeError):
            raw_extra = []
    if not isinstance(raw_extra, (list, tuple)):
        raw_extra = []

    raw_slots = [("main", prog.get("time"))]
    raw_slots.extend((f"extra:{idx}", value) for idx, value in enumerate(raw_extra))
    slots = []
    for suffix, value in raw_slots:
        parsed = _parse_time(value)
        if parsed is None:
            logger.debug("Ignoring malformed program time program=%s slot=%s value=%r", prog.get("id"), suffix, value)
            continue
        slots.append((suffix, parsed[0], parsed[1]))
    return slots


def program_runs_on(prog: dict[str, Any], d: datetime) -> bool:
    """Whether a calendar-based program fires on ``d``.

    Interval programs deliberately return ``False`` here: their day is defined
    by the live APScheduler anchor, consumed separately through
    :func:`_get_interval_next_runs`.
    """
    if not prog.get("enabled", True):
        return False
    schedule_type = prog.get("schedule_type") or "weekdays"
    if schedule_type == "interval":
        return False
    if schedule_type == "even-odd":
        want_even = prog.get("even_odd", "even") == "even"
        return (d.day % 2 == 0) == want_even
    return d.weekday() in (prog.get("days") or [])


def _local_naive(value: datetime) -> datetime:
    """Use scheduler wall-clock time in the calculator's naive-local domain."""
    if value.tzinfo is not None:
        return value.replace(tzinfo=None)
    return value


def _get_interval_next_runs(programs: list[dict[str, Any]]) -> dict[tuple[int, str], datetime]:
    """Read all interval ``next_run_time`` values from APScheduler once."""
    interval_ids = {
        int(p["id"])
        for p in programs
        if p.get("id") is not None and (p.get("schedule_type") or "weekdays") == "interval"
    }
    if not interval_ids:
        return {}
    try:
        from irrigation_scheduler import get_scheduler

        wrapper = get_scheduler()
        scheduler = getattr(wrapper, "scheduler", None) if wrapper is not None else None
        if scheduler is None:
            return {}
        result: dict[tuple[int, str], datetime] = {}
        for job in scheduler.get_jobs():
            match = _INTERVAL_JOB_RE.match(str(getattr(job, "id", "")))
            if not match:
                continue
            program_id = int(match.group(1))
            if program_id not in interval_ids:
                continue
            next_run = getattr(job, "next_run_time", None)
            if isinstance(next_run, datetime):
                result[(program_id, match.group(2))] = _local_naive(next_run)
        return result
    except (ImportError, OSError, RuntimeError, ValueError, TypeError, AttributeError) as e:
        logger.debug("Interval scheduler metadata unavailable: %s", e)
        return {}


def _weather_duration_coefficient() -> int:
    """Return the current legacy (H1) coefficient without network access.

    The scheduler currently applies ``WeatherAdjustment.get_coefficient()``;
    it does not enable the optional H2 balance coefficient. Missing or invalid
    display-only cache data is neutral (100%): it must not erase an otherwise
    visible active run that the live-capable scheduler may still execute.
    """
    try:
        from services.weather.adjustment import WeatherAdjustment
        from services.weather.service import WeatherService

        adjustment = WeatherAdjustment(db.db_path)
        if not adjustment.is_enabled():
            return 100
        weather = WeatherService(db.db_path).get_weather(cache_only=True)
        if weather is None:
            return 100
        effective_weather = adjustment._select_input_source(weather)
        decision = adjustment.should_skip(cache_only=True, weather=effective_weather)
        if decision.get("details", {}).get("type") == "weather_unavailable":
            return 100
        return max(0, min(200, int(adjustment.get_coefficient(weather=effective_weather))))
    except (ImportError, OSError, sqlite3.Error, ValueError, TypeError, AttributeError) as e:
        logger.debug("Weather coefficient unavailable in next-watering: %s", e)
        return 100


def _interval_step(prog: dict[str, Any]) -> timedelta | None:
    try:
        days = int(prog.get("interval_days"))
    except (ValueError, TypeError):
        return None
    return timedelta(days=days) if days > 0 else None


def _first_calendar_start_after(prog: dict[str, Any], hour: int, minute: int, lower_bound: datetime) -> datetime | None:
    for offset in range(0, 15):
        day = lower_bound + timedelta(days=offset)
        if not program_runs_on(prog, day):
            continue
        candidate = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate > lower_bound:
            return candidate
    return None


def _latest_calendar_start_not_after(
    prog: dict[str, Any], hour: int, minute: int, upper_bound: datetime
) -> datetime | None:
    best = None
    for offset in range(-14, 1):
        day = upper_bound + timedelta(days=offset)
        if not program_runs_on(prog, day):
            continue
        candidate = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= upper_bound and (best is None or candidate > best):
            best = candidate
    return best


def first_program_start_after(
    prog: dict[str, Any],
    lower_bound: datetime,
    *,
    suffix: str = "main",
    hour: int | None = None,
    minute: int | None = None,
    interval_next_runs: dict[tuple[int, str], datetime] | None = None,
) -> datetime | None:
    """Return the first scheduler-backed program start strictly after a bound."""
    if not prog or not prog.get("enabled", True):
        return None
    if hour is None or minute is None:
        parsed = _parse_time(prog.get("time"))
        if parsed is None:
            return None
        hour, minute = parsed

    if (prog.get("schedule_type") or "weekdays") != "interval":
        return _first_calendar_start_after(prog, hour, minute, lower_bound)

    step = _interval_step(prog)
    next_run = (interval_next_runs or {}).get((int(prog.get("id")), suffix))
    if step is None or next_run is None:
        return None
    if next_run > lower_bound:
        return next_run
    elapsed_steps = math.floor((lower_bound - next_run) / step) + 1
    return next_run + step * elapsed_steps


def _latest_program_start_not_after(
    prog: dict[str, Any],
    upper_bound: datetime,
    *,
    suffix: str,
    hour: int,
    minute: int,
    interval_next_runs: dict[tuple[int, str], datetime],
) -> datetime | None:
    if (prog.get("schedule_type") or "weekdays") != "interval":
        return _latest_calendar_start_not_after(prog, hour, minute, upper_bound)
    step = _interval_step(prog)
    next_run = interval_next_runs.get((int(prog.get("id")), suffix))
    if step is None or next_run is None:
        return None
    if next_run <= upper_bound:
        elapsed_steps = math.floor((upper_bound - next_run) / step)
        return next_run + step * elapsed_steps
    rewind_steps = math.ceil((next_run - upper_bound) / step)
    return next_run - step * rewind_steps


def _adjusted_duration(base_duration: int, coefficient: int) -> int:
    adjusted = round(base_duration * coefficient / 100.0)
    return 0 if coefficient == 0 else max(1, adjusted)


def compute_next_watering(
    zone_ids=None,
    *,
    all_zones: list | None = None,
    programs: list | None = None,
    skip_today: bool | None = None,
    enforce_limit: bool = True,
) -> dict[int, dict]:
    """Calculate the nearest real scheduler candidate for each requested zone.

    Public/request-derived explicit ID lists keep the default work bound.
    Trusted internal snapshots that already bound their database read may pass
    ``enforce_limit=False`` (for example the all-zones status projection).
    """
    if all_zones is None:
        all_zones = db.get_zones() or []
    if programs is None:
        programs = db.get_programs() or []
    explicit_zone_ids = zone_ids is not None
    if zone_ids is None:
        zone_ids = [int(z.get("id")) for z in all_zones if int(z.get("group_id") or z.get("group") or 0) != 999]
    zone_ids = normalize_requested_zone_ids(
        zone_ids,
        enforce_limit=explicit_zone_ids and enforce_limit,
    )
    zone_by_id = {int(z["id"]): z for z in all_zones}
    base_duration = {int(z["id"]): int(z.get("duration") or 0) for z in all_zones}

    active_schedule_zones = {
        int(z["id"])
        for z in all_zones
        if str(z.get("state") or "").lower() in _ACTIVE_SCHEDULE_STATES
        and str(z.get("watering_start_source") or "").lower() == "schedule"
    }
    coefficient = _weather_duration_coefficient() if active_schedule_zones else 100

    program_maps = []
    for program in programs:
        if not program.get("enabled", True):
            continue
        slots = _program_start_slots(program)
        if not slots:
            continue
        try:
            zones_sorted = sorted(int(value) for value in (program.get("zones") or []))
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Ignoring malformed program zones program=%s: %s", program.get("id"), e)
            continue
        if not zones_sorted:
            continue

        base_offsets: dict[int, int] = {}
        active_offsets: dict[int, int] = {}
        base_total = 0
        active_total = 0
        for zone_id in zones_sorted:
            base_offsets[zone_id] = base_total
            active_offsets[zone_id] = active_total
            duration = base_duration.get(zone_id, 0)
            base_total += duration
            active_total += _adjusted_duration(duration, coefficient)
        program_maps.append(
            {
                "prog": program,
                "slots": slots,
                "zones_sorted": zones_sorted,
                "base_offsets": base_offsets,
                "active_offsets": active_offsets,
                "base_total": base_total,
                "active_total": active_total,
                "is_active": bool(active_schedule_zones.intersection(zones_sorted)),
            }
        )

    actual_now = datetime.now()
    if skip_today is None:
        skip_today = weather_skip_today()
    interval_next_runs = _get_interval_next_runs([item["prog"] for item in program_maps])

    cancellation_cache: dict[tuple[int, str, int], bool] = {}

    def is_cancelled(program: dict[str, Any], start: datetime, group_id: int) -> bool:
        if not group_id or program.get("id") is None:
            return False
        key = (int(program["id"]), start.strftime("%Y-%m-%d"), int(group_id))
        if key not in cancellation_cache:
            try:
                cancellation_cache[key] = bool(db.is_program_run_cancelled_for_group(*key))
            except (sqlite3.Error, OSError) as e:
                logger.debug("Program cancellation lookup failed for %s: %s", key, e)
                cancellation_cache[key] = False
        return cancellation_cache[key]

    result: dict[int, dict] = {}
    for zone_id in zone_ids:
        zone = zone_by_id.get(zone_id)
        group_id = int(zone.get("group_id") or 0) if zone else 0
        zone_lower_bound = actual_now
        postpone = parse_dt(zone.get("postpone_until")) if zone else None
        if postpone and postpone > zone_lower_bound:
            zone_lower_bound = postpone

        best_dt = None
        best_map = None
        for item in program_maps:
            program = item["prog"]
            if zone_id not in item["base_offsets"]:
                continue

            # A nominal in-progress window is not evidence that a program is
            # still executing.  Require a scheduler-owned active zone, then use
            # the current H1 coefficient for this run's remaining offsets.
            if item["is_active"] and item["active_total"] > 0:
                for suffix, hour, minute in item["slots"]:
                    start = _latest_program_start_not_after(
                        program,
                        actual_now,
                        suffix=suffix,
                        hour=hour,
                        minute=minute,
                        interval_next_runs=interval_next_runs,
                    )
                    if start is None or actual_now >= start + timedelta(minutes=item["active_total"]):
                        continue
                    candidate = start + timedelta(minutes=item["active_offsets"][zone_id])
                    if candidate <= zone_lower_bound or is_cancelled(program, start, group_id):
                        continue
                    if best_dt is None or candidate < best_dt:
                        best_dt = candidate
                        best_map = item

            # Future weather is intentionally not projected: the scheduler
            # decides it at run time, so use stable nominal offsets here.
            offset = item["base_offsets"][zone_id]
            for suffix, hour, minute in item["slots"]:
                # Starts at/before ``actual_now`` belong to the current run and
                # are considered only by the evidence-backed active branch
                # above.  Without an active scheduler-owned zone, reviving a
                # passed nominal start creates phantom late-zone slots after a
                # weather-shortened run has already finished.
                start_bound = max(actual_now, zone_lower_bound - timedelta(minutes=offset))
                for _attempt in range(32):
                    start = first_program_start_after(
                        program,
                        start_bound,
                        suffix=suffix,
                        hour=hour,
                        minute=minute,
                        interval_next_runs=interval_next_runs,
                    )
                    if start is None:
                        break
                    candidate = start + timedelta(minutes=offset)
                    if skip_today and start.date() == actual_now.date():
                        start_bound = start
                        continue
                    if is_cancelled(program, start, group_id):
                        start_bound = start
                        continue
                    if candidate <= zone_lower_bound:
                        start_bound = start
                        continue
                    if best_dt is None or candidate < best_dt:
                        best_dt = candidate
                        best_map = item
                    break

        zones_sorted = best_map["zones_sorted"] if best_map else []
        result[zone_id] = {
            "next_dt": best_dt,
            "program": best_map["prog"] if best_map else None,
            "zone_position": zones_sorted.index(zone_id) + 1 if zone_id in zones_sorted else None,
            "total_zones": len(zones_sorted) if best_map else None,
            "has_programs": any(zone_id in item["base_offsets"] for item in program_maps),
        }
    return result
