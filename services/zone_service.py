from typing import Any, Dict, List, Optional
from database import db


def list_zones() -> List[Dict[str, Any]]:
    return db.get_zones()


def get_zone(zone_id: int) -> Optional[Dict[str, Any]]:
    return db.get_zone(zone_id)


def create_zone(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return db.create_zone(payload)


def update_zone(zone_id: int, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return db.update_zone(zone_id, payload)


def delete_zone(zone_id: int) -> bool:
    return db.delete_zone(zone_id)


