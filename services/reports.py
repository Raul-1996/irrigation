from datetime import datetime, timedelta
from typing import Literal
from database import db

def _period_to_range(period: str) -> tuple:
    now = datetime.now()
    if period == 'today':
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == 'yesterday':
        d = now - timedelta(days=1)
        start = d.replace(hour=0, minute=0, second=0, microsecond=0)
        now = d.replace(hour=23, minute=59, second=59, microsecond=0)
    elif period == '7':
        start = now - timedelta(days=7)
    elif period == '30':
        start = now - timedelta(days=30)
    else:
        start = now - timedelta(days=7)
    return (start, now)

def build_report_text(period: str = 'today', fmt: Literal['brief','full'] = 'brief') -> str:
    # Using existing water_usage helper for simplicity
    days = 1 if period in ('today','yesterday') else (7 if period=='7' else (30 if period=='30' else 7))
    stats = db.get_water_statistics(days=days)
    lines = []
    lines.append(f"Отчёт за {period}: всего воды {stats['total_liters']} л, среднедневной {stats['avg_daily']} л")
    if fmt == 'brief':
        top = stats['zone_usage'][:3]
        if top:
            lines.append('Топ зон:')
            for it in top:
                lines.append(f"- {it['name']}: {round(it['liters'] or 0,2)} л")
    else:
        for it in stats['zone_usage']:
            lines.append(f"- {it['name']}: {round(it['liters'] or 0,2)} л")
    return '\n'.join(lines)

