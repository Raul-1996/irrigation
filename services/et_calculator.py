"""ET Calculator — Evapotranspiration and irrigation runtime calculations.

Based on Hargreaves-Samani method, calibrated for C3 turf grass (Kentucky bluegrass,
fescue) on chernozem soil. All formulas from IRRIGATION-ALGORITHM.md sections 1-2.

Pure functions, no side effects, no DB/MQTT dependencies. Python 3.9 compatible.
"""

from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Altitude and microclimate corrections per site
ALTITUDE_CORRECTION = {
    "orsk": 1.0,           # 230 m, no correction needed
    "cholpon_ata": 1.12,   # 1600 m: +12% ET due to UV and pressure
}

LAKE_HUMIDITY_FACTOR = {
    "orsk": 1.0,
    "cholpon_ata": 0.92,   # -8% ET due to lake humidity (Issyk-Kul)
}

# Hunter nozzle precipitation rates (mm/h)
NOZZLE_PR = {
    "mp_rotator": 10.0,
    "pgp_ultra": 13.0,
    "pro_fixed": 40.0,
    "i20": 15.0,
}

# Safety limits
MIN_IRRIGATION_MM = 2.0
MIN_ZONE_RUNTIME_MIN = 2.0
MAX_ZONE_RUNTIME_MIN = 60.0

# Default infiltration rate for chernozem (mm/h)
DEFAULT_MAX_INFILTRATION_MM_H = 15.0

# Cycle-soak max continuous run for high-Pr nozzles (min)
CYCLE_SOAK_MAX_RUN_MIN = 8.0
CYCLE_SOAK_PAUSE_MIN = 12.0


# ---------------------------------------------------------------------------
# ET Base lookup (Table 1.1)
# ---------------------------------------------------------------------------

def lookup_et_base(t_avg):
    # type: (float) -> float
    """ET_base by temperature range (Table 1.1). Returns mm/day.

    Based on Hargreaves-Samani, calibrated for C3 turf grass.
    """
    if t_avg < 5:
        return 0.0
    elif t_avg < 15:
        return 2.0
    elif t_avg < 20:
        return 3.0
    elif t_avg < 25:
        return 4.0
    elif t_avg < 30:
        return 5.5
    elif t_avg < 35:
        return 6.5
    elif t_avg < 40:
        return 7.5
    else:
        return 8.0


# ---------------------------------------------------------------------------
# Temperature coefficient Kt (Table 1.2)
# ---------------------------------------------------------------------------

def calc_kt(t_avg):
    # type: (float) -> float
    """Temperature correction coefficient Kt (Table 1.2).

    Adjusts ET_base relative to optimal C3 grass growth temperature (18-24 C).
    """
    if t_avg < 5:
        return 0.0
    elif t_avg < 10:
        return 0.5
    elif t_avg < 15:
        return 0.7
    elif t_avg < 20:
        return 0.85
    elif t_avg < 25:
        return 1.0   # optimum for C3
    elif t_avg < 30:
        return 1.15
    elif t_avg < 35:
        return 1.25
    elif t_avg < 40:
        return 1.35
    else:
        return 1.4


# ---------------------------------------------------------------------------
# Precipitation effectiveness K_precip
# ---------------------------------------------------------------------------

def calc_k_precip(et_need_mm, precip_48h_mm):
    # type: (float, float) -> float
    """Subtract effective precipitation from irrigation need.

    Precipitation effectiveness by layer:
      - first 5 mm: 90% absorbed
      - 5-15 mm: 75% absorbed
      - >15 mm: 50% absorbed (runoff on chernozem)

    Returns corrected irrigation need (mm), never negative.
    """
    if precip_48h_mm <= 0:
        return et_need_mm

    effective = 0.0
    remaining = precip_48h_mm

    # First 5 mm
    chunk = min(remaining, 5.0)
    effective += chunk * 0.90
    remaining -= chunk

    # 5-15 mm
    if remaining > 0:
        chunk = min(remaining, 10.0)
        effective += chunk * 0.75
        remaining -= chunk

    # >15 mm
    if remaining > 0:
        effective += remaining * 0.50

    result = et_need_mm - effective
    return max(result, 0.0)


