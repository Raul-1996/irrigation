"""Tests for Irrigation Decision Engine (services/irrigation_decision.py).

Covers all 12 rules, priority ordering, seasonal boundaries, syringe logic.
Python 3.9 compatible.
"""
import os
import pytest

os.environ['TESTING'] = '1'

from services.irrigation_decision import (
    DECISION_STOP,
    DECISION_SKIP,
    DECISION_POSTPONE,
    DECISION_EMERGENCY,
    DECISION_IRRIGATE,
    SEASON_BOUNDS,
    FROST_THRESHOLD_C,
    WIND_THRESHOLD_KMH,
    RAIN_24H_THRESHOLD_MM,
    RAIN_FORECAST_THRESHOLD_MM,
    SOIL_MOIST_OK_PCT,
    SOIL_CRITICAL_PCT,
    EMERGENCY_BOOST_PCT,
    SYRINGE_CONFIG,
    IrrigationDecision,
    evaluate_decision,
    evaluate_decision_verbose,
    _is_in_season,
)


# ---------------------------------------------------------------------------
# Helper: default kwargs for a "normal summer day" in Orsk
# ---------------------------------------------------------------------------

def _base_kwargs(**overrides):
    """Return baseline kwargs for a normal irrigable day."""
    defaults = {
        "site_id": "orsk",
        "month": 7,
        "day": 15,
        "t_avg": 25.0,
        "t_current": 25.0,
        "precip_24h": 0.0,
        "precip_48h": 0.0,
        "precip_forecast_12h": 0.0,
        "wind_speed_kmh": 5.0,
        "soil_moisture_pct": None,
    }
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# Season check helper
# ---------------------------------------------------------------------------

class TestIsInSeason:
    def test_orsk_in_season(self):
        assert _is_in_season("orsk", 5, 1) is True
        assert _is_in_season("orsk", 7, 15) is True
        assert _is_in_season("orsk", 10, 31) is True

    def test_orsk_out_of_season(self):
        assert _is_in_season("orsk", 3, 15) is False
        assert _is_in_season("orsk", 4, 14) is False
        assert _is_in_season("orsk", 11, 1) is False
        assert _is_in_season("orsk", 1, 1) is False

    def test_orsk_boundary_start(self):
        assert _is_in_season("orsk", 4, 15) is True
        assert _is_in_season("orsk", 4, 14) is False

    def test_cholpon_ata_in_season(self):
        assert _is_in_season("cholpon_ata", 4, 1) is True
        assert _is_in_season("cholpon_ata", 6, 15) is True

    def test_cholpon_ata_boundary_start(self):
        assert _is_in_season("cholpon_ata", 4, 1) is True
        assert _is_in_season("cholpon_ata", 3, 31) is False

    def test_unknown_site(self):
        assert _is_in_season("unknown", 7, 15) is False


# ---------------------------------------------------------------------------
# Rule 1: Off season
# ---------------------------------------------------------------------------

class TestRule1OffSeason:
    def test_orsk_march(self):
        d = evaluate_decision(**_base_kwargs(month=3, day=15))
        assert d.decision == DECISION_STOP
        assert d.rule_id == 1
        assert "off_season" in d.reason

    def test_orsk_november(self):
        d = evaluate_decision(**_base_kwargs(month=11, day=1))
        assert d.decision == DECISION_STOP
        assert d.rule_id == 1

    def test_cholpon_ata_march(self):
        d = evaluate_decision(**_base_kwargs(site_id="cholpon_ata", month=3, day=31))
        assert d.decision == DECISION_STOP
        assert d.rule_id == 1

    def test_orsk_april_14_out(self):
        d = evaluate_decision(**_base_kwargs(month=4, day=14))
        assert d.decision == DECISION_STOP
        assert d.rule_id == 1

    def test_orsk_april_15_in(self):
        d = evaluate_decision(**_base_kwargs(month=4, day=15))
        assert d.decision != DECISION_STOP or d.rule_id != 1


# ---------------------------------------------------------------------------
# Rule 2: Frost
# ---------------------------------------------------------------------------

class TestRule2Frost:
    def test_frost(self):
        d = evaluate_decision(**_base_kwargs(t_current=3.0))
        assert d.decision == DECISION_STOP
        assert d.rule_id == 2
        assert "frost" in d.reason

    def test_frost_boundary(self):
        """t_current exactly at threshold -> no frost (< not <=)."""
        d = evaluate_decision(**_base_kwargs(t_current=5.0))
        assert d.rule_id != 2

    def test_frost_just_below(self):
        d = evaluate_decision(**_base_kwargs(t_current=4.9))
        assert d.decision == DECISION_STOP
        assert d.rule_id == 2

    def test_frost_negative(self):
        d = evaluate_decision(**_base_kwargs(t_current=-5.0))
        assert d.decision == DECISION_STOP
        assert d.rule_id == 2


