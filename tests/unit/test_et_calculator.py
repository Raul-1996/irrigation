"""Tests for ET Calculator (services/et_calculator.py).

Covers: lookup_et_base, calc_kt, calc_k_precip, calc_et_corrected,
        calc_irrigation_need, calc_zone_runtime, calc_cycle_soak.
Python 3.9 compatible.
"""
import os
import pytest

os.environ['TESTING'] = '1'

from services.et_calculator import (
    ALTITUDE_CORRECTION,
    LAKE_HUMIDITY_FACTOR,
    MIN_ZONE_RUNTIME_MIN,
    MAX_ZONE_RUNTIME_MIN,
    MIN_IRRIGATION_MM,
    DEFAULT_MAX_INFILTRATION_MM_H,
    CYCLE_SOAK_MAX_RUN_MIN,
    CYCLE_SOAK_PAUSE_MIN,
    lookup_et_base,
    calc_kt,
    calc_k_precip,
    calc_et_corrected,
    calc_irrigation_need,
    calc_zone_runtime,
    calc_cycle_soak,
)


# ---------------------------------------------------------------------------
# lookup_et_base
# ---------------------------------------------------------------------------

class TestLookupEtBase:
    def test_below_5(self):
        assert lookup_et_base(4.9) == 0.0
        assert lookup_et_base(-10.0) == 0.0

    def test_range_5_15(self):
        assert lookup_et_base(5.0) == 2.0
        assert lookup_et_base(10.0) == 2.0
        assert lookup_et_base(14.9) == 2.0

    def test_range_15_20(self):
        assert lookup_et_base(15.0) == 3.0
        assert lookup_et_base(19.9) == 3.0

    def test_range_20_25(self):
        assert lookup_et_base(20.0) == 4.0
        assert lookup_et_base(24.9) == 4.0

    def test_range_25_30(self):
        assert lookup_et_base(25.0) == 5.5
        assert lookup_et_base(29.9) == 5.5

    def test_range_30_35(self):
        assert lookup_et_base(30.0) == 6.5
        assert lookup_et_base(34.9) == 6.5

    def test_range_35_40(self):
        assert lookup_et_base(35.0) == 7.5
        assert lookup_et_base(39.9) == 7.5

    def test_above_40(self):
        assert lookup_et_base(40.0) == 8.0
        assert lookup_et_base(50.0) == 8.0

    def test_boundary_exact(self):
        """Verify exact boundary values (left-inclusive)."""
        assert lookup_et_base(5.0) == 2.0
        assert lookup_et_base(15.0) == 3.0
        assert lookup_et_base(20.0) == 4.0
        assert lookup_et_base(25.0) == 5.5
        assert lookup_et_base(30.0) == 6.5
        assert lookup_et_base(35.0) == 7.5
        assert lookup_et_base(40.0) == 8.0


# ---------------------------------------------------------------------------
# calc_kt
# ---------------------------------------------------------------------------

class TestCalcKt:
    def test_below_5(self):
        assert calc_kt(4.9) == 0.0
        assert calc_kt(-5.0) == 0.0

    def test_range_5_10(self):
        assert calc_kt(5.0) == 0.5
        assert calc_kt(9.9) == 0.5

    def test_range_10_15(self):
        assert calc_kt(10.0) == 0.7
        assert calc_kt(14.9) == 0.7

    def test_range_15_20(self):
        assert calc_kt(15.0) == 0.85
        assert calc_kt(19.9) == 0.85

    def test_range_20_25_optimum(self):
        """C3 grass optimum: Kt = 1.0"""
        assert calc_kt(20.0) == 1.0
        assert calc_kt(22.0) == 1.0
        assert calc_kt(24.9) == 1.0

    def test_range_25_30(self):
        assert calc_kt(25.0) == 1.15
        assert calc_kt(29.9) == 1.15

    def test_range_30_35(self):
        assert calc_kt(30.0) == 1.25
        assert calc_kt(34.9) == 1.25

    def test_range_35_40(self):
        assert calc_kt(35.0) == 1.35
        assert calc_kt(39.9) == 1.35

    def test_above_40(self):
        assert calc_kt(40.0) == 1.4
        assert calc_kt(45.0) == 1.4


# ---------------------------------------------------------------------------
# calc_k_precip
# ---------------------------------------------------------------------------

