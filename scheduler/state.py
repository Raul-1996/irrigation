#!/usr/bin/env python3
"""
State management mixin: postpones, active zones, active programs.
"""
import json
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)


class StateMixin:
    """Mixin for state management on IrrigationScheduler."""

    @staticmethod
    def _parse_dt(s: Optional[str]) -> Optional[datetime]:
        if not s:
            return None
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
            try:
                return datetime.strptime(s, fmt)
            except (ValueError, TypeError, KeyError) as e:
                logger.debug("Exception in _parse_dt: %s", e)
                continue
        return None

    def clear_expired_postpones(self) -> None:
        """Сбрасывает отложенный полив для зон, у которых срок истек."""
        try:
            zones = self.db.get_zones()
            now = datetime.now()
            expired: List[int] = []
            for z in zones:
                pu = z.get('postpone_until')
                if not pu:
                    continue
                dt = self._parse_dt(pu)
                if dt is None or now >= dt:
                    expired.append(int(z['id']))
            for zone_id in expired:
                try:
                    self.db.update_zone_postpone(zone_id, None, None)
                    try:
                        self.db.add_log('postpone_expired', json.dumps({'zone': zone_id}))
                    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
                        logger.debug("Handled exception in clear_expired_postpones: %s", e)
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
                    logger.error(f"Не удалось сбросить отложку для зоны {zone_id}: {e}")
            if expired:
                logger.info(f"Сброшены истекшие отложки зон: {expired}")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.error(f"Ошибка очистки истекших отложек: {e}")

    def get_active_programs(self) -> Dict[int, Dict[str, Any]]:
        return {pid: {'job_ids': jobs} for pid, jobs in self.program_jobs.items()}

    def get_active_zones(self) -> Dict[int, datetime]:
        return self.active_zones.copy()