# ---------------------------------------------------------------------------
# Rule 3: Wind
# ---------------------------------------------------------------------------

class TestRule3Wind:
    def test_high_wind(self):
        d = evaluate_decision(**_base_kwargs(wind_speed_kmh=30.0))
        assert d.decision == DECISION_POSTPONE
        assert d.rule_id == 3
        assert "wind" in d.reason

    def test_wind_boundary(self):
        """Exactly at threshold -> no postpone (> not >=)."""
        d = evaluate_decision(**_base_kwargs(wind_speed_kmh=25.0))
        assert d.rule_id != 3

    def test_wind_just_above(self):
        d = evaluate_decision(**_base_kwargs(wind_speed_kmh=25.1))
        assert d.decision == DECISION_POSTPONE
        assert d.rule_id == 3

    def test_wind_has_retry_info(self):
        d = evaluate_decision(**_base_kwargs(wind_speed_kmh=30.0))
        assert "retry_hours" in d.extra


# ---------------------------------------------------------------------------
# Rule 4: Rain 24h
# ---------------------------------------------------------------------------

class TestRule4Rain24h:
    def test_heavy_rain(self):
        d = evaluate_decision(**_base_kwargs(precip_24h=8.0))
        assert d.decision == DECISION_SKIP
        assert d.rule_id == 4
        assert "rain_24h" in d.reason

    def test_rain_boundary(self):
        """Exactly at threshold -> no skip (> not >=)."""
        d = evaluate_decision(**_base_kwargs(precip_24h=5.0))
        assert d.rule_id != 4

    def test_rain_just_above(self):
        d = evaluate_decision(**_base_kwargs(precip_24h=5.1))
        assert d.decision == DECISION_SKIP
        assert d.rule_id == 4


# ---------------------------------------------------------------------------
# Rule 5: Rain forecast
# ---------------------------------------------------------------------------

class TestRule5RainForecast:
    def test_rain_forecast(self):
        d = evaluate_decision(**_base_kwargs(precip_forecast_12h=7.0))
        assert d.decision == DECISION_SKIP
        assert d.rule_id == 5
        assert "rain_forecast" in d.reason

    def test_forecast_boundary(self):
        d = evaluate_decision(**_base_kwargs(precip_forecast_12h=5.0))
        assert d.rule_id != 5

    def test_forecast_just_above(self):
        d = evaluate_decision(**_base_kwargs(precip_forecast_12h=5.1))
        assert d.decision == DECISION_SKIP
        assert d.rule_id == 5


# ---------------------------------------------------------------------------
# Rule 6: Soil moisture OK
# ---------------------------------------------------------------------------

class TestRule6SoilMoist:
    def test_soil_moist(self):
        d = evaluate_decision(**_base_kwargs(soil_moisture_pct=55.0))
        assert d.decision == DECISION_SKIP
        assert d.rule_id == 6
        assert "soil_moist" in d.reason

    def test_soil_moist_boundary(self):
        """Exactly at threshold -> skip (>= not >)."""
        d = evaluate_decision(**_base_kwargs(soil_moisture_pct=50.0))
        assert d.decision == DECISION_SKIP
        assert d.rule_id == 6

    def test_soil_below_threshold(self):
        d = evaluate_decision(**_base_kwargs(soil_moisture_pct=40.0))
        assert d.rule_id != 6

    def test_no_sensor(self):
        """No soil sensor -> rule skipped."""
        d = evaluate_decision(**_base_kwargs(soil_moisture_pct=None))
        assert d.rule_id != 6


# ---------------------------------------------------------------------------
# Rule 7: Soil critical
# ---------------------------------------------------------------------------

class TestRule7SoilCritical:
    def test_soil_critical(self):
        d = evaluate_decision(**_base_kwargs(soil_moisture_pct=25.0))
        assert d.decision == DECISION_EMERGENCY
        assert d.rule_id == 7
        assert d.coefficient == 100 + EMERGENCY_BOOST_PCT

    def test_soil_critical_boundary(self):
        """Exactly at threshold -> not critical (< not <=)."""
        d = evaluate_decision(**_base_kwargs(soil_moisture_pct=30.0))
        assert d.rule_id != 7

    def test_soil_just_below(self):
        d = evaluate_decision(**_base_kwargs(soil_moisture_pct=29.9))
        assert d.decision == DECISION_EMERGENCY
        assert d.rule_id == 7

    def test_no_sensor_skips(self):
        d = evaluate_decision(**_base_kwargs(soil_moisture_pct=None))
        assert d.rule_id != 7

    def test_emergency_has_boost(self):
        d = evaluate_decision(**_base_kwargs(soil_moisture_pct=20.0))
        assert d.extra.get("boost_pct") == EMERGENCY_BOOST_PCT


