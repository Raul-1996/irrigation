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
    run_was_confirmed,
    zone_has_active_program,
)

zones_history_api_bp = Blueprint("zones_history_api", __name__)

ALLOWED_DAYS = {7, 30}


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
        "SELECT id, zone_id, group_id, start_utc, end_utc, total_liters, "
        "status, source, confirmed, created_at FROM zone_runs WHERE " + " AND ".join(clauses)
    )
    try:
        with sqlite3.connect(db.db_path, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
        dated_rows = []
        for row in rows:
            run = dict(row)
            start_local = parse_controller_datetime(run.get("start_utc"), controller_tz)
            if start_local is not None and from_local <= start_local.date() <= to_local:
                dated_rows.append((start_local, run))
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
) -> list[dict[str, Any]]:
    out = []
    for d in dates:
        item = {
            "date": _iso_local(d),
            "actual_minutes": int(actual_min.get(d, 0)),
            "runs": int(runs_count.get(d, 0)),
            "plan_minutes": int(plan_min.get(d, 0)) if has_plan else None,
        }
        out.append(item)
    return out


def _zone_identity_for_run(
    run: dict[str, Any],
    zone_lookup: dict[int, dict[str, Any]],
    controller_tz: tzinfo,
) -> tuple[dict[str, Any], bool]:
    """Resolve a run without assigning a deleted predecessor to a reused ID."""
    zone = zone_lookup.get(int(run.get("zone_id") or 0))
    if not zone:
        return {}, True

    def sqlite_utc(raw: Any) -> datetime | None:
        try:
            value = datetime.fromisoformat(str(raw).replace("Z", "+00:00")) if raw else None
        except (TypeError, ValueError):
            return None
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC) if value is not None else None

    zone_created = sqlite_utc(zone.get("created_at"))
    run_created = sqlite_utc(run.get("created_at"))
    if zone_created is not None and run_created is not None:
        if run_created < zone_created:
            return {}, True
        if run_created > zone_created:
            return zone, False

    # SQLite timestamps have one-second precision. Resolve equal timestamps
    # with the physical start invariant: a run cannot predate its own zone.
    start_local = parse_controller_datetime(run.get("start_utc"), controller_tz)
    if start_local is not None and zone_created is not None and start_local.astimezone(UTC) < zone_created:
        return {}, True
    return zone, False


def _serialize_run(
    run: dict[str, Any],
    zone_lookup: dict[int, dict[str, Any]],
    controller_tz: tzinfo,
) -> dict[str, Any]:
    zone, zone_deleted = _zone_identity_for_run(run, zone_lookup, controller_tz)
    duration_min = 0
    start_local = parse_controller_datetime(run.get("start_utc"), controller_tz)
    end_local = parse_controller_datetime(run.get("end_utc"), controller_tz)
    if start_local is not None and end_local is not None:
        dsec = (end_local - start_local).total_seconds()
        duration_min = round(dsec / 60.0) if dsec > 0 else 0
    return {
        "id": int(run.get("id")) if run.get("id") is not None else None,
        "zone_id": int(run.get("zone_id") or 0),
        "zone_name": "Удалённая зона" if zone_deleted else zone.get("name"),
        "zone_deleted": zone_deleted,
        "group_id": int(run.get("group_id") or 0),
        "start_utc": start_local.isoformat(timespec="seconds") if start_local is not None else None,
        "end_utc": end_local.isoformat(timespec="seconds") if end_local is not None else None,
        "duration_min": duration_min,
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

    programs = db.get_programs() or []
    plan_by_date = calculate_plan_for_zone(
        zone_id,
        int(zone.get("duration") or 0),
        dates,
        programs,
        as_of_local=now_local,
    )
    has_plan = zone_has_active_program(zone_id, programs)

    raw_runs = _fetch_runs(from_d, to_d, controller_tz, zone_ids=[zone_id])
    actual_min, runs_count = calculate_actual_for_zone(raw_runs, dates, controller_tz=controller_tz)

    daily = _build_daily(dates, actual_min, runs_count, plan_by_date, has_plan)

    zone_lookup = {int(zone["id"]): zone}
    runs_out = [_serialize_run(r, zone_lookup, controller_tz) for r in raw_runs]

    total_actual = sum(actual_min.values())
    total_plan = sum(plan_by_date.values()) if has_plan else 0
    summary = calculate_summary(total_actual, total_plan, has_plan)
    total_liters, liters_partial, has_liters = _aggregate_liters([r for r in raw_runs if run_counts_as_actual(r)])
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

    # Per-zone plan, summed across the selection.
    plan_agg: dict[date, int] = {d: 0 for d in dates}
    has_plan_any = False
    for z in zones:
        zid = int(z["id"])
        z_has_plan = zone_has_active_program(zid, programs)
        if z_has_plan:
            has_plan_any = True
        # Decision Q2: zones without programs contribute 0 (not NULL).
        # We use the calc helper either way — it returns {date: 0} when no match.
        zplan = calculate_plan_for_zone(
            zid,
            int(z.get("duration") or 0),
            dates,
            programs,
            as_of_local=now_local,
        )
        for d in dates:
            plan_agg[d] += int(zplan.get(d, 0))

    if zone_filter_id is not None:
        raw_runs = _fetch_runs(from_d, to_d, controller_tz, zone_ids=[zone_filter_id])
    elif group_filter_id is not None:
        raw_runs = [] if group_filter_id == 999 else _fetch_runs(from_d, to_d, controller_tz, group_id=group_filter_id)
    else:
        raw_runs = _fetch_runs(from_d, to_d, controller_tz, exclude_group_id=999)
    actual_min, runs_count = calculate_actual_for_zone(raw_runs, dates, controller_tz=controller_tz)

    daily = _build_daily(dates, actual_min, runs_count, plan_agg, has_plan_any)

    zone_lookup = {int(z["id"]): z for z in all_zones}
    runs_out = [_serialize_run(r, zone_lookup, controller_tz) for r in raw_runs]

    total_actual = sum(actual_min.values())
    total_plan = sum(plan_agg.values()) if has_plan_any else 0
    summary = calculate_summary(total_actual, total_plan, has_plan_any)
    total_liters, liters_partial, has_liters = _aggregate_liters([r for r in raw_runs if run_counts_as_actual(r)])
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
        [
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
        sdt = parse_controller_datetime(r.get("start_utc"), controller_tz)
        edt = parse_controller_datetime(r.get("end_utc"), controller_tz)
        date_str = sdt.date().isoformat() if sdt else ""
        start_str = sdt.strftime("%H:%M:%S") if sdt else ""
        end_str = edt.strftime("%H:%M:%S") if edt else ""
        writer.writerow(
            [
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
