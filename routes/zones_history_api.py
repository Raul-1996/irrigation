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

import csv
import io
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from flask import Blueprint, jsonify, request, Response

from database import db
from services.history_calc import (
    calculate_actual_for_zone,
    calculate_plan_for_zone,
    calculate_summary,
    date_range,
    zone_has_active_program,
)

zones_history_api_bp = Blueprint('zones_history_api', __name__)

ALLOWED_DAYS = {7, 30}


# ---- helpers ----

def _parse_days() -> Optional[int]:
    raw = request.args.get('days', '7')
    try:
        d = int(raw)
    except (TypeError, ValueError):
        return None
    if d not in ALLOWED_DAYS:
        return None
    return d


def _today_local() -> date:
    return datetime.now().astimezone().date()


def _iso_local(d: date) -> str:
    return d.isoformat()


def _fetch_runs_for_zones(zone_ids: List[int], from_local: date, to_local: date) -> List[Dict[str, Any]]:
    """Return zone_runs rows for ``zone_ids`` whose start_utc falls in the
    [from_local, to_local] local-date range (inclusive on both ends).

    We use a half-open UTC window slightly wider than the local range to
    account for timezone offsets, then filter precisely by local date.
    """
    if not zone_ids:
        return []
    # Half-open UTC window covering [from_local 00:00 local, to_local+1 00:00 local].
    start_local = datetime.combine(from_local, datetime.min.time()).astimezone()
    end_local = datetime.combine(to_local + timedelta(days=1), datetime.min.time()).astimezone()
    start_utc = start_local.astimezone().astimezone(tz=None).utctimetuple()
    # We just need ISO strings for the SQL parameters.
    from datetime import timezone
    start_iso = start_local.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    end_iso = end_local.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')

    placeholders = ','.join('?' * len(zone_ids))
    sql = (
        f"SELECT id, zone_id, group_id, start_utc, end_utc, total_liters, status, source "
        f"FROM zone_runs "
        f"WHERE zone_id IN ({placeholders}) "
        f"  AND start_utc IS NOT NULL "
        f"  AND start_utc >= ? AND start_utc < ? "
        f"ORDER BY start_utc DESC"
    )
    params = list(zone_ids) + [start_iso, end_iso]
    try:
        with sqlite3.connect(db.db_path, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


def _build_daily(
    dates: List[date],
    actual_min: Dict[date, int],
    runs_count: Dict[date, int],
    plan_min: Dict[date, int],
    has_plan: bool,
) -> List[Dict[str, Any]]:
    out = []
    for d in dates:
        item = {
            'date': _iso_local(d),
            'actual_minutes': int(actual_min.get(d, 0)),
            'runs': int(runs_count.get(d, 0)),
            'plan_minutes': int(plan_min.get(d, 0)) if has_plan else None,
        }
        out.append(item)
    return out


def _serialize_run(run: Dict[str, Any], zone_lookup: Dict[int, Dict[str, Any]]) -> Dict[str, Any]:
    z = zone_lookup.get(int(run.get('zone_id') or 0)) or {}
    duration_min = 0
    s = run.get('start_utc')
    e = run.get('end_utc')
    if s and e:
        try:
            sdt = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
            edt = datetime.fromisoformat(str(e).replace('Z', '+00:00'))
            dsec = (edt - sdt).total_seconds()
            duration_min = int(round(dsec / 60.0)) if dsec > 0 else 0
        except (TypeError, ValueError):
            duration_min = 0
    return {
        'id': int(run.get('id')) if run.get('id') is not None else None,
        'zone_id': int(run.get('zone_id') or 0),
        'zone_name': z.get('name'),
        'group_id': int(run.get('group_id') or 0),
        'start_utc': run.get('start_utc'),
        'end_utc': run.get('end_utc'),
        'duration_min': duration_min,
        'liters': run.get('total_liters'),
        'status': run.get('status'),
        'source': run.get('source'),
    }


def _aggregate_liters(runs: List[Dict[str, Any]]) -> Tuple[Optional[float], bool, bool]:
    """Return (total_liters_or_none, partial_flag, any_data_flag).

    - any_data_flag = at least one run has a non-NULL total_liters
    - partial_flag = some rows have liters, some don't
    - total = sum of available liters or None when no data
    """
    has_any = False
    has_missing = False
    total = 0.0
    for r in runs:
        v = r.get('total_liters')
        if v is None:
            has_missing = True
        else:
            has_any = True
            try:
                total += float(v)
            except (TypeError, ValueError):
                pass
    if not has_any:
        return None, False, False
    return total, has_missing, True


# ---- per-zone JSON ----

@zones_history_api_bp.route('/api/zones/<int:zone_id>/history', methods=['GET'])
def get_zone_history(zone_id: int):
    days = _parse_days()
    if days is None:
        return jsonify({'success': False, 'message': 'days must be one of 7, 30'}), 400
    zone = db.get_zone(zone_id)
    if not zone:
        return jsonify({'success': False, 'message': 'zone not found'}), 404

    today = _today_local()
    dates = date_range(today, days)
    from_d, to_d = dates[0], dates[-1]

    programs = db.get_programs() or []
    plan_by_date = calculate_plan_for_zone(zone_id, int(zone.get('duration') or 0), dates, programs)
    has_plan = zone_has_active_program(zone_id, programs)

    raw_runs = _fetch_runs_for_zones([zone_id], from_d, to_d)
    actual_min, runs_count = calculate_actual_for_zone(raw_runs, dates)

    daily = _build_daily(dates, actual_min, runs_count, plan_by_date, has_plan)

    zone_lookup = {int(zone['id']): zone}
    runs_out = [_serialize_run(r, zone_lookup) for r in raw_runs]

    total_actual = sum(actual_min.values())
    total_plan = sum(plan_by_date.values()) if has_plan else 0
    summary = calculate_summary(total_actual, total_plan, has_plan)
    total_liters, liters_partial, has_liters = _aggregate_liters(raw_runs)
    summary.update({
        'total_minutes': int(total_actual),
        'total_runs': int(sum(runs_count.values())),
        'total_liters': total_liters,
        'liters_partial': bool(liters_partial),
        'has_liters': bool(has_liters),
    })

    return jsonify({
        'success': True,
        'zone': {
            'id': int(zone['id']),
            'name': zone.get('name'),
            'duration': int(zone.get('duration') or 0),
            'group_id': int(zone.get('group_id') or 0),
        },
        'period': {
            'from': _iso_local(from_d),
            'to': _iso_local(to_d),
            'days': days,
        },
        'summary': summary,
        'daily': daily,
        'runs': runs_out,
    })


# ---- global JSON ----

@zones_history_api_bp.route('/api/zones/history', methods=['GET'])
def get_global_history():
    days = _parse_days()
    if days is None:
        return jsonify({'success': False, 'message': 'days must be one of 7, 30'}), 400

    group_id_raw = request.args.get('group_id')
    zone_id_raw = request.args.get('zone_id')

    all_zones = db.get_zones() or []
    # Drop the special "no irrigation" group from defaults.
    zones = [z for z in all_zones if int(z.get('group_id') or 0) != 999]

    if zone_id_raw:
        try:
            zid = int(zone_id_raw)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'message': 'invalid zone_id'}), 400
        zones = [z for z in zones if int(z['id']) == zid]
    elif group_id_raw:
        try:
            gid = int(group_id_raw)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'message': 'invalid group_id'}), 400
        zones = [z for z in zones if int(z.get('group_id') or 0) == gid]

    today = _today_local()
    dates = date_range(today, days)
    from_d, to_d = dates[0], dates[-1]

    programs = db.get_programs() or []

    # Per-zone plan, summed across the selection.
    plan_agg: Dict[date, int] = {d: 0 for d in dates}
    has_plan_any = False
    for z in zones:
        zid = int(z['id'])
        z_has_plan = zone_has_active_program(zid, programs)
        if z_has_plan:
            has_plan_any = True
        # Decision Q2: zones without programs contribute 0 (not NULL).
        # We use the calc helper either way — it returns {date: 0} when no match.
        zplan = calculate_plan_for_zone(zid, int(z.get('duration') or 0), dates, programs)
        for d in dates:
            plan_agg[d] += int(zplan.get(d, 0))

    zone_ids = [int(z['id']) for z in zones]
    raw_runs = _fetch_runs_for_zones(zone_ids, from_d, to_d)
    actual_min, runs_count = calculate_actual_for_zone(raw_runs, dates)

    daily = _build_daily(dates, actual_min, runs_count, plan_agg, has_plan_any)

    zone_lookup = {int(z['id']): z for z in all_zones}
    runs_out = [_serialize_run(r, zone_lookup) for r in raw_runs]

    total_actual = sum(actual_min.values())
    total_plan = sum(plan_agg.values()) if has_plan_any else 0
    summary = calculate_summary(total_actual, total_plan, has_plan_any)
    total_liters, liters_partial, has_liters = _aggregate_liters(raw_runs)
    summary.update({
        'total_minutes': int(total_actual),
        'total_runs': int(sum(runs_count.values())),
        'total_liters': total_liters,
        'liters_partial': bool(liters_partial),
        'has_liters': bool(has_liters),
    })

    return jsonify({
        'success': True,
        'period': {
            'from': _iso_local(from_d),
            'to': _iso_local(to_d),
            'days': days,
        },
        'filters': {
            'group_id': int(group_id_raw) if group_id_raw else None,
            'zone_id': int(zone_id_raw) if zone_id_raw else None,
        },
        'zone_count': len(zones),
        'summary': summary,
        'daily': daily,
        'runs': runs_out,
    })