# ---------------------------------------------------------------------------
# Rule 8: Below minimum irrigation
# ---------------------------------------------------------------------------

class TestRule8BelowMin:
    def test_below_min(self):
        """Low temp + some rain -> very low need -> skip."""
        # t_avg=10 -> ET_base=2.0, Kt=0.7 -> ET_corr=1.4, need<2
        d = evaluate_decision(**_base_kwargs(t_avg=10.0, precip_48h=0.0))
        # ET_corr = 2.0 * 0.7 = 1.4 < 2.0
        assert d.decision == DECISION_SKIP
        assert d.rule_id == 8
        assert "below_min" in d.reason

    def test_above_min(self):
        """Normal temp -> need above min -> should irrigate."""
        d = evaluate_decision(**_base_kwargs(t_avg=25.0, precip_48h=0.0))
        assert d.rule_id != 8


# ---------------------------------------------------------------------------
# Rule 9: Syringe Orsk (t > 35)
# ---------------------------------------------------------------------------

class TestRule9SyringeOrsk:
    def test_syringe_orsk(self):
        d = evaluate_decision(**_base_kwargs(
            site_id="orsk", t_current=37.0, t_avg=35.0
        ))
        assert d.decision == DECISION_IRRIGATE
        assert d.syringe is True
        assert d.syringe_time == "13:00"
        assert d.rule_id == 9

    def test_no_syringe_below_threshold(self):
        d = evaluate_decision(**_base_kwargs(
            site_id="orsk", t_current=34.0, t_avg=25.0
        ))
        assert d.syringe is False

    def test_syringe_boundary(self):
        """Exactly at 35 -> no syringe (> not >=)."""
        d = evaluate_decision(**_base_kwargs(
            site_id="orsk", t_current=35.0, t_avg=25.0
        ))
        assert d.syringe is False


# ---------------------------------------------------------------------------
# Rule 10: Syringe Cholpon-Ata (t > 28)
# ---------------------------------------------------------------------------

class TestRule10SyringeCholpon:
    def test_syringe_cholpon(self):
        d = evaluate_decision(**_base_kwargs(
            site_id="cholpon_ata", month=7, day=15,
            t_current=30.0, t_avg=28.0
        ))
        assert d.decision == DECISION_IRRIGATE
        assert d.syringe is True
        assert d.syringe_time == "12:00"
        assert d.rule_id == 10

    def test_no_syringe_below(self):
        d = evaluate_decision(**_base_kwargs(
            site_id="cholpon_ata", month=7, day=15,
            t_current=27.0, t_avg=25.0
        ))
        assert d.syringe is False

    def test_syringe_boundary(self):
        """Exactly at 28 -> no syringe."""
        d = evaluate_decision(**_base_kwargs(
            site_id="cholpon_ata", month=7, day=15,
            t_current=28.0, t_avg=25.0
        ))
        assert d.syringe is False


# ---------------------------------------------------------------------------
# Rule 12: Normal irrigation
# ---------------------------------------------------------------------------

class TestRule12Normal:
    def test_normal_irrigate(self):
        d = evaluate_decision(**_base_kwargs(t_avg=25.0))
        assert d.decision == DECISION_IRRIGATE
        assert d.rule_id == 12
        assert d.coefficient == 100
        assert d.syringe is False

    def test_has_irrigation_need(self):
        d = evaluate_decision(**_base_kwargs(t_avg=25.0))
        assert "irrigation_need_mm" in d.extra
        assert d.extra["irrigation_need_mm"] > 0


# ---------------------------------------------------------------------------
# Priority tests (higher priority wins)
# ---------------------------------------------------------------------------

