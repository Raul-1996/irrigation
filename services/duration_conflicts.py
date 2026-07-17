"""Проверка конфликтов программ при изменении длительности зоны.

Единый алгоритм пересечения интервалов для single- и bulk-эндпоинтов
/api/zones/check-duration-conflicts[-bulk]. Каждое изменение считается
независимо: длительности остальных зон берутся из ``zones_cache`` как есть.
"""

import json
import logging

logger = logging.getLogger(__name__)


def compute_duration_conflicts(zone_id: int, new_duration: int, programs: list, zones_cache: dict) -> list[dict]:
    """Вернуть список конфликтов программ для зоны с новой длительностью.

    ``programs`` — список программ (db.get_programs()),
    ``zones_cache`` — {zone_id: zone_dict} (db.get_zones()).
    """

    def get_zone_group(zid: int):
        z = zones_cache.get(zid)
        return z["group_id"] if z else None

    def get_zone_duration(zid: int):
        z = zones_cache.get(zid)
        if not z:
            return 0
        try:
            return int(z.get("duration") or 0)
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in get_zone_duration: %s", e)
            return 0

    conflicts = []
    for program in programs:
        prog_days = program["days"] if isinstance(program["days"], list) else json.loads(program["days"])
        prog_zones = program["zones"] if isinstance(program["zones"], list) else json.loads(program["zones"])
        if zone_id not in prog_zones:
            continue
        try:
            p_hour, p_min = map(int, program["time"].split(":"))
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in compute_duration_conflicts: %s", e)
            continue
        start_a = p_hour * 60 + p_min
        total_duration_a = 0
        for zid in prog_zones:
            total_duration_a += new_duration if zid == zone_id else get_zone_duration(zid)
        end_a = start_a + total_duration_a
        groups_a = set(filter(lambda g: g is not None, [get_zone_group(zid) for zid in prog_zones]))
        for other in programs:
            if other["id"] == program["id"]:
                continue
            other_days = other["days"] if isinstance(other["days"], list) else json.loads(other["days"])
            if not (set(prog_days) & set(other_days)):
                continue
            other_zones = other["zones"] if isinstance(other["zones"], list) else json.loads(other["zones"])
            common_zones = set(prog_zones) & set(other_zones)
            groups_b = set(filter(lambda g: g is not None, [get_zone_group(zid) for zid in other_zones]))
            if not common_zones and not (groups_a & groups_b):
                continue
            try:
                oh, om = map(int, other["time"].split(":"))
            except (ValueError, TypeError, KeyError) as e:
                logger.debug("Exception in compute_duration_conflicts: %s", e)
                continue
            start_b = oh * 60 + om
            total_duration_b = 0
            for zid in other_zones:
                total_duration_b += get_zone_duration(zid)
            end_b = start_b + total_duration_b
            if start_a < end_b and end_a > start_b:
                conflicts.append(
                    {
                        "checked_program_id": program["id"],
                        "checked_program_name": program["name"],
                        "checked_program_time": program["time"],
                        "other_program_id": other["id"],
                        "other_program_name": other["name"],
                        "other_program_time": other["time"],
                        "common_zones": list(common_zones),
                        "common_groups": list(groups_a & groups_b),
                        "overlap_start": max(start_a, start_b),
                        "overlap_end": min(end_a, end_b),
                    }
                )
    return conflicts
