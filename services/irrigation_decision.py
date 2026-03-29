"""Irrigation Decision Engine — Decision table from IRRIGATION-ALGORITHM.md.

Evaluates 12 prioritized rules to determine irrigation action.
Pure logic module, no DB/MQTT dependencies. Python 3.9 compatible.
"""

import logging
from typing import Any, Dict, List, Optional

from services.et_calculator import (
    MIN_IRRIGATION_MM,
    calc_irrigation_need,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Decision constants
# ---------------------------------------------------------------------------

DECISION_STOP = 'stop'            # Irrigation forbidden
DECISION_SKIP = 'skip'            # Skip this cycle
DECISION_POSTPONE = 'postpone'    # Postpone (wind)
DECISION_EMERGENCY = 'emergency'  # Emergency irrigation
DECISION_IRRIGATE = 'irrigate'    # Standard irrigation
DECISION_SYRINGE = 'syringe'     # Syringe cooling cycle

# ---------------------------------------------------------------------------
# Season boundaries
# ---------------------------------------------------------------------------

SEASON_BOUNDS = {
    "orsk": {
        "start_month": 4,
        "start_day": 15,
        "end_month": 10,
        "end_day": 31,
    },
    "cholpon_ata": {
        "start_month": 4,
        "start_day": 1,
        "end_month": 10,
        "end_day": 31,
    },
}

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

FROST_THRESHOLD_C = 5.0
WIND_THRESHOLD_KMH = 25.0
RAIN_24H_THRESHOLD_MM = 5.0
RAIN_FORECAST_THRESHOLD_MM = 5.0
SOIL_MOIST_OK_PCT = 50.0
SOIL_CRITICAL_PCT = 30.0
EMERGENCY_BOOST_PCT = 20  # +20% to irrigation norm

# Syringe thresholds per site
SYRINGE_CONFIG = {
    "orsk": {
        "temp_threshold_c": 35.0,
        "syringe_time": "13:00",
    },
    "cholpon_ata": {
        "temp_threshold_c": 28.0,
        "syringe_time": "12:00",
    },
}


# ---------------------------------------------------------------------------
# Decision result
# ---------------------------------------------------------------------------

class IrrigationDecision(object):
    """Result of the decision engine evaluation."""

    def __init__(
        self,
        decision,       # type: str
        reason,         # type: str
        rule_id,        # type: int
        coefficient=100,  # type: int
        syringe=False,  # type: bool
        syringe_time=None,  # type: Optional[str]
        extra=None,     # type: Optional[Dict[str, Any]]
    ):
        self.decision = decision
        self.reason = reason
        self.rule_id = rule_id
        self.coefficient = coefficient
        self.syringe = syringe
        self.syringe_time = syringe_time
        self.extra = extra or {}

    def to_dict(self):
        # type: () -> Dict[str, Any]
        result = {
            "decision": self.decision,
            "reason": self.reason,
            "rule_id": self.rule_id,
            "coefficient": self.coefficient,
            "syringe": self.syringe,
        }
        if self.syringe_time is not None:
            result["syringe_time"] = self.syringe_time
        if self.extra:
            result["extra"] = self.extra
        return result

    def __repr__(self):
        return "IrrigationDecision(decision=%r, reason=%r, rule_id=%d)" % (
            self.decision, self.reason, self.rule_id
        )


# ---------------------------------------------------------------------------
# Helper: season check
# ---------------------------------------------------------------------------

def _is_in_season(site_id, month, day):
    # type: (str, int, int) -> bool
    """Check if the given date is within the active irrigation season."""
    bounds = SEASON_BOUNDS.get(site_id)
    if bounds is None:
        return False

    start_m = bounds["start_month"]
    start_d = bounds["start_day"]
    end_m = bounds["end_month"]
    end_d = bounds["end_day"]

    # Convert to comparable tuple (month, day)
    date_tuple = (month, day)
    start_tuple = (start_m, start_d)
    end_tuple = (end_m, end_d)

    return start_tuple <= date_tuple <= end_tuple


# ---------------------------------------------------------------------------
# Main decision function
# ---------------------------------------------------------------------------

def evaluate_decision(
    site_id,                  # type: str
    month,                    # type: int
    day,                      # type: int
    t_avg,                    # type: float
    t_current,                # type: float
    precip_24h,               # type: float
    precip_48h,               # type: float
    precip_forecast_12h,      # type: float
    wind_speed_kmh,           # type: float
    soil_moisture_pct=None,   # type: Optional[float]
):
    # type: (...) -> IrrigationDecision
    """Apply the decision table from IRRIGATION-ALGORITHM.md.

    Rules are checked by priority (1->12); first matching rule is final.

    Args:
        site_id: 'orsk' or 'cholpon_ata'
        month: Current month (1-12)
        day: Current day of month (1-31)
        t_avg: Average daily temperature (C)
        t_current: Current temperature (C)
        precip_24h: Precipitation in last 24h (mm)
        precip_48h: Precipitation in last 48h (mm)
        precip_forecast_12h: Precipitation forecast for next 12h (mm)
        wind_speed_kmh: Current wind speed (km/h)
        soil_moisture_pct: Soil moisture as % of field capacity (None if no sensor)

    Returns:
        IrrigationDecision with action and metadata.
    """

    # --- Rule 1: Off season ---
    if not _is_in_season(site_id, month, day):
        return IrrigationDecision(
            decision=DECISION_STOP,
            reason="off_season",
            rule_id=1,
            coefficient=0,
        )

    # --- Rule 2: Frost ---
    if t_current < FROST_THRESHOLD_C:
        return IrrigationDecision(
            decision=DECISION_STOP,
            reason="frost: %.1f°C < %.0f°C" % (t_current, FROST_THRESHOLD_C),
            rule_id=2,
            coefficient=0,
        )

    # --- Rule 3: Wind ---
    if wind_speed_kmh > WIND_THRESHOLD_KMH:
        return IrrigationDecision(
            decision=DECISION_POSTPONE,
            reason="wind: %.1f km/h > %.0f km/h" % (wind_speed_kmh, WIND_THRESHOLD_KMH),
            rule_id=3,
            extra={"retry_hours": 2, "max_retries": 3},
        )

    # --- Rule 4: Rain in last 24h ---
    if precip_24h > RAIN_24H_THRESHOLD_MM:
        return IrrigationDecision(
            decision=DECISION_SKIP,
            reason="rain_24h: %.1f mm > %.0f mm" % (precip_24h, RAIN_24H_THRESHOLD_MM),
            rule_id=4,
            coefficient=0,
        )

    # --- Rule 5: Rain forecast ---
    if precip_forecast_12h > RAIN_FORECAST_THRESHOLD_MM:
        return IrrigationDecision(
            decision=DECISION_SKIP,
            reason="rain_forecast: %.1f mm > %.0f mm" % (precip_forecast_12h, RAIN_FORECAST_THRESHOLD_MM),
            rule_id=5,
            coefficient=0,
        )

    # --- Rule 6: Soil moisture OK ---
    if soil_moisture_pct is not None and soil_moisture_pct >= SOIL_MOIST_OK_PCT:
        return IrrigationDecision(
            decision=DECISION_SKIP,
            reason="soil_moist: %.0f%% >= %.0f%%" % (soil_moisture_pct, SOIL_MOIST_OK_PCT),
            rule_id=6,
            coefficient=0,
        )

    # --- Rule 7: Soil moisture critical ---
    if soil_moisture_pct is not None and soil_moisture_pct < SOIL_CRITICAL_PCT:
        return IrrigationDecision(
            decision=DECISION_EMERGENCY,
            reason="soil_critical: %.0f%% < %.0f%%" % (soil_moisture_pct, SOIL_CRITICAL_PCT),
            rule_id=7,
            coefficient=100 + EMERGENCY_BOOST_PCT,
            extra={"boost_pct": EMERGENCY_BOOST_PCT},
        )

    # --- Rule 8: Below minimum irrigation threshold ---
    irrigation_need = calc_irrigation_need(t_avg, precip_48h, site_id)
    if irrigation_need < MIN_IRRIGATION_MM:
        return IrrigationDecision(
            decision=DECISION_SKIP,
            reason="below_min: %.1f mm < %.1f mm" % (irrigation_need, MIN_IRRIGATION_MM),
            rule_id=8,
            coefficient=0,
        )

    # --- From here on, we WILL irrigate. Check for syringe. ---

    syringe = False
    syringe_time = None  # type: Optional[str]
    syringe_cfg = SYRINGE_CONFIG.get(site_id)

    # --- Rule 9: Syringe for Orsk (t > 35) ---
    if site_id == "orsk" and syringe_cfg is not None:
        if t_current > syringe_cfg["temp_threshold_c"]:
            syringe = True
            syringe_time = syringe_cfg["syringe_time"]

    # --- Rule 10: Syringe for Cholpon-Ata (t > 28) ---
    if site_id == "cholpon_ata" and syringe_cfg is not None:
        if t_current > syringe_cfg["temp_threshold_c"]:
            syringe = True
            syringe_time = syringe_cfg["syringe_time"]

    # --- Rule 11: Cycle-soak detection (informational) ---
    # Actual cycle-soak is computed per-zone by et_calculator.calc_cycle_soak().
    # Here we just flag that it may be needed.
    extra = {}  # type: Dict[str, Any]
    extra["irrigation_need_mm"] = round(irrigation_need, 2)

    # --- Rule 12: Standard irrigation ---
    reason = "irrigate: need %.1f mm" % irrigation_need
    if syringe:
        reason += " + syringe at %s" % syringe_time

    return IrrigationDecision(
        decision=DECISION_IRRIGATE,
        reason=reason,
        rule_id=12 if not syringe else (9 if site_id == "orsk" else 10),
        coefficient=100,
        syringe=syringe,
        syringe_time=syringe_time,
        extra=extra,
    )


# ---------------------------------------------------------------------------
# Convenience: evaluate all rules and return a list of checked rules
# (useful for debugging / decision log display)
# ---------------------------------------------------------------------------

def evaluate_decision_verbose(
    site_id,                  # type: str
    month,                    # type: int
    day,                      # type: int
    t_avg,                    # type: float
    t_current,                # type: float
    precip_24h,               # type: float
    precip_48h,               # type: float
    precip_forecast_12h,      # type: float
    wind_speed_kmh,           # type: float
    soil_moisture_pct=None,   # type: Optional[float]
):
    # type: (...) -> Dict[str, Any]
    """Evaluate decision and return detailed rule check results.

    Returns dict with 'decision' (IrrigationDecision) and 'rules' (list of checked rules).
    """
    decision = evaluate_decision(
        site_id=site_id,
        month=month,
        day=day,
        t_avg=t_avg,
        t_current=t_current,
        precip_24h=precip_24h,
        precip_48h=precip_48h,
        precip_forecast_12h=precip_forecast_12h,
        wind_speed_kmh=wind_speed_kmh,
        soil_moisture_pct=soil_moisture_pct,
    )

    rules_checked = []  # type: List[Dict[str, Any]]

    in_season = _is_in_season(site_id, month, day)
    rules_checked.append({
        "rule_id": 1, "name": "off_season",
        "condition": "in_season=%s" % in_season,
        "triggered": not in_season,
    })

    rules_checked.append({
        "rule_id": 2, "name": "frost",
        "condition": "t_current=%.1f < %.0f" % (t_current, FROST_THRESHOLD_C),
        "triggered": t_current < FROST_THRESHOLD_C,
    })

    rules_checked.append({
        "rule_id": 3, "name": "wind",
        "condition": "wind=%.1f > %.0f" % (wind_speed_kmh, WIND_THRESHOLD_KMH),
        "triggered": wind_speed_kmh > WIND_THRESHOLD_KMH,
    })

    rules_checked.append({
        "rule_id": 4, "name": "rain_24h",
        "condition": "precip_24h=%.1f > %.0f" % (precip_24h, RAIN_24H_THRESHOLD_MM),
        "triggered": precip_24h > RAIN_24H_THRESHOLD_MM,
    })

    rules_checked.append({
        "rule_id": 5, "name": "rain_forecast",
        "condition": "forecast_12h=%.1f > %.0f" % (precip_forecast_12h, RAIN_FORECAST_THRESHOLD_MM),
        "triggered": precip_forecast_12h > RAIN_FORECAST_THRESHOLD_MM,
    })

    if soil_moisture_pct is not None:
        rules_checked.append({
            "rule_id": 6, "name": "soil_moist",
            "condition": "soil=%.0f%% >= %.0f%%" % (soil_moisture_pct, SOIL_MOIST_OK_PCT),
            "triggered": soil_moisture_pct >= SOIL_MOIST_OK_PCT,
        })
        rules_checked.append({
            "rule_id": 7, "name": "soil_critical",
            "condition": "soil=%.0f%% < %.0f%%" % (soil_moisture_pct, SOIL_CRITICAL_PCT),
            "triggered": soil_moisture_pct < SOIL_CRITICAL_PCT,
        })
    else:
        rules_checked.append({
            "rule_id": 6, "name": "soil_moist",
            "condition": "no sensor",
            "triggered": False,
        })
        rules_checked.append({
            "rule_id": 7, "name": "soil_critical",
            "condition": "no sensor",
            "triggered": False,
        })

    irrigation_need = calc_irrigation_need(t_avg, precip_48h, site_id)
    rules_checked.append({
        "rule_id": 8, "name": "below_min",
        "condition": "need=%.1f < %.1f" % (irrigation_need, MIN_IRRIGATION_MM),
        "triggered": irrigation_need < MIN_IRRIGATION_MM,
    })

    return {
        "decision": decision,
        "rules": rules_checked,
        "irrigation_need_mm": round(irrigation_need, 2),
    }
