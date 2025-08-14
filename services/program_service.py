from typing import Any, Dict, List, Optional
from database import db


def list_programs() -> List[Dict[str, Any]]:
    return db.get_programs()


def get_program(program_id: int) -> Optional[Dict[str, Any]]:
    return db.get_program(program_id)


def create_program(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return db.create_program(payload)


def update_program(program_id: int, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return db.update_program(program_id, payload)


def delete_program(program_id: int) -> bool:
    return db.delete_program(program_id)


def check_conflicts(program_id: Optional[int], time: str, zones: List[int], days: List[int]) -> List[Dict[str, Any]]:
    return db.check_program_conflicts(program_id, time, zones, days)