# ---- CSV ----

@zones_history_api_bp.route('/api/zones/<int:zone_id>/history.csv', methods=['GET'])
def get_zone_history_csv(zone_id: int):
    days = _parse_days()
    if days is None:
        return jsonify({'success': False, 'message': 'days must be one of 7, 30'}), 400
    zone = db.get_zone(zone_id)
    if not zone:
        return jsonify({'success': False, 'message': 'zone not found'}), 404

    today = _today_local()
    dates = date_range(today, days)
    from_d, to_d = dates[0], dates[-1]
    raw_runs = _fetch_runs_for_zones([zone_id], from_d, to_d)

    buf = io.StringIO()
    # BOM so Excel opens UTF-8 cleanly.
    buf.write('\ufeff')
    writer = csv.writer(buf)
    writer.writerow([
        'date', 'start_time', 'end_time', 'zone_id', 'zone_name',
        'duration_min', 'liters', 'source', 'status',
    ])
    for r in raw_runs:
        s = r.get('start_utc')
        e = r.get('end_utc')
        try:
            sdt = datetime.fromisoformat(str(s).replace('Z', '+00:00')) if s else None
        except (TypeError, ValueError):
            sdt = None
        try:
            edt = datetime.fromisoformat(str(e).replace('Z', '+00:00')) if e else None
        except (TypeError, ValueError):
            edt = None
        date_str = sdt.astimezone().date().isoformat() if sdt else ''
        start_str = sdt.astimezone().strftime('%H:%M:%S') if sdt else ''
        end_str = edt.astimezone().strftime('%H:%M:%S') if edt else ''
        if sdt and edt:
            dsec = (edt - sdt).total_seconds()
            dur_min = int(round(dsec / 60.0)) if dsec > 0 else 0
        else:
            dur_min = 0
        writer.writerow([
            date_str,
            start_str,
            end_str,
            r.get('zone_id'),
            zone.get('name') or '',
            dur_min,
            '' if r.get('total_liters') is None else r.get('total_liters'),
            r.get('source') or '',
            r.get('status') or '',
        ])

    fname = f"irrigation-history-zone-{zone_id}-{_iso_local(from_d)}_{_iso_local(to_d)}.csv"
    resp = Response(buf.getvalue(), mimetype='text/csv; charset=utf-8')
    resp.headers['Content-Disposition'] = f'attachment; filename="{fname}"'
    return resp
