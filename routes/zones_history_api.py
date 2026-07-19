"""Issue #35: zone watering history API.

Three endpoints, all GET-only:
  - GET /api/zones/<id>/history?days=7      — per-zone JSON
  - GET /api/zones/history?days=7           — global JSON (filters: group_id, zone_id)
  - GET /api/zones/<id>/history.csv?days=7  — per-zone CSV download

Access (decision Q4): guest allowed — the deployment perimeter is closed by
nginx basic-auth / CF Worker, so the API itself doesn't gate on session role.

``days`` is whitelisted to {7, 30} (anything else returns 400).
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import sqlite3
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any

from flask import Blueprint, Response, jsonify, request

from database import db
from services.history_calc import (
    calculate_actual_for_zone,
    calculate_plan_for_zone,
    calculate_summary,
    date_range,
    get_controller_timezone,
    parse_controller_datetime,
    run_counts_as_actual,
    run_duration_minutes,
    run_was_confirmed,
    zone_has_active_program,
    zone_plan_is_available,
)

zones_history_api_bp = Blueprint("zones_history_api", __name__)

ALLOWED_DAYS = {7, 30}
CSV_FORMULA_SIGILS = frozenset("=+-@")
CSV_FORMULA_LEADING_CHARS = "".join(chr(codepoint) for codepoint in range(0x21))


# ---- helpers ----


def _parse_days() -> int | None:
    raw = request.args.get("days", "7")
    try:
        d = int(raw)
    except (TypeError, ValueError):
        return None
    if d not in ALLOWED_DAYS:
        return None
    return d


def _controller_now(controller_tz: tzinfo) -> datetime:
    return datetime.now(controller_tz)


def _history_clock() -> tuple[tzinfo, datetime]:
    controller_tz = get_controller_timezone()
    now_local = _controller_now(controller_tz)
    if now_local.tzinfo is None:
        now_local = now_local.replace(tzinfo=controller_tz)
    else:
        now_local = now_local.astimezone(controller_tz)
    return controller_tz, now_local


def _iso_local(d: date) -> str:
    return d.isoformat()


def _csv_safe_cell(value: Any) -> Any:
    """Prevent spreadsheet applications from executing persisted text."""
    if isinstance(value, str):
        normalized = value.lstrip(CSV_FORMULA_LEADING_CHARS)
        if normalized and normalized[0] in CSV_FORMULA_SIGILS:
            return f"'{value}"
    return value


def _sqlite_utc_datetime(raw: Any) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00")) if raw else None
    except (TypeError, ValueError):
        return None
    if value is not None and value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC) if value is not None else None


def _raw_run_datetime(raw: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")) if raw else None
    except (TypeError, ValueError):
        return None


def _ambiguous_local_candidates(parsed: datetime, controller_tz: tzinfo) -> tuple[datetime, datetime] | None:
    if parsed.tzinfo is not None:
        return None
    fold_zero = parsed.replace(tzinfo=controller_tz, fold=0)
    fold_one = parsed.replace(tzinfo=controller_tz, fold=1)
    if fold_zero.utcoffset() == fold_one.utcoffset():
        return None
    return fold_zero, fold_one


def _run_start_local(run: dict[str, Any], controller_tz: tzinfo) -> datetime | None:
    """Resolve an ambiguous naive start with its UTC row-creation instant."""
    parsed = _raw_run_datetime(run.get("start_utc"))
    if parsed is None:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(controller_tz)
    candidates = _ambiguous_local_candidates(parsed, controller_tz)
    created_at = _sqlite_utc_datetime(run.get("created_at"))
    if candidates is None or created_at is None:
        return parsed.replace(tzinfo=controller_tz)
    return min(
        candidates,
        key=lambda candidate: abs((candidate.astimezone(UTC) - created_at).total_seconds()),
    )


def _run_end_local(
    run: dict[str, Any],
    controller_tz: tzinfo,
    start_local: datetime | None,
) -> datetime | None:
    parsed = _raw_run_datetime(run.get("end_utc"))
    if parsed is None:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(controller_tz)
    candidates = _ambiguous_local_candidates(parsed, controller_tz)
    if candidates is None:
        return parsed.replace(tzinfo=controller_tz)

    target_utc = None
    if start_local is not None:
        try:
            monotonic_delta = float(run.get("end_monotonic")) - float(run.get("start_monotonic"))
        except (TypeError, ValueError):
            monotonic_delta = 0.0
        if monotonic_delta > 0:
            target_utc = start_local.astimezone(UTC) + timedelta(seconds=monotonic_delta)
    if target_utc is not None:
        return min(
            candidates,
            key=lambda candidate: abs((candidate.astimezone(UTC) - target_utc).total_seconds()),
        )
    if start_local is not None:
        later_candidates = [
            candidate for candidate in candidates if candidate.astimezone(UTC) > start_local.astimezone(UTC)
        ]
        if later_candidates:
            return min(later_candidates, key=lambda candidate: candidate.astimezone(UTC))
    return candidates[0]


def _run_sort_instant(start_local: datetime) -> datetime:
    """Sort by the same unambiguous instant exposed by serialization."""
    return start_local.astimezone(UTC)


def _fetch_runs(
    from_local: date,
    to_local: date,
    controller_tz: tzinfo,
    *,
    zone_ids: list[int] | None = None,
    group_id: int | None = None,
    exclude_group_id: int | None = None,
) -> list[dict[str, Any]]:
    """Return runs in one explicit controller-local calendar window.

    ``zone_runs`` uses the controller-local naive timestamp convention of its
    production writers, but legacy/test rows can be RFC 3339. SQL therefore
    narrows to a two-day-padded candidate window; Python performs the exact
    timezone-aware local-date check before any bucket, list, or CSV consumes it.
    """
    if zone_ids is not None and not zone_ids:
        return []
    # Two days covers the maximum calendar-date shift between valid UTC
    # offsets (+14:00 to -12:00) while retaining a bounded candidate query.
    padded_from = from_local - timedelta(days=2)
    padded_to_exclusive = to_local + timedelta(days=3)
    start_text = datetime.combine(padded_from, datetime.min.time()).strftime("%Y-%m-%d %H:%M:%S")
    end_text = datetime.combine(padded_to_exclusive, datetime.min.time()).strftime("%Y-%m-%d %H:%M:%S")

    clauses = ["start_utc IS NOT NULL", "start_utc >= ?", "start_utc < ?"]
    params: list[Any] = [start_text, end_text]
    if zone_ids is not None:
        placeholders = ",".join("?" * len(zone_ids))
        clauses.append(f"zone_id IN ({placeholders})")
        params.extend(int(zone_id) for zone_id in zone_ids)
    if group_id is not None:
        clauses.append("group_id = ?")
        params.append(int(group_id))
    if exclude_group_id is not None:
        clauses.append("group_id != ?")
        params.append(int(exclude_group_id))
    sql = (
        "SELECT id, zone_id, group_id, start_utc, end_utc, start_monotonic, "
        "end_monotonic, total_liters, status, source, confirmed, created_at "
        "FROM zone_runs WHERE " + " AND ".join(clauses)
    )
    try:
        with sqlite3.connect(db.db_path, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
        dated_rows = []
        range_start = datetime.combine(from_local, datetime.min.time(), tzinfo=controller_tz).astimezone(UTC)
        range_end = datetime.combine(
            to_local + timedelta(days=1),
            datetime.min.time(),
            tzinfo=controller_tz,
        ).astimezone(UTC)
        for row in rows:
            run = dict(row)
            start_local = _run_start_local(run, controller_tz)
            end_local = _run_end_local(run, controller_tz, start_local)
            overlaps = (
                start_local is not None
                and start_local.astimezone(UTC) < range_end
                and (end_local is None or end_local.astimezone(UTC) > range_start)
            )
            if overlaps:
                # Resolve legacy controller-local naive folds exactly once.
                # Every downstream duration/bucket/serializer then consumes
                # these same aware UTC instants instead of reparsing fold=0.
                run["start_utc"] = start_local.astimezone(UTC).isoformat()
                if end_local is not None:
                    run["end_utc"] = end_local.astimezone(UTC).isoformat()
                dated_rows.append((_run_sort_instant(start_local), run))
        dated_rows.sort(key=lambda item: item[0], reverse=True)
        return [run for _, run in dated_rows]
    except sqlite3.Error:
        return []


def _build_daily(
    dates: list[date],
    actual_min: dict[date, int],
    runs_count: dict[date, int],
    plan_min: dict[date, int],
    has_plan: bool,
    plan_available: bool = True,
) -> list[dict[str, Any]]:
    out = []
    for d in dates:
        item = {
            "date": _iso_local(d),
            "actual_minutes": int(actual_min.get(d, 0)),
            "runs": int(runs_count.get(d, 0)),
            "plan_minutes": int(plan_min.get(d, 0)) if has_plan and plan_available else None,
        }
        out.append(item)
    return out


def _coerce_program_zone_ids(program: dict[str, Any]) -> list[int]:
    raw = program.get("zones") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
    try:
        return sorted(set(int(zone_id) for zone_id in raw))
    except (TypeError, ValueError):
        return []


def _expected_interval_slots(program: dict[str, Any]) -> set[str]:
    raw_extra = program.get("extra_times") or []
    if isinstance(raw_extra, str):
        try:
            raw_extra = json.loads(raw_extra)
        except (json.JSONDecodeError, TypeError):
            raw_extra = []
    if not isinstance(raw_extra, list):
        raw_extra = []

    slots: set[str] = set()
    for suffix, value in [("main", program.get("time")), *[(f"extra:{i}", v) for i, v in enumerate(raw_extra)]]:
        try:
            parts = str(value).split(":")
            hour, minute = map(int, parts) if len(parts) == 2 else (-1, -1)
        except (TypeError, ValueError):
            continue
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            slots.add(suffix)
    return slots


def _load_interval_occurrences(
    programs: list[dict[str, Any]],
    start_local: datetime,
    end_local: datetime,
    controller_tz: tzinfo,
) -> dict[int, list[datetime]]:
    """Read interval fires from live scheduler metadata, failing closed."""
    interval_programs = [
        program
        for program in programs
        if bool(program.get("enabled", 1)) and (program.get("schedule_type") or "weekdays") == "interval"
    ]
    if not interval_programs:
        return {}
    try:
        from irrigation_scheduler import get_scheduler

        scheduler = get_scheduler()
        anchors_getter = getattr(scheduler, "get_program_interval_anchors", None)
        occurrences_getter = getattr(scheduler, "get_program_occurrences", None)
        if not callable(anchors_getter) or not callable(occurrences_getter):
            return {}
    except Exception:
        # History is informational. A missing/unhealthy scheduler backend must
        # make its interval plan unavailable, never fail the HTTP request.
        return {}

    result: dict[int, list[datetime]] = {}
    for program in interval_programs:
        try:
            program_id = int(program["id"])
            expected_slots = _expected_interval_slots(program)
            if not expected_slots:
                continue
            anchors = anchors_getter(program_id)
            if not expected_slots.issubset(set(anchors)):
                continue
            by_slot = occurrences_getter(program_id, start_local, end_local, limit=512)
            occurrences: list[datetime] = []
            for slot in expected_slots:
                for raw_occurrence in by_slot.get(slot, []):
                    occurrence = parse_controller_datetime(raw_occurrence, controller_tz)
                    if occurrence is not None:
                        # Python compares two datetimes that share one ZoneInfo
                        # by wall time, so fold=0 and fold=1 can compare equal.
                        # Normalize before both deduplication and ordering to
                        # preserve the two real instants in a repeated hour.
                        occurrences.append(occurrence.astimezone(UTC))
            result[program_id] = sorted(set(occurrences))
        except Exception:
            # APScheduler job stores may surface sqlite3, SQLAlchemy, or
            # backend-specific errors. Absence of trustworthy metadata is an
            # unavailable plan, not permission to invent an anchor.
            continue
    return result


def _interval_query_start(
    from_local: date,
    programs: list[dict[str, Any]],
    zone_durations: dict[int, int],
    controller_tz: tzinfo,
) -> datetime:
    max_window_minutes = 0
    for program in programs:
        zones = _coerce_program_zone_ids(program)
        max_window_minutes = max(
            max_window_minutes,
            sum(max(0, int(zone_durations.get(zone_id, 0))) for zone_id in zones),
        )
    range_start = datetime.combine(from_local, datetime.min.time(), tzinfo=controller_tz)
    return (range_start.astimezone(UTC) - timedelta(minutes=max_window_minutes)).astimezone(controller_tz)


def _cohort_matches_current(runs: list[dict[str, Any]], current_zone_ids: set[int]) -> bool:
    return all(not run.get("zone_deleted") and int(run.get("zone_id") or 0) in current_zone_ids for run in runs)


def _cohort_matches_group_filter(
    selected_zone_runs: list[dict[str, Any]],
    *,
    group_id: int | None = None,
    exclude_group_id: int | None = None,
) -> bool:
    for run in selected_zone_runs:
        run_group_id = int(run.get("group_id") or 0)
        if group_id is not None and run_group_id != int(group_id):
            return False
        if exclude_group_id is not None and run_group_id == int(exclude_group_id):
            return False
    return True


def _actuals_are_complete(runs: list[dict[str, Any]], controller_tz: tzinfo) -> bool:
    for run in runs:
        if not run_counts_as_actual(run):
            continue
        start_local = _run_start_local(run, controller_tz)
        end_local = _run_end_local(run, controller_tz, start_local)
        if start_local is None or end_local is None or end_local.astimezone(UTC) <= start_local.astimezone(UTC):
            return False
    return True


def _apply_summary_availability(
    summary: dict[str, Any],
    *,
    has_plan: bool,
    plan_available: bool,
    cohort_matches_current: bool,
    actuals_complete: bool,
) -> None:
    savings_available = bool(has_plan and plan_available and cohort_matches_current and actuals_complete)
    summary["plan_available"] = bool(plan_available)
    summary["cohort_matches_current"] = bool(cohort_matches_current)
    summary["actuals_complete"] = bool(actuals_complete)
    summary["savings_available"] = savings_available
    summary["savings_unavailable_reason"] = None
    if has_plan and not plan_available:
        summary["plan_minutes"] = None
        summary["saved_minutes"] = None
        summary["savings_unavailable_reason"] = "plan_unavailable"
    elif has_plan and not cohort_matches_current:
        summary["saved_minutes"] = None
        summary["savings_unavailable_reason"] = "historical_zone_cohort_changed"
    elif has_plan and not actuals_complete:
        summary["saved_minutes"] = None
        summary["savings_unavailable_reason"] = "actual_run_open"


def _zone_identity_for_run(
    run: dict[str, Any],
    zone_lookup: dict[int, dict[str, Any]],
    controller_tz: tzinfo,
) -> tuple[dict[str, Any], bool]:
    """Resolve a run without assigning a deleted predecessor to a reused ID."""
    zone = zone_lookup.get(int(run.get("zone_id") or 0))
    if not zone:
        return {}, True

    zone_created = _sqlite_utc_datetime(zone.get("created_at"))
    run_created = _sqlite_utc_datetime(run.get("created_at"))
    if zone_created is not None and run_created is not None:
        if run_created < zone_created:
            return {}, True
        if run_created > zone_created:
            return zone, False

    # SQLite timestamps have one-second precision. Resolve equal timestamps
    # with the physical start invariant: a run cannot predate its own zone.
    start_local = _run_start_local(run, controller_tz)
    if start_local is not None and zone_created is not None and start_local.astimezone(UTC) < zone_created:
        return {}, True
    return zone, False


def _current_identity_runs(
    runs: list[dict[str, Any]],
    zone_lookup: dict[int, dict[str, Any]],
    controller_tz: tzinfo,
) -> list[dict[str, Any]]:
    """Exclude deleted/legacy predecessors from current-zone aggregates."""
    return [run for run in runs if not _zone_identity_for_run(run, zone_lookup, controller_tz)[1]]


def _serialize_run(
    run: dict[str, Any],
    zone_lookup: dict[int, dict[str, Any]],
    controller_tz: tzinfo,
) -> dict[str, Any]:
    zone, zone_deleted = _zone_identity_for_run(run, zone_lookup, controller_tz)
    start_local = _run_start_local(run, controller_tz)
    end_local = _run_end_local(run, controller_tz, start_local)
    return {
        "id": int(run.get("id")) if run.get("id") is not None else None,
        "zone_id": int(run.get("zone_id") or 0),
        "zone_name": "Удалённая зона" if zone_deleted else zone.get("name"),
        "zone_deleted": zone_deleted,
        "group_id": int(run.get("group_id") or 0),
        "start_utc": start_local.isoformat(timespec="seconds") if start_local is not None else None,
        "end_utc": end_local.isoformat(timespec="seconds") if end_local is not None else None,
        "duration_min": run_duration_minutes(run, controller_tz),
        "liters": run.get("total_liters"),
        "status": run.get("status"),
        "source": run.get("source"),
        "confirmed": run_was_confirmed(run),
        "counts_as_actual": run_counts_as_actual(run),
    }


def _aggregate_liters(runs: list[dict[str, Any]]) -> tuple[float | None, bool, bool]:
    """Return (total_liters_or_none, partial_flag, any_data_flag).

    - any_data_flag = at least one run has a non-NULL total_liters
    - partial_flag = some rows have liters, some don't
    - total = sum of available liters or None when no data
    """
    has_any = False
    has_missing = False
    total = 0.0
    for r in runs:
        v = r.get("total_liters")
        if v is None:
            has_missing = True
        else:
            has_any = True
            with contextlib.suppress(TypeError, ValueError):
                total += float(v)
    if not has_any:
        return None, False, False
    return total, has_missing, True


# ---- per-zone JSON ----


@zones_history_api_bp.route("/api/zones/<int:zone_id>/history", methods=["GET"])
def get_zone_history(zone_id: int):
    days = _parse_days()
    if days is None:
        return jsonify({"success": False, "message": "days must be one of 7, 30"}), 400
    zone = db.get_zone(zone_id)
    if not zone:
        return jsonify({"success": False, "message": "zone not found"}), 404

    controller_tz, now_local = _history_clock()
    today = now_local.date()
    dates = date_range(today, days)
    from_d, to_d = dates[0], dates[-1]

    all_zones = db.get_zones() or []
    if not any(int(item.get("id") or 0) == int(zone_id) for item in all_zones):
        all_zones.append(zone)
    zone_durations = {int(item["id"]): int(item.get("duration") or 0) for item in all_zones}
    programs = db.get_programs() or []
    interval_occurrences = _load_interval_occurrences(
        programs,
        _interval_query_start(from_d, programs, zone_durations, controller_tz),
        now_local,
        controller_tz,
    )
    has_plan = zone_has_active_program(zone_id, programs)
    plan_available = zone_plan_is_available(
        zone_id,
        int(zone.get("duration") or 0),
        programs,
        zone_durations=zone_durations,
        interval_occurrences=interval_occurrences,
    )
    plan_by_date = calculate_plan_for_zone(
        zone_id,
        int(zone.get("duration") or 0),
        dates,
        programs,
        zone_durations=zone_durations,
        interval_occurrences=interval_occurrences,
        as_of_local=now_local,
    )

    raw_runs = _fetch_runs(from_d, to_d, controller_tz, zone_ids=[zone_id])
    zone_lookup = {int(zone["id"]): zone}
    current_identity_runs = _current_identity_runs(raw_runs, zone_lookup, controller_tz)
    actual_min, runs_count = calculate_actual_for_zone(current_identity_runs, dates, controller_tz=controller_tz)

    daily = _build_daily(dates, actual_min, runs_count, plan_by_date, has_plan, plan_available)

    runs_out = [_serialize_run(r, zone_lookup, controller_tz) for r in raw_runs]
    cohort_matches_current = _cohort_matches_current(runs_out, {int(zone_id)})
    actuals_complete = _actuals_are_complete(current_identity_runs, controller_tz)

    total_actual = sum(actual_min.values())
    total_plan = sum(plan_by_date.values()) if has_plan and plan_available else 0
    summary = calculate_summary(total_actual, total_plan, has_plan)
    _apply_summary_availability(
        summary,
        has_plan=has_plan,
        plan_available=plan_available,
        cohort_matches_current=cohort_matches_current,
        actuals_complete=actuals_complete,
    )
    total_liters, liters_partial, has_liters = _aggregate_liters(
        [r for r in current_identity_runs if run_counts_as_actual(r)]
    )
    summary.update(
        {
            "total_minutes": int(total_actual),
            "total_runs": int(sum(runs_count.values())),
            "total_liters": total_liters,
            "liters_partial": bool(liters_partial),
            "has_liters": bool(has_liters),
        }
    )

    return jsonify(
        {
            "success": True,
            "zone": {
                "id": int(zone["id"]),
                "name": zone.get("name"),
                "duration": int(zone.get("duration") or 0),
                "group_id": int(zone.get("group_id") or 0),
            },
            "period": {
                "from": _iso_local(from_d),
                "to": _iso_local(to_d),
                "days": days,
            },
            "summary": summary,
            "daily": daily,
            "runs": runs_out,
        }
    )


# ---- global JSON ----


@zones_history_api_bp.route("/api/zones/history", methods=["GET"])
def get_global_history():
    days = _parse_days()
    if days is None:
        return jsonify({"success": False, "message": "days must be one of 7, 30"}), 400

    group_id_raw = request.args.get("group_id")
    zone_id_raw = request.args.get("zone_id")

    all_zones = db.get_zones() or []
    # Drop the special "no irrigation" group from defaults.
    zones = [z for z in all_zones if int(z.get("group_id") or 0) != 999]

    zone_filter_id: int | None = None
    group_filter_id: int | None = None
    if zone_id_raw is not None:
        try:
            zone_filter_id = int(zone_id_raw)
        except (TypeError, ValueError):
            return jsonify({"success": False, "message": "invalid zone_id"}), 400
        zones = [z for z in zones if int(z["id"]) == zone_filter_id]
    elif group_id_raw is not None:
        try:
            group_filter_id = int(group_id_raw)
        except (TypeError, ValueError):
            return jsonify({"success": False, "message": "invalid group_id"}), 400
        zones = [z for z in zones if int(z.get("group_id") or 0) == group_filter_id]

    controller_tz, now_local = _history_clock()
    today = now_local.date()
    dates = date_range(today, days)
    from_d, to_d = dates[0], dates[-1]

    programs = db.get_programs() or []
    zone_durations = {int(zone["id"]): int(zone.get("duration") or 0) for zone in all_zones}
    interval_occurrences = _load_interval_occurrences(
        programs,
        _interval_query_start(from_d, programs, zone_durations, controller_tz),
        now_local,
        controller_tz,
    )

    # Per-zone plan, summed across the selection.
    plan_agg: dict[date, int] = {d: 0 for d in dates}
    has_plan_any = False
    plan_available_all = True
    for z in zones:
        zid = int(z["id"])
        z_has_plan = zone_has_active_program(zid, programs)
        if z_has_plan:
            has_plan_any = True
            if not zone_plan_is_available(
                zid,
                int(z.get("duration") or 0),
                programs,
                zone_durations=zone_durations,
                interval_occurrences=interval_occurrences,
            ):
                plan_available_all = False
        # Decision Q2: zones without programs contribute 0 (not NULL).
        # We use the calc helper either way — it returns {date: 0} when no match.
        zplan = calculate_plan_for_zone(
            zid,
            int(z.get("duration") or 0),
            dates,
            programs,
            zone_durations=zone_durations,
            interval_occurrences=interval_occurrences,
            as_of_local=now_local,
        )
        for d in dates:
            plan_agg[d] += int(zplan.get(d, 0))

    current_zone_ids = {int(zone["id"]) for zone in zones}
    selected_zone_runs: list[dict[str, Any]] = []
    if zone_filter_id is not None:
        raw_runs = _fetch_runs(from_d, to_d, controller_tz, zone_ids=[zone_filter_id])
    elif group_filter_id is not None:
        raw_runs = [] if group_filter_id == 999 else _fetch_runs(from_d, to_d, controller_tz, group_id=group_filter_id)
        selected_zone_runs = _fetch_runs(
            from_d,
            to_d,
            controller_tz,
            zone_ids=sorted(current_zone_ids),
        )
    else:
        raw_runs = _fetch_runs(from_d, to_d, controller_tz, exclude_group_id=999)
        selected_zone_runs = _fetch_runs(
            from_d,
            to_d,
            controller_tz,
            zone_ids=sorted(current_zone_ids),
        )

    zone_lookup = {int(z["id"]): z for z in all_zones}
    current_identity_runs = _current_identity_runs(raw_runs, zone_lookup, controller_tz)
    actual_min, runs_count = calculate_actual_for_zone(current_identity_runs, dates, controller_tz=controller_tz)

    daily = _build_daily(
        dates,
        actual_min,
        runs_count,
        plan_agg,
        has_plan_any,
        plan_available_all,
    )

    runs_out = [_serialize_run(r, zone_lookup, controller_tz) for r in raw_runs]
    cohort_matches_current = _cohort_matches_current(runs_out, current_zone_ids)
    if group_filter_id is not None and group_filter_id != 999:
        cohort_matches_current = cohort_matches_current and _cohort_matches_group_filter(
            selected_zone_runs,
            group_id=group_filter_id,
        )
    elif zone_filter_id is None and group_filter_id is None:
        cohort_matches_current = cohort_matches_current and _cohort_matches_group_filter(
            selected_zone_runs,
            exclude_group_id=999,
        )
    actuals_complete = _actuals_are_complete(current_identity_runs, controller_tz)

    total_actual = sum(actual_min.values())
    total_plan = sum(plan_agg.values()) if has_plan_any and plan_available_all else 0
    summary = calculate_summary(total_actual, total_plan, has_plan_any)
    _apply_summary_availability(
        summary,
        has_plan=has_plan_any,
        plan_available=plan_available_all,
        cohort_matches_current=cohort_matches_current,
        actuals_complete=actuals_complete,
    )
    total_liters, liters_partial, has_liters = _aggregate_liters(
        [r for r in current_identity_runs if run_counts_as_actual(r)]
    )
    summary.update(
        {
            "total_minutes": int(total_actual),
            "total_runs": int(sum(runs_count.values())),
            "total_liters": total_liters,
            "liters_partial": bool(liters_partial),
            "has_liters": bool(has_liters),
        }
    )

    return jsonify(
        {
            "success": True,
            "period": {
                "from": _iso_local(from_d),
                "to": _iso_local(to_d),
                "days": days,
            },
            "filters": {
                "group_id": group_filter_id,
                "zone_id": zone_filter_id,
            },
            "zone_count": len(zones),
            "summary": summary,
            "daily": daily,
            "runs": runs_out,
        }
    )


# ---- CSV ----


@zones_history_api_bp.route("/api/zones/<int:zone_id>/history.csv", methods=["GET"])
def get_zone_history_csv(zone_id: int):
    days = _parse_days()
    if days is None:
        return jsonify({"success": False, "message": "days must be one of 7, 30"}), 400
    zone = db.get_zone(zone_id)
    if not zone:
        return jsonify({"success": False, "message": "zone not found"}), 404

    controller_tz, now_local = _history_clock()
    today = now_local.date()
    dates = date_range(today, days)
    from_d, to_d = dates[0], dates[-1]
    raw_runs = _fetch_runs(from_d, to_d, controller_tz, zone_ids=[zone_id])

    buf = io.StringIO()
    # BOM so Excel opens UTF-8 cleanly.
    buf.write("\ufeff")
    writer = csv.writer(buf)
    writer.writerow(
        _csv_safe_cell(value)
        for value in [
            "date",
            "start_time",
            "end_time",
            "zone_id",
            "zone_name",
            "duration_min",
            "liters",
            "source",
            "status",
        ]
    )
    zone_lookup = {int(zone["id"]): zone}
    for r in raw_runs:
        serialized = _serialize_run(r, zone_lookup, controller_tz)
        sdt = _run_start_local(r, controller_tz)
        edt = _run_end_local(r, controller_tz, sdt)
        date_str = sdt.date().isoformat() if sdt else ""
        start_str = sdt.strftime("%H:%M:%S") if sdt else ""
        end_str = edt.strftime("%H:%M:%S") if edt else ""
        writer.writerow(
            _csv_safe_cell(value)
            for value in [
                date_str,
                start_str,
                end_str,
                r.get("zone_id"),
                serialized["zone_name"] or "",
                serialized["duration_min"],
                "" if r.get("total_liters") is None else r.get("total_liters"),
                r.get("source") or "",
                r.get("status") or "",
            ]
        )

    fname = f"irrigation-history-zone-{zone_id}-{_iso_local(from_d)}_{_iso_local(to_d)}.csv"
    resp = Response(buf.getvalue(), mimetype="text/csv")
    resp.headers["Content-Disposition"] = f'attachment; filename="{fname}"'
    return resp