class TestCalcKPrecip:
    def test_no_rain(self):
        """No precipitation -> need unchanged."""
        assert calc_k_precip(5.0, 0.0) == 5.0

    def test_negative_rain(self):
        """Negative precipitation treated as zero."""
        assert calc_k_precip(5.0, -1.0) == 5.0

    def test_first_layer_5mm(self):
        """5mm rain: 90% effective -> 4.5mm deducted."""
        result = calc_k_precip(5.0, 5.0)
        assert abs(result - 0.5) < 0.01

    def test_two_layers_10mm(self):
        """10mm rain: 5*0.9 + 5*0.75 = 4.5 + 3.75 = 8.25 effective."""
        result = calc_k_precip(10.0, 10.0)
        expected = 10.0 - 8.25
        assert abs(result - expected) < 0.01

    def test_all_three_layers_20mm(self):
        """20mm rain: 5*0.9 + 10*0.75 + 5*0.50 = 4.5+7.5+2.5 = 14.5 effective."""
        result = calc_k_precip(15.0, 20.0)
        expected = 15.0 - 14.5
        assert abs(result - expected) < 0.01

    def test_excess_precip_returns_zero(self):
        """When effective precip > need, result is 0 (not negative)."""
        result = calc_k_precip(3.0, 20.0)
        assert result == 0.0

    def test_small_rain_1mm(self):
        """1mm rain at 90% -> 0.9mm deducted."""
        result = calc_k_precip(5.0, 1.0)
        assert abs(result - 4.1) < 0.01

    def test_large_rain_30mm(self):
        """30mm: 5*0.9 + 10*0.75 + 15*0.50 = 4.5+7.5+7.5 = 19.5 effective."""
        result = calc_k_precip(20.0, 30.0)
        expected = 20.0 - 19.5
        assert abs(result - expected) < 0.01


# ---------------------------------------------------------------------------
# calc_et_corrected
# ---------------------------------------------------------------------------

class TestCalcEtCorrected:
    def test_orsk_no_correction(self):
        """Orsk: K_alt=1.0, K_lake=1.0 -> ET_base * Kt."""
        result = calc_et_corrected(22.0, "orsk")
        et_base = lookup_et_base(22.0)  # 4.0
        kt = calc_kt(22.0)              # 1.0
        expected = et_base * kt * 1.0 * 1.0
        assert abs(result - expected) < 0.01
        assert abs(result - 4.0) < 0.01

    def test_cholpon_ata_correction(self):
        """Cholpon-Ata: K_alt=1.12, K_lake=0.92 -> ~1.0304 multiplier."""
        result = calc_et_corrected(22.0, "cholpon_ata")
        et_base = lookup_et_base(22.0)  # 4.0
        kt = calc_kt(22.0)              # 1.0
        expected = et_base * kt * 1.12 * 0.92
        assert abs(result - expected) < 0.01

    def test_cold_temperature(self):
        """Below 5C: ET_base=0, Kt=0 -> result=0."""
        assert calc_et_corrected(3.0, "orsk") == 0.0
        assert calc_et_corrected(3.0, "cholpon_ata") == 0.0

    def test_hot_temperature_orsk(self):
        """35C in Orsk: high ET."""
        result = calc_et_corrected(35.0, "orsk")
        expected = 7.5 * 1.35 * 1.0 * 1.0  # 10.125
        assert abs(result - expected) < 0.01

    def test_unknown_site_defaults_to_1(self):
        """Unknown site_id gets default corrections of 1.0."""
        result = calc_et_corrected(22.0, "unknown_site")
        expected = 4.0 * 1.0 * 1.0 * 1.0
        assert abs(result - expected) < 0.01


# ---------------------------------------------------------------------------
# calc_irrigation_need
# ---------------------------------------------------------------------------

class TestCalcIrrigationNeed:
    def test_no_rain(self):
        """No rain -> full ET corrected need."""
        result = calc_irrigation_need(22.0, 0.0, "orsk")
        expected = calc_et_corrected(22.0, "orsk")
        assert abs(result - expected) < 0.01

    def test_with_rain(self):
        """Rain reduces need."""
        result = calc_irrigation_need(22.0, 3.0, "orsk")
        et_corr = calc_et_corrected(22.0, "orsk")
        # 3mm rain effective: 3*0.9 = 2.7
        expected = et_corr - 2.7
        assert abs(result - expected) < 0.01

    def test_heavy_rain_zero_need(self):
        """Heavy rain -> need = 0."""
        result = calc_irrigation_need(15.0, 50.0, "orsk")
        assert result == 0.0


# ---------------------------------------------------------------------------
# calc_zone_runtime
# ---------------------------------------------------------------------------

