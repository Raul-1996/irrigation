"""Единый расчёт «следующего полива» зон по программам.

Семантика (согласована с планировщиком irrigation_scheduler):
- программа с пустым списком days не запускается никогда;
- weather-skip на сегодня сдвигает нижнюю границу поиска на завтра (Issue #34);
- postpone_until зоны сдвигает её персональную нижнюю границу поиска;
- отменённый на сегодня запуск программы для группы зоны пропускается.

Используется эндпоинтами /api/zones/<id>/next-watering,
/api/zones/next-watering-bulk и расчётом next_start группы в /api/status.
"""

import logging
import sqlite3
from datetime import datetime, timedelta

from database import db
from services.helpers import parse_dt

logger = logging.getLogger(__name__)


def weather_skip_today() -> bool:
    """Issue #34: if today's weather decision is skip, next-watering must not
    return a same-day slot — scheduler would skip it anyway. Returns False on
    any error (best-effort: better to show a same-day slot than 'Никогда')."""
    try:
        from services.weather_adjustment import get_weather_adjustment

        adj = get_weather_adjustment(db.db_path)
        if not adj.is_enabled():
            return False
        # cache_only: /api/status поллится каждые несколько секунд — сетевой
        # запрос за погодой здесь блокировал бы UI (решение при самом поливе
        # принимается свежими данными в планировщике).
        return bool(adj.should_skip(cache_only=True).get("skip"))
    except (ImportError, OSError, sqlite3.Error, ValueError, TypeError) as e:
        logger.debug("Weather-skip check failed in next-watering: %s", e)
        return False


def program_runs_on(prog, d) -> bool:
    """Планируется ли запуск программы в календарный день ``d``.

    Семантика планировщика (_schedule_single_time): weekdays — день недели в
    days (пустой список → никогда); even-odd — чётность числа месяца (NULL в
    even_odd, как и у CronTrigger планировщика, означает нечётные дни);
    interval — ежедневная аппроксимация: IntervalTrigger пере-якорится при
    каждом старте сервиса, точный день заранее неизвестен.
    """
    st = prog.get("schedule_type") or "weekdays"
    if st == "interval":
        return True
    if st == "even-odd":
        want_even = prog.get("even_odd", "even") == "even"
        return (d.day % 2 == 0) == want_even
    return d.weekday() in (prog.get("days") or [])


def first_program_start_after(prog, lower_bound):
    """Return the first scheduled program-start datetime strictly > lower_bound.

    Same 0..14 day search as the per-program ``next_start`` in
    ``compute_next_watering`` but anchored at *lower_bound* instead of the
    global *now*.  Returns None if the program never runs (see
    ``program_runs_on``) or its time string is malformed.
    """
    if not prog:
        return None
    try:
        hh, mm = [int(x) for x in str(prog.get("time") or "00:00").split(":", 1)]
    except (ValueError, TypeError, KeyError) as e:
        logger.debug("Exception in first_program_start_after time parse: %s", e)
        return None
    for off in range(0, 15):
        d = lower_bound + timedelta(days=off)
        if program_runs_on(prog, d):
            cand = d.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if cand > lower_bound:
                return cand
    return None