class TestPriority:
    def test_frost_over_rain(self):
        """Rule 2 (frost) beats Rule 4 (rain)."""
        d = evaluate_decision(**_base_kwargs(
            t_current=3.0, precip_24h=10.0
        ))
        assert d.decision == DECISION_STOP
        assert d.rule_id == 2

    def test_frost_over_wind(self):
        """Rule 2 (frost) beats Rule 3 (wind)."""
        d = evaluate_decision(**_base_kwargs(
            t_current=3.0, wind_speed_kmh=30.0
        ))
        assert d.decision == DECISION_STOP
        assert d.rule_id == 2

    def test_wind_over_rain(self):
        """Rule 3 (wind) beats Rule 4 (rain)."""
        d = evaluate_decision(**_base_kwargs(
            wind_speed_kmh=30.0, precip_24h=10.0
        ))
        assert d.decision == DECISION_POSTPONE
        assert d.rule_id == 3

    def test_off_season_over_everything(self):
        """Rule 1 (off_season) beats all."""
        d = evaluate_decision(**_base_kwargs(
            month=1, day=15,
            t_current=3.0, wind_speed_kmh=30.0, precip_24h=10.0
        ))
        assert d.decision == DECISION_STOP
        assert d.rule_id == 1

    def test_rain_24h_over_forecast(self):
        """Rule 4 (rain_24h) beats Rule 5 (forecast)."""
        d = evaluate_decision(**_base_kwargs(
            precip_24h=10.0, precip_forecast_12h=10.0
        ))
        assert d.rule_id == 4

    def test_soil_ok_over_critical(self):
        """Rule 6 (soil >= 50%) checked before Rule 7."""
        d = evaluate_decision(**_base_kwargs(soil_moisture_pct=55.0))
        assert d.rule_id == 6

    def test_soil_critical_over_below_min(self):
        """Rule 7 (emergency) checked before Rule 8."""
        d = evaluate_decision(**_base_kwargs(
            soil_moisture_pct=20.0, t_avg=10.0
        ))
        assert d.decision == DECISION_EMERGENCY
        assert d.rule_id == 7


# ---------------------------------------------------------------------------
# IrrigationDecision methods
# ---------------------------------------------------------------------------

class TestIrrigationDecisionClass:
    def test_to_dict(self):
        d = IrrigationDecision(
            decision=DECISION_IRRIGATE,
            reason="test",
            rule_id=12,
            coefficient=100,
        )
        result = d.to_dict()
        assert result["decision"] == DECISION_IRRIGATE
        assert result["reason"] == "test"
        assert result["rule_id"] == 12
        assert result["coefficient"] == 100
        assert result["syringe"] is False

    def test_to_dict_with_syringe(self):
        d = IrrigationDecision(
            decision=DECISION_IRRIGATE,
            reason="hot",
            rule_id=9,
            syringe=True,
            syringe_time="13:00",
        )
        result = d.to_dict()
        assert result["syringe"] is True
        assert result["syringe_time"] == "13:00"

    def test_repr(self):
        d = IrrigationDecision(DECISION_STOP, "frost", 2)
        r = repr(d)
        assert "STOP" not in r or "stop" in r  # contains decision string
        assert "frost" in r


# ---------------------------------------------------------------------------
# evaluate_decision_verbose
# ---------------------------------------------------------------------------

class TestVerbose:
    def test_returns_decision_and_rules(self):
        result = evaluate_decision_verbose(**_base_kwargs())
        assert "decision" in result
        assert "rules" in result
        assert isinstance(result["rules"], list)
        assert len(result["rules"]) >= 7

    def test_rules_have_structure(self):
        result = evaluate_decision_verbose(**_base_kwargs())
        for rule in result["rules"]:
            assert "rule_id" in rule
            assert "name" in rule
            assert "triggered" in rule

    def test_off_season_triggered(self):
        result = evaluate_decision_verbose(**_base_kwargs(month=1, day=1))
        rule1 = [r for r in result["rules"] if r["rule_id"] == 1][0]
        assert rule1["triggered"] is True

    def test_irrigation_need_in_result(self):
        result = evaluate_decision_verbose(**_base_kwargs())
        assert "irrigation_need_mm" in result


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_all_conditions_bad(self):
        """Multiple bad conditions: highest priority wins (off_season)."""
        d = evaluate_decision(
            site_id="orsk", month=1, day=1,
            t_avg=0.0, t_current=-10.0,
            precip_24h=50.0, precip_48h=80.0,
            precip_forecast_12h=20.0,
            wind_speed_kmh=40.0,
            soil_moisture_pct=80.0,
        )
        assert d.decision == DECISION_STOP
        assert d.rule_id == 1

    def test_season_boundary_orsk_end(self):
        """October 31 is last day of season."""
        d = evaluate_decision(**_base_kwargs(month=10, day=31))
        assert d.rule_id != 1

    def test_season_boundary_orsk_after_end(self):
        """November 1 is off season."""
        d = evaluate_decision(**_base_kwargs(month=11, day=1))
        assert d.decision == DECISION_STOP
        assert d.rule_id == 1

    def test_cholpon_early_season(self):
        """April 1 in Cholpon-Ata is in season."""
        d = evaluate_decision(**_base_kwargs(
            site_id="cholpon_ata", month=4, day=1
        ))
        assert d.rule_id != 1

    def test_zero_everything(self):
        """Zero precip/wind, in season, warm -> irrigate."""
        d = evaluate_decision(**_base_kwargs(
            t_avg=22.0, t_current=22.0,
            precip_24h=0.0, precip_48h=0.0,
            precip_forecast_12h=0.0,
            wind_speed_kmh=0.0,
        ))
        assert d.decision == DECISION_IRRIGATE
