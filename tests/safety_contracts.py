"""Strict safety-contract fakes shared by legacy API regression tests."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from unittest.mock import MagicMock, patch


def complete_group_stop_scheduler(db) -> MagicMock:
    """Return a scheduler fake with an exact, physically-complete OFF partition."""

    scheduler = MagicMock()

    def cancel_group_jobs(group_id: int, **_kwargs) -> dict[str, object]:
        normalized_group_id = int(group_id)
        stopped = sorted(
            int(zone["id"]) for zone in (db.get_zones() or []) if int(zone.get("group_id") or 0) == normalized_group_id
        )
        return {
            "success": True,
            "group_id": normalized_group_id,
            "aggregate_valid": True,
            "stopped": stopped,
            "unresolved": [],
            "unverified_zone_ids": [],
            "retry_scheduled": False,
        }

    scheduler.cancel_group_jobs.side_effect = cancel_group_jobs
    scheduler.scheduler.get_jobs.return_value = []
    return scheduler


@contextmanager
def confirmed_group_stop(db, *get_scheduler_targets: str):
    """Patch one or more imported ``get_scheduler`` call sites consistently."""

    targets = get_scheduler_targets or ("irrigation_scheduler.get_scheduler",)
    scheduler = complete_group_stop_scheduler(db)
    with ExitStack() as stack:
        for target in targets:
            stack.enter_context(patch(target, return_value=scheduler))
        yield scheduler
