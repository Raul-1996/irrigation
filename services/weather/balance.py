"""Virtual water-balance coefficient (H2) — normalised rolling ET-deficit.

Single responsibility: once per night, pull the last few *completed* days of
Open-Meteo history, compute a daily ET₀ deficit (need − effective rain), keep a
short rolling window of it, maintain a long-horizon EMA "climate norm" of ET₀,
and turn the window/norm ratio into a watering multiplier (``coef``) cached in
``settings``. The irrigation path reads only the cached integer — no maths runs
while a zone is firing.

Why a *relative* model (deficit vs climate norm), not a mm soil-water balance:
the controller has no soil-moisture / rain sensor and we deliberately do not
translate watering minutes into mm replenishment (no sprinkler-rate data). The
ratio ``D_win / Norm_win`` is unit-free and maps directly onto "water more / less
than the base zone time" — honest by construction. See
``specs/weather-h2-water-balance-design.md``.

Design invariants (from the adversarial review — do NOT regress):
    * Rain is a *soft credit* only, via ``supply_d``. There is no hard reset and
      no ``rain_reset_mm`` parameter (a false 18mm forecast must not wipe the
      window). The lower ``clamp(D_win, 0, ∞)`` provides saturation.
    * The norm is an EMA over ``norm_window_days`` (~30), kept SEPARATE from the
      short deficit window (~3). Same-window normalisation degenerates to ~100.
    * If the buffer is shorter than ``window_days`` full days OR the norm is not
      ready → ``coef = 100`` (neutral). The formula is NOT applied (no 50 on
      cold start).
    * History days are *completed* days only (an offset back from "today"); the
      current partial day is never used.

Everything here is best-effort: any exception leaves the previous ``coef_cached``
in place (graceful degradation) and never propagates to the scheduler.
"""

import json
import logging
import sqlite3
from datetime import date, datetime

logger = logging.getLogger(__name__)


# --- Setting keys (key-value ``settings`` table) -----------------------------
_K_ENABLED = "weather.balance.enabled"
_K_WINDOW_DAYS = "weather.balance.window_days"
_K_NORM_WINDOW_DAYS = "weather.balance.norm_window_days"
_K_COEF_MIN = "weather.balance.coef_min"
_K_COEF_MAX = "weather.balance.coef_max"
_K_KC = "weather.balance.kc"
_K_INTERCEPT_MM = "weather.balance.intercept_mm"
_K_STALE_FALLBACK_DAYS = "weather.balance.stale_fallback_days"
_K_DEFICIT_BUFFER = "weather.balance.deficit_buffer"
_K_LAST_RECALC_DATE = "weather.balance.last_recalc_date"
_K_COEF_CACHED = "weather.balance.coef_cached"
_K_ET0_NORM_DAILY = "weather.balance.et0_norm_daily"
_K_NORM_LAST_DAY = "weather.balance.norm_last_day"

# --- Defaults (mirror the migration; used when a key is missing) -------------
_DEFAULT_WINDOW_DAYS = 3
_DEFAULT_NORM_WINDOW_DAYS = 30
_DEFAULT_COEF_MIN = 50
_DEFAULT_COEF_MAX = 150
_DEFAULT_KC = 1.0
_DEFAULT_INTERCEPT_MM = 4.0
_NEUTRAL_COEF = 100

# Minimum history length before the formula may run at all (review BLOCKER 3/4).
_MIN_HISTORY_DAYS = 7

# How many past days to request from Open-Meteo. Must cover the norm horizon
# plus a margin for the completed-day offset and any API edge trimming.
_HISTORY_FETCH_DAYS = 35