class TestCalcZoneRuntime:
    def test_standard_calculation(self):
        """5mm / 10mm/h = 0.5h = 30 min."""
        result = calc_zone_runtime(5.0, 10.0)
        assert abs(result - 30.0) < 0.1

    def test_clamp_minimum(self):
        """Very small need -> clamped to MIN_ZONE_RUNTIME_MIN (2)."""
        result = calc_zone_runtime(0.1, 10.0)
        # 0.1/10*60 = 0.6 min -> clamped to 2
        assert result == MIN_ZONE_RUNTIME_MIN

    def test_clamp_maximum(self):
        """Very large need -> clamped to MAX_ZONE_RUNTIME_MIN (60)."""
        result = calc_zone_runtime(20.0, 10.0)
        # 20/10*60 = 120 min -> clamped to 60
        assert result == MAX_ZONE_RUNTIME_MIN

    def test_zero_need(self):
        """Zero irrigation need -> 0 runtime."""
        assert calc_zone_runtime(0.0, 10.0) == 0.0

    def test_zero_pr(self):
        """Zero precipitation rate -> 0 runtime (safety)."""
        assert calc_zone_runtime(5.0, 0.0) == 0.0

    def test_negative_pr(self):
        """Negative Pr -> 0 runtime."""
        assert calc_zone_runtime(5.0, -1.0) == 0.0

    def test_negative_need(self):
        """Negative need -> 0."""
        assert calc_zone_runtime(-1.0, 10.0) == 0.0

    def test_exact_boundary(self):
        """Exactly at max boundary."""
        result = calc_zone_runtime(10.0, 10.0)
        # 10/10*60 = 60 -> exactly MAX
        assert result == MAX_ZONE_RUNTIME_MIN

    def test_mp_rotator(self):
        """MP Rotator: 4mm / 10mm/h -> 24 min."""
        result = calc_zone_runtime(4.0, 10.0)
        assert abs(result - 24.0) < 0.1


# ---------------------------------------------------------------------------
# calc_cycle_soak
# ---------------------------------------------------------------------------

class TestCalcCycleSoak:
    def test_not_needed_low_pr(self):
        """Pr <= infiltration -> single cycle, no soak."""
        result = calc_cycle_soak(30.0, 10.0)
        assert len(result) == 1
        assert result[0]["run_min"] == 30.0
        assert result[0]["soak_min"] == 0

    def test_not_needed_equal_pr(self):
        """Pr == infiltration -> single cycle."""
        result = calc_cycle_soak(20.0, 15.0)
        assert len(result) == 1
        assert result[0]["run_min"] == 20.0
        assert result[0]["soak_min"] == 0

    def test_pro_fixed_high_pr(self):
        """Pro Fixed at 40mm/h, infiltration 15mm/h -> needs cycle-soak."""
        result = calc_cycle_soak(20.0, 40.0)
        assert len(result) > 1
        # Each run should be <= CYCLE_SOAK_MAX_RUN_MIN
        for cycle in result:
            assert cycle["run_min"] <= CYCLE_SOAK_MAX_RUN_MIN + 0.1
        # All but last should have soak
        for cycle in result[:-1]:
            assert cycle["soak_min"] == CYCLE_SOAK_PAUSE_MIN
        # Last cycle: no soak
        assert result[-1]["soak_min"] == 0
        # Total run time should equal original
        total = sum(c["run_min"] for c in result)
        assert abs(total - 20.0) < 0.1

    def test_custom_infiltration(self):
        """Custom max_infiltration_mm_h."""
        result = calc_cycle_soak(15.0, 20.0, max_infiltration_mm_h=10.0)
        assert len(result) > 1

    def test_short_runtime_high_pr(self):
        """Short runtime: may fit in one cycle even with high Pr."""
        result = calc_cycle_soak(5.0, 40.0)
        # 5 min < 8 min max_run -> single cycle
        assert len(result) == 1
        assert result[0]["run_min"] == 5.0
        assert result[0]["soak_min"] == 0

    def test_max_run_calculation(self):
        """Verify max_run = min(infiltration/Pr * 60, CYCLE_SOAK_MAX_RUN_MIN)."""
        # Pr=40, infiltration=15: (15/40)*60 = 22.5 min, but capped at 8
        result = calc_cycle_soak(24.0, 40.0)
        for cycle in result[:-1]:
            assert cycle["run_min"] <= CYCLE_SOAK_MAX_RUN_MIN + 0.1

    def test_returns_list_of_dicts(self):
        """Return type check."""
        result = calc_cycle_soak(10.0, 10.0)
        assert isinstance(result, list)
        assert len(result) >= 1
        assert "run_min" in result[0]
        assert "soak_min" in result[0]
