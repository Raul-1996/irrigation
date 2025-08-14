from typing import Any, Dict, List, Optional
from database import db


def list_groups() -> List[Dict[str, Any]]:
    return db.get_groups()


def create_group(name: str) -> Optional[Dict[str, Any]]:
    return db.create_group(name)


def update_group(group_id: int, name: str) -> bool:
    return db.update_group(group_id, name)


def delete_group(group_id: int) -> bool:
    return db.delete_group(group_id)