def compute_next_watering(
    zone_ids=None,
    *,
    all_zones: list | None = None,
    programs: list | None = None,
    skip_today: bool | None = None,
) -> dict[int, dict]:
    """Рассчитать ближайший будущий старт полива для набора зон.

    ``zone_ids=None`` → все зоны, кроме служебной группы 999.
    ``all_zones``/``programs`` — необязательные предзагруженные списки
    (иначе читаются из БД); ``skip_today`` — предвычисленный результат
    ``weather_skip_today()`` (иначе вычисляется здесь).

    Возвращает словарь ``{zone_id: entry}``, где entry:
    ``next_dt`` (datetime | None), ``program`` (dict | None — программа
    победившего кандидата), ``zone_position``/``total_zones`` (позиция зоны
    в отсортированном списке зон этой программы), ``has_programs`` (bool —
    входит ли зона хоть в одну программу).
    """
    if all_zones is None:
        all_zones = db.get_zones() or []
    if programs is None:
        programs = db.get_programs() or []
    if zone_ids is None:
        zone_ids = [int(z.get("id")) for z in all_zones if int(z.get("group_id") or z.get("group") or 0) != 999]
    zone_ids = [int(z) for z in zone_ids]
    duration_by_zone = {int(z["id"]): int(z.get("duration") or 0) for z in all_zones}
    offset_map_per_program = []
    for p in programs:
        try:
            zones_list = sorted([int(x) for x in (p.get("zones") or [])])
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in compute_next_watering: %s", e)
            zones_list = []
        offsets = {}
        cum = 0
        for zid in zones_list:
            offsets[zid] = cum
            cum += int(duration_by_zone.get(zid, 0))
        offset_map_per_program.append({"prog": p, "offsets": offsets, "zones_sorted": zones_list})
    now = datetime.now()
    # Issue #34: weather skip in effect → push baseline past today so all
    # per-program candidates land on the next eligible day.
    if skip_today is None:
        skip_today = weather_skip_today()
    if skip_today:
        tomorrow_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        if tomorrow_midnight > now:
            now = tomorrow_midnight
    # Per-zone postpone lookup — lets us advance the per-zone lower bound
    # so cards never display a next-run inside an active postpone window.
    zone_by_id = {int(z["id"]): z for z in all_zones}
    prog_info = {}
    for pm in offset_map_per_program:
        p = pm["prog"]
        try:
            hh, mm = [int(x) for x in str(p.get("time") or "00:00").split(":", 1)]
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in compute_next_watering: %s", e)
            hh, mm = 0, 0
        best = None
        today_start = None
        if program_runs_on(p, now):
            today_start = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        try:
            zones_list = p.get("zones") or []
            total_prog_min = sum(int(duration_by_zone.get(int(zid), 0)) for zid in zones_list)
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in compute_next_watering: %s", e)
            total_prog_min = 0
        in_progress = False
        elapsed_min = 0
        if today_start and today_start <= now and total_prog_min > 0:
            today_end = today_start + timedelta(minutes=total_prog_min)
            if now < today_end:
                in_progress = True
                elapsed_min = int((now - today_start).total_seconds() // 60)
        for off in range(0, 14):
            d = now + timedelta(days=off)
            if program_runs_on(p, d):
                cand = d.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if cand <= now:
                    continue
                best = cand
                break
        prog_info[p.get("id")] = {
            "next_start": best,
            "today_start": today_start,
            "in_progress": in_progress,
            "elapsed_min": elapsed_min,
        }
    result = {}
    for zid in zone_ids:
        best_dt = None
        best_pm = None
        # Per-zone lower bound: if zone has an active postpone_until in the
        # future, advance zone_now to it so we never display a next-run
        # that the scheduler would skip.
        z = zone_by_id.get(int(zid))
        zone_now = now
        if z:
            pu_dt = parse_dt(z.get("postpone_until"))
            if pu_dt and pu_dt > zone_now:
                zone_now = pu_dt
        for pm in offset_map_per_program:
            p = pm["prog"]
            offsets = pm["offsets"]
            if zid not in offsets:
                continue
            pinfo = prog_info.get(p.get("id")) or {}
            cancelled_today = False
            try:
                gid = int(z.get("group_id") or 0) if z else 0
                if gid and pinfo.get("today_start"):
                    run_date = pinfo["today_start"].strftime("%Y-%m-%d")
                    cancelled_today = db.is_program_run_cancelled_for_group(int(p.get("id")), run_date, gid)
            except (sqlite3.Error, OSError) as e:
                logger.debug("Exception in compute_next_watering: %s", e)
                cancelled_today = False

            # When the zone is postponed (zone_now > now), an in-progress
            # program for the cohort is irrelevant for THIS zone — the
            # scheduler will skip it. Fall through to "search future
            # programs from zone_now".
            if pinfo.get("in_progress") and pinfo.get("today_start") and not cancelled_today and zone_now <= now:
                off_min = int(offsets.get(zid, 0))
                if off_min >= int(pinfo.get("elapsed_min") or 0):
                    cand = pinfo["today_start"] + timedelta(minutes=off_min)
                else:
                    start_dt = pinfo.get("next_start")
                    if not start_dt:
                        continue
                    cand = start_dt + timedelta(minutes=off_min)
            else:
                # Common path: cached next_start (anchored at global now).
                # When the zone is postponed (or the start landed inside
                # the postpone window), recompute from zone_now. Якорь
                # сдвигается на offset зоны: postpone, истекающий между
                # стартом программы и слотом зоны, не должен уводить показ
                # на следующий запуск — планировщик польёт в сегодняшний
                # слот (job зоны стреляет в start+offset, когда postpone
                # уже истёк).
                off_min = int(offsets.get(zid, 0))
                if zone_now != now:
                    start_dt = first_program_start_after(p, zone_now - timedelta(minutes=off_min))
                else:
                    start_dt = pinfo.get("next_start")
                if not start_dt:
                    continue
                cand = start_dt + timedelta(minutes=off_min)
                try:
                    # cancelled_today shifts to next-week's run — but if
                    # zone is postponed, we already searched from zone_now
                    # which strictly skips the postpone window, so the
                    # cancelled_today shift is unnecessary (and would be
                    # incorrect — it ignores postpone_until).
                    if p.get("id") is not None and pinfo.get("today_start") and cancelled_today and zone_now == now:
                        hh, mm = map(int, str(p.get("time") or "00:00").split(":", 1))
                        ns = None
                        for off in range(1, 15):
                            d = now + timedelta(days=off)
                            if program_runs_on(p, d):
                                ns = d.replace(hour=hh, minute=mm, second=0, microsecond=0)
                                break
                        if ns:
                            cand = ns + timedelta(minutes=int(offsets.get(zid, 0)))
                except (ValueError, TypeError, KeyError) as e:
                    logger.debug("Handled exception in compute_next_watering: %s", e)
            if cand <= zone_now:
                continue
            if best_dt is None or cand < best_dt:
                best_dt = cand
                best_pm = pm
        zones_sorted = best_pm["zones_sorted"] if best_pm else []
        result[int(zid)] = {
            "next_dt": best_dt,
            "program": best_pm["prog"] if best_pm else None,
            "zone_position": (zones_sorted.index(int(zid)) + 1) if int(zid) in zones_sorted else None,
            "total_zones": len(zones_sorted) if best_pm else None,
            "has_programs": any(int(zid) in pm["offsets"] for pm in offset_map_per_program),
        }
    return result
