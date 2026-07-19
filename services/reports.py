import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Literal

from database import db

logger = logging.getLogger(__name__)


def _period_to_range(period: str) -> tuple:
    now = datetime.now()
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    elif period == "yesterday":
        end = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = end - timedelta(days=1)
    elif period == "7":
        start = now - timedelta(days=7)
        end = now
    elif period == "30":
        start = now - timedelta(days=30)
        end = now
    else:
        start = now - timedelta(days=7)
        end = now
    return (start, end)


def _as_utc_sql_timestamp(value: datetime) -> str:
    """Convert a naive controller-local boundary to stored SQLite UTC."""
    return value.astimezone(UTC).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


def _calendar_day_statistics(start: datetime, end: datetime) -> dict:
    """Aggregate one controller-local day from authoritative run history.

    Current production writes measured liters to ``zone_runs``. The older
    ``water_usage`` table is retained as a fallback for installations that
    contain legacy rows, but is never added on top of zone-runs data because
    the two sources can overlap.
    """
    report_date = start.strftime("%Y-%m-%d")
    start_local = start.strftime("%Y-%m-%d %H:%M:%S")
    end_local = end.strftime("%Y-%m-%d %H:%M:%S")
    start_utc = _as_utc_sql_timestamp(start)
    end_utc = _as_utc_sql_timestamp(end)
    try:
        with sqlite3.connect(db.db_path, timeout=1.0) as conn:
            conn.row_factory = sqlite3.Row
            runs_summary = conn.execute(
                """
                SELECT COUNT(*) AS run_count,
                       COUNT(total_liters) AS measured_count,
                       COALESCE(SUM(total_liters), 0) AS total_liters
                FROM zone_runs
                WHERE start_utc IS NOT NULL
                  AND start_utc >= ? AND start_utc < ?
                  AND group_id != 999
                """,
                (start_local, end_local),
            ).fetchone()
            run_count = int(runs_summary["run_count"] or 0)
            measured_count = int(runs_summary["measured_count"] or 0)
            if measured_count:
                usage_rows = conn.execute(
                    """
                    SELECT zr.zone_id, z.name, SUM(zr.total_liters) AS liters
                    FROM zone_runs AS zr
                    LEFT JOIN zones AS z ON z.id = zr.zone_id
                    WHERE zr.start_utc IS NOT NULL
                      AND zr.start_utc >= ? AND zr.start_utc < ?
                      AND zr.group_id != 999
                      AND zr.total_liters IS NOT NULL
                    GROUP BY zr.zone_id, z.name
                    ORDER BY liters DESC
                    """,
                    (start_local, end_local),
                ).fetchall()
                total_liters = round(float(runs_summary["total_liters"] or 0), 2)
                source = "zone_runs"
                partial = measured_count < run_count
                has_data = True
            else:
                legacy_summary = conn.execute(
                    """
                    SELECT COUNT(*) AS row_count,
                           COALESCE(SUM(liters), 0) AS total_liters
                    FROM water_usage
                    WHERE timestamp >= ? AND timestamp < ?
                    """,
                    (start_utc, end_utc),
                ).fetchone()
                legacy_count = int(legacy_summary["row_count"] or 0)
                usage_rows = conn.execute(
                    """
                    SELECT w.zone_id, z.name, SUM(w.liters) AS liters
                    FROM water_usage AS w
                    LEFT JOIN zones AS z ON z.id = w.zone_id
                    WHERE w.timestamp >= ? AND w.timestamp < ?
                    GROUP BY w.zone_id, z.name
                    ORDER BY liters DESC
                    """,
                    (start_utc, end_utc),
                ).fetchall()
                total_liters = round(float(legacy_summary["total_liters"] or 0), 2)
                source = "water_usage" if legacy_count else "none"
                partial = False
                has_data = bool(legacy_count)
        return {
            "total_liters": total_liters,
            "avg_daily": total_liters,
            "zone_usage": [dict(row) for row in usage_rows],
            "period_days": 1,
            "date": report_date,
            "source": source,
            "partial": partial,
            "has_data": has_data,
        }
    except (AttributeError, OSError, sqlite3.Error, TypeError, ValueError) as error:
        # This function is used by the frequently-polled status endpoint; do
        # not amplify one persistent DB failure into a high-volume disk log.
        logger.debug("Calendar water report failed: %s", type(error).__name__)
        return {
            "total_liters": 0,
            "avg_daily": 0,
            "zone_usage": [],
            "period_days": 1,
            "date": report_date,
            "source": "unavailable",
            "partial": False,
            "has_data": False,
            "error_code": "WATER_REPORT_UNAVAILABLE",
        }


def get_calendar_water_report(period: Literal["today", "yesterday"] = "today") -> dict:
    """Return structured water totals for one controller-local calendar day."""
    normalized = period if period in ("today", "yesterday") else "today"
    return _calendar_day_statistics(*_period_to_range(normalized))


def build_report_text(period: str = "today", fmt: Literal["brief", "full"] = "brief") -> str:
    if period in ("today", "yesterday"):
        stats = get_calendar_water_report(period)
    else:
        days = 7 if period == "7" else (30 if period == "30" else 7)
        stats = db.get_water_statistics(days=days)
    lines = []
    lines.append(f"Отчёт за {period}: всего воды {stats['total_liters']} л, среднедневной {stats['avg_daily']} л")
    if fmt == "brief":
        top = stats["zone_usage"][:3]
        if top:
            lines.append("Топ зон:")
            for it in top:
                lines.append(f"- {it['name']}: {round(it['liters'] or 0, 2)} л")
    else:
        for it in stats["zone_usage"]:
            lines.append(f"- {it['name']}: {round(it['liters'] or 0, 2)} л")
    return "\n".join(lines)