def _read_settings(conn) -> dict:
    """Read all balance params from ``settings`` with typed defaults.

    A single connection is reused for the whole recalc so the read is cheap and
    consistent with the write that follows.
    """
    keys = [
        _K_WINDOW_DAYS,
        _K_NORM_WINDOW_DAYS,
        _K_COEF_MIN,
        _K_COEF_MAX,
        _K_KC,
        _K_INTERCEPT_MM,
        _K_DEFICIT_BUFFER,
        _K_LAST_RECALC_DATE,
        _K_ET0_NORM_DAILY,
        _K_NORM_LAST_DAY,
    ]
    raw: dict[str, str] = {}
    conn.row_factory = sqlite3.Row
    for key in keys:
        cur = conn.execute("SELECT value FROM settings WHERE key = ? LIMIT 1", (key,))
        row = cur.fetchone()
        if row and row["value"] is not None:
            raw[key] = str(row["value"])

    def _as_int(key: str, default: int) -> int:
        try:
            return int(float(raw[key]))
        except (KeyError, ValueError, TypeError):
            return default

    def _as_float_opt(key: str):
        try:
            val = raw[key]
            if val is None or val == "":
                return None
            return float(val)
        except (KeyError, ValueError, TypeError):
            return None

    return {
        "window_days": max(1, _as_int(_K_WINDOW_DAYS, _DEFAULT_WINDOW_DAYS)),
        "norm_window_days": max(1, _as_int(_K_NORM_WINDOW_DAYS, _DEFAULT_NORM_WINDOW_DAYS)),
        "coef_min": _as_int(_K_COEF_MIN, _DEFAULT_COEF_MIN),
        "coef_max": _as_int(_K_COEF_MAX, _DEFAULT_COEF_MAX),
        "kc": (lambda v: v if v is not None else _DEFAULT_KC)(_as_float_opt(_K_KC)),
        "intercept_mm": (lambda v: v if v is not None else _DEFAULT_INTERCEPT_MM)(_as_float_opt(_K_INTERCEPT_MM)),
        "deficit_buffer": raw.get(_K_DEFICIT_BUFFER, ""),
        "last_recalc_date": raw.get(_K_LAST_RECALC_DATE, ""),
        "et0_norm_daily": _as_float_opt(_K_ET0_NORM_DAILY),
        "norm_last_day": raw.get(_K_NORM_LAST_DAY, ""),
    }


def compute_deficit_day(et0_fact: float, precip_fact: float, kc: float, intercept_mm: float) -> float:
    """Daily ET deficit: ``need − supply`` where supply is intercepted rain.

    ``need = ET0 × Kc`` (Kc=1.0 in v1, so zones cancel out). ``supply = P_eff =
    max(0, P − intercept)`` — rain below the canopy-interception threshold does
    not reach the soil. Rain is not multiplied by Kc (physically it suppresses
    demand independent of crop coefficient). May be negative on a wet day; that
    negative is what lets a real downpour pull the window sum down (soft credit).
    """
    need = et0_fact * kc
    supply = max(0.0, precip_fact - intercept_mm)
    return need - supply


def apply_rain_gate(precip: float, rain_state):
    """Hook for a future physical rain sensor — currently a no-op.

    With no rain sensor (``rain_state is None``) the Open-Meteo precipitation is
    returned unchanged. When a sensor is wired in, this is where its truth-table
    correction of ``P_eff`` will live. Kept out of the core so the formula stays
    sensor-agnostic.
    """
    return precip


