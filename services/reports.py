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
    """Aggregate one local calendar day using an end-exclusive UTC range."""
    start_utc = _as_utc_sql_timestamp(start)
    end_utc = _as_utc_sql_timestamp(end)
    try:
        with sqlite3.connect(db.db_path, timeout=1.0) as conn:
            conn.row_factory = sqlite3.Row
            total_row = conn.execute(
                """
                SELECT COALESCE(SUM(liters), 0) AS total_liters
                FROM water_usage
                WHERE timestamp >= ? AND timestamp < ?
                """,
                (start_utc, end_utc),
            ).fetchone()
            usage_rows = conn.execute(
                """
                SELECT z.name, SUM(w.liters) AS liters
                FROM water_usage AS w
                LEFT JOIN zones AS z ON z.id = w.zone_id
                WHERE w.timestamp >= ? AND w.timestamp < ?
                GROUP BY w.zone_id, z.name
                ORDER BY liters DESC
                """,
                (start_utc, end_utc),
            ).fetchall()
        total_liters = round(float(total_row["total_liters"] or 0), 2)
        return {
            "total_liters": total_liters,
            "avg_daily": total_liters,
            "zone_usage": [dict(row) for row in usage_rows],
            "period_days": 1,
        }
    except (AttributeError, OSError, sqlite3.Error, TypeError, ValueError) as error:
        logger.error("Calendar water report failed: %s", type(error).__name__)
        return {"total_liters": 0, "avg_daily": 0, "zone_usage": [], "period_days": 1}


def build_report_text(period: str = "today", fmt: Literal["brief", "full"] = "brief") -> str:
    if period in ("today", "yesterday"):
        stats = _calendar_day_statistics(*_period_to_range(period))
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
