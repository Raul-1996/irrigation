from typing import Dict, Any
from irrigation_scheduler import get_scheduler


def reschedule_program(program: Dict[str, Any]) -> None:
    scheduler = get_scheduler()
    if scheduler:
        scheduler.schedule_program(program['id'], program)


def cancel_program(program_id: int) -> None:
    scheduler = get_scheduler()
    if scheduler:
        scheduler.cancel_program(program_id)