def _parse_buffer(raw: str) -> list[dict]:
    """Decode the JSON ring buffer ``[{"date","deficit"}, ...]`` defensively."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            out = []
            for item in data:
                if isinstance(item, dict) and "date" in item and "deficit" in item:
                    out.append({"date": str(item["date"]), "deficit": float(item["deficit"])})
            return out
    except (json.JSONDecodeError, ValueError, TypeError):
        logger.debug("water-balance: deficit_buffer parse failed, resetting")
    return []


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _compute_coef(window_sum: float, norm_window: float, coef_min: int, coef_max: int) -> int:
    """``clamp(100 × D_win / Norm_win, coef_min, coef_max)`` with zero-guard."""
    if norm_window <= 0:  # guard division by zero (review BLOCKER 3)
        return _NEUTRAL_COEF
    raw = 100.0 * window_sum / norm_window
    return round(_clamp(raw, coef_min, coef_max))


def _build_history_rows(et0_list, precip_list, time_list, today: date) -> list[dict]:
    """Zip the daily arrays into ``[{date, et0, precip}]`` for *completed* days.

    Drops the current partial day (``>= today``) and any row with a missing ET₀
    value. Open-Meteo returns chronologically ascending dates.
    """
    rows = []
    n = min(len(et0_list), len(precip_list), len(time_list))
    for i in range(n):
        d_str = str(time_list[i])
        try:
            d = datetime.strptime(d_str, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        if d >= today:  # only completed days
            continue
        et0 = et0_list[i]
        if et0 is None:
            continue
        try:
            et0_f = float(et0)
        except (ValueError, TypeError):
            continue
        try:
            precip_f = float(precip_list[i]) if precip_list[i] is not None else 0.0
        except (ValueError, TypeError):
            precip_f = 0.0
        rows.append({"date": d_str, "et0": et0_f, "precip": precip_f})
    return rows


def _bootstrap_norm(history_rows: list[dict], norm_window_days: int) -> float | None:
    """Seed the EMA norm from the average ET₀ over the available history.

    Returns ``None`` if there is not enough history to trust a norm
    (``_MIN_HISTORY_DAYS``), which forces ``coef=100`` upstream.
    """
    if len(history_rows) < _MIN_HISTORY_DAYS:
        return None
    sample = history_rows[-norm_window_days:] if norm_window_days > 0 else history_rows
    et0_vals = [r["et0"] for r in sample]
    if not et0_vals:
        return None
    return sum(et0_vals) / len(et0_vals)


def read_cached_coef(db_path: str) -> int:
    """Return the cached water-balance coefficient (int), defaulting to 100.

    This is the hot-path read used while a zone is firing — a single keyed
    settings lookup, no computation.
    """
    try:
        with sqlite3.connect(db_path, timeout=5) as conn:
            cur = conn.execute("SELECT value FROM settings WHERE key = ? LIMIT 1", (_K_COEF_CACHED,))
            row = cur.fetchone()
            if row and row[0] is not None:
                return int(float(row[0]))
    except (sqlite3.Error, ValueError, TypeError) as e:
        logger.debug("water-balance: read_cached_coef failed: %s", e)
    return _NEUTRAL_COEF


def has_computed(db_path: str) -> bool:
    """True if the nightly balance job has produced a coefficient at least once.

    The UI layer uses this to decide whether to surface the balance "second
    opinion": a fresh-install default ``coef_cached=100`` is indistinguishable
    from a genuinely computed 100, so we key off ``last_recalc_date`` being set
    (written only by an actual recalc). Lets shadow mode (flag off) still show
    the second opinion without balance steering watering.
    """
    try:
        with sqlite3.connect(db_path, timeout=5) as conn:
            cur = conn.execute("SELECT value FROM settings WHERE key = ? LIMIT 1", (_K_LAST_RECALC_DATE,))
            row = cur.fetchone()
            return bool(row and row[0])
    except (sqlite3.Error, ValueError, TypeError):
        return False


def recalc_balance(db_path: str) -> dict | None:
    """Recompute the water-balance coefficient from Open-Meteo history.

    Best-effort: returns the computed summary dict on success, ``None`` on any
    failure or no-op (idempotent same-day skip). Never raises — the caller is a
    scheduler job that must not crash on a bad night.

    Flow: idempotency check → fetch completed-day history → daily deficits →
    update EMA norm (bootstrap on first run) → roll the deficit window → coef →
    write settings (``coef_cached`` LAST for consistency) + audit-log row.
    """
    try:
        from services.weather.cache import get_location
        from services.weather.client import fetch_history

        today = date.today()
        today_str = today.isoformat()

        with sqlite3.connect(db_path, timeout=5) as conn:
            cfg = _read_settings(conn)

        # Idempotency: one recalc per calendar day (review #6 — keep it simple).
        if cfg["last_recalc_date"] == today_str:
            logger.debug("water-balance: already recalculated for %s, skipping", today_str)
            return None

        loc = get_location(db_path)
        if not loc:
            logger.debug("water-balance: location not configured, skipping")
            return None

        payload = fetch_history(loc["latitude"], loc["longitude"], past_days=_HISTORY_FETCH_DAYS)
        if not payload:
            logger.info("water-balance: history fetch returned nothing, keeping previous coef")
            return None

        daily = payload.get("daily", {}) if isinstance(payload, dict) else {}
        history_rows = _build_history_rows(
            daily.get("et0_fao_evapotranspiration", []),
            daily.get("precipitation_sum", []),
            daily.get("time", []),
            today,
        )

        window_days = cfg["window_days"]
        norm_window_days = cfg["norm_window_days"]
        kc = cfg["kc"]
        intercept_mm = cfg["intercept_mm"]

        # Idempotency per *completed day*: a manual recalc clears
        # ``last_recalc_date`` to bypass the same-calendar-day skip, so guard the
        # one-shot operations (EMA fold + audit-log row) against folding the same
        # latest completed day twice on repeated clicks (review #2). The nightly
        # path is already day-idempotent; this protects the manual trigger.
        latest_day_str = history_rows[-1]["date"] if history_rows else None
        already_folded = latest_day_str is not None and latest_day_str == cfg["norm_last_day"]

        # --- EMA climate norm (BLOCKER 3): separate long horizon ------------
        norm = cfg["et0_norm_daily"]
        if norm is None or norm <= 0:
            norm = _bootstrap_norm(history_rows, norm_window_days)  # may stay None
        elif history_rows and not already_folded:
            # Advance the EMA by the latest completed day only (once per day).
            alpha = 2.0 / (norm_window_days + 1)
            latest_et0 = history_rows[-1]["et0"]
            norm = alpha * latest_et0 + (1.0 - alpha) * norm

        # --- Rolling deficit window -----------------------------------------
        last_window = history_rows[-window_days:] if window_days > 0 else []
        buffer_out = [
            {"date": r["date"], "deficit": round(compute_deficit_day(r["et0"], r["precip"], kc, intercept_mm), 4)}
            for r in last_window
        ]
        window_sum = sum(item["deficit"] for item in buffer_out)
        window_sum = max(0.0, window_sum)  # lower clamp = saturation (BLOCKER 2)

        # --- Coefficient: only with a full window AND a ready norm (BLOCKER 4)
        # Norm_win is summed over the SAME number of days as the deficit window
        # (units must match D_win's window-day sum). The long horizon lives only
        # in how ``norm`` (the *daily* climate norm) is derived — EMA over
        # norm_window_days — NOT in the denominator's day count.
        norm_ready = norm is not None and norm > 0 and len(history_rows) >= _MIN_HISTORY_DAYS
        full_window = len(last_window) >= window_days
        if norm_ready and full_window:
            norm_window = norm * window_days * kc
            coef = _compute_coef(window_sum, norm_window, cfg["coef_min"], cfg["coef_max"])
        else:
            coef = _NEUTRAL_COEF

        # --- Persist (coef_cached LAST — its presence implies the rest) -----
        latest_day = history_rows[-1] if history_rows else None
        deficit_today = round(buffer_out[-1]["deficit"], 4) if buffer_out else 0.0
        with sqlite3.connect(db_path, timeout=5) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                (_K_DEFICIT_BUFFER, json.dumps(buffer_out)),
            )
            if norm is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                    (_K_ET0_NORM_DAILY, str(round(norm, 4))),
                )
            if latest_day_str is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                    (_K_NORM_LAST_DAY, latest_day_str),
                )
            conn.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                (_K_LAST_RECALC_DATE, today_str),
            )
            # coef_cached written LAST: guarantees buffer/date/norm are in place
            # before the hot path can observe the new coefficient (review #6).
            conn.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                (_K_COEF_CACHED, str(coef)),
            )
            # Audit-log row for shadow-mode forecast/fact review — once per
            # completed day (a repeated manual recalc must not duplicate it).
            if not already_folded:
                conn.execute(
                    "INSERT INTO weather_balance_log "
                    "(date, et0_fact, et0_norm, precip_fact, precip_eff, deficit_day, deficit_window, "
                    "coefficient, created_at) "
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime("now"))',
                    (
                        latest_day["date"] if latest_day else today_str,
                        round(latest_day["et0"], 4) if latest_day else None,
                        round(norm, 4) if norm is not None else None,
                        round(latest_day["precip"], 4) if latest_day else None,
                        round(max(0.0, latest_day["precip"] - intercept_mm), 4) if latest_day else None,
                        deficit_today,
                        round(window_sum, 4),
                        coef,
                    ),
                )
            conn.commit()

        logger.info(
            "water-balance: coef=%d (D_win=%.2f, norm=%.2f, history=%dd, window=%dd)",
            coef,
            window_sum,
            norm if norm is not None else 0.0,
            len(history_rows),
            len(last_window),
        )
        return {
            "coefficient": coef,
            "deficit_window": round(window_sum, 4),
            "et0_norm_daily": round(norm, 4) if norm is not None else None,
            "history_days": len(history_rows),
            "deficit_buffer": buffer_out,
            "norm_ready": norm_ready,
            "full_window": full_window,
        }
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError) as e:
        # Best-effort: previous coef_cached stays in place (graceful degradation).
        logger.warning("water-balance: recalc failed, keeping previous coef: %s", e)
        return None