# ---------------------------------------------------------------------------
# Corrected ET
# ---------------------------------------------------------------------------

def calc_et_corrected(t_avg, site_id):
    # type: (float, str) -> float
    """Calculate corrected daily ET (mm).

    ET_corrected = ET_base * Kt * K_altitude * K_lake

    Args:
        t_avg: Average daily temperature (C)
        site_id: 'orsk' or 'cholpon_ata'

    Returns:
        Corrected ET in mm/day
    """
    et_base = lookup_et_base(t_avg)
    kt = calc_kt(t_avg)
    k_alt = ALTITUDE_CORRECTION.get(site_id, 1.0)
    k_lake = LAKE_HUMIDITY_FACTOR.get(site_id, 1.0)
    return et_base * kt * k_alt * k_lake


# ---------------------------------------------------------------------------
# Irrigation need
# ---------------------------------------------------------------------------

def calc_irrigation_need(t_avg, precip_48h_mm, site_id):
    # type: (float, float, str) -> float
    """Calculate corrected irrigation need (mm) after precipitation deduction.

    Args:
        t_avg: Average daily temperature (C)
        precip_48h_mm: Precipitation in last 48h (mm)
        site_id: 'orsk' or 'cholpon_ata'

    Returns:
        Irrigation need in mm (0 if below minimum)
    """
    et_corrected = calc_et_corrected(t_avg, site_id)
    return calc_k_precip(et_corrected, precip_48h_mm)


# ---------------------------------------------------------------------------
# Zone runtime
# ---------------------------------------------------------------------------

def calc_zone_runtime(irrigation_need_mm, pr_mm_h):
    # type: (float, float) -> float
    """Calculate zone runtime in minutes.

    Formula: runtime_min = (irrigation_need_mm / pr_mm_h) * 60
    Clamped to [MIN_ZONE_RUNTIME_MIN, MAX_ZONE_RUNTIME_MIN].

    Args:
        irrigation_need_mm: Required irrigation depth (mm)
        pr_mm_h: Nozzle precipitation rate (mm/h)

    Returns:
        Runtime in minutes, clamped to safety limits.
    """
    if pr_mm_h <= 0 or irrigation_need_mm <= 0:
        return 0.0

    runtime = (irrigation_need_mm / pr_mm_h) * 60.0
    return max(MIN_ZONE_RUNTIME_MIN, min(MAX_ZONE_RUNTIME_MIN, round(runtime, 1)))


# ---------------------------------------------------------------------------
# Cycle-soak
# ---------------------------------------------------------------------------

def calc_cycle_soak(runtime_min, pr_mm_h, max_infiltration_mm_h=None):
    # type: (float, float, Optional[float]) -> List[Dict[str, float]]
    """Split irrigation into cycles if Pr exceeds soil infiltration rate.

    For high-Pr nozzles (e.g. Pro Fixed at 40 mm/h) on chernozem
    (infiltration ~15 mm/h), continuous watering causes surface runoff.
    Solution: short run cycles with soak pauses for absorption.

    Args:
        runtime_min: Total required runtime (minutes)
        pr_mm_h: Nozzle precipitation rate (mm/h)
        max_infiltration_mm_h: Max soil infiltration rate (mm/h), default 15.0

    Returns:
        List of dicts with 'run_min' and 'soak_min' keys.
    """
    if max_infiltration_mm_h is None:
        max_infiltration_mm_h = DEFAULT_MAX_INFILTRATION_MM_H

    if pr_mm_h <= max_infiltration_mm_h:
        return [{"run_min": round(runtime_min, 1), "soak_min": 0}]

    # Max continuous run before runoff
    max_run_min = (max_infiltration_mm_h / pr_mm_h) * 60.0
    max_run_min = min(max_run_min, CYCLE_SOAK_MAX_RUN_MIN)

    cycles = []  # type: List[Dict[str, float]]
    remaining = runtime_min
    while remaining > 0:
        run = min(remaining, max_run_min)
        remaining -= run
        soak = CYCLE_SOAK_PAUSE_MIN if remaining > 0 else 0
        cycles.append({
            "run_min": round(run, 1),
            "soak_min": soak,
        })

    return cycles
