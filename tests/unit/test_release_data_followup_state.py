"""Generation-fence regressions for physical zone observations."""

from __future__ import annotations

from unittest.mock import Mock, patch

from services.zones_state import update_zone_state_internal


def test_internal_transition_never_retries_across_a_new_physical_observation():
    fake_db = Mock()
    authorised = {
        "id": 7,
        "version": 3,
        "state": "stopping",
        "commanded_state": "off",
        "observed_state": "unconfirmed",
        "command_id": "off-generation",
        "sequence_id": None,
        "watering_start_time": "2026-07-23 06:00:00",
        "mqtt_server_id": 1,
        "topic": "/relay/K1",
        "group_id": 1,
    }
    fresh_physical_on = {**authorised, "version": 4, "observed_state": "on"}
    fake_db.update_zone_versioned.return_value = (False, fresh_physical_on)

    applied, snapshot = update_zone_state_internal(
        7,
        {"state": "off", "command_id": None},
        snapshot=authorised,
        audit_reason="stale_off_confirmation",
        db=fake_db,
    )

    assert applied is False
    assert snapshot == fresh_physical_on
    assert fake_db.update_zone_versioned.call_count == 1


def _sse_zone(*, state: str = "stopping") -> dict:
    return {
        "id": 7,
        "version": 3,
        "state": state,
        "commanded_state": "off",
        "observed_state": "unconfirmed",
        "command_id": "off-generation",
        "watering_start_time": "2026-07-23 06:00:00",
        "topic": "/relay/K1",
        "mqtt_server_id": 1,
    }


def test_sse_fresh_off_has_no_terminal_side_effect_after_rejected_generation_cas():
    from services import sse_hub

    database = Mock()
    database.get_zone.return_value = _sse_zone()
    verifier = Mock()
    verifier.command_registered_at.return_value = None
    verifier.apply_live_confirmation.return_value = False

    with (
        patch.object(sse_hub, "_db", database),
        patch.object(sse_hub, "state_verifier", verifier),
        patch.object(sse_hub, "_app_config", {}),
        patch.object(sse_hub, "_SSE_HUB_ZONE_TOPICS", {1: {"/relay/K1": [7]}}),
        patch.object(sse_hub, "_SSE_HUB_MV_TOPICS", {}),
        patch.object(sse_hub, "_SSE_HUB_MQTT", {}),
        patch.object(sse_hub, "_audited_zone_update", return_value=False) as persist,
        patch.object(sse_hub, "_finish_observed_run") as finish,
        patch.object(sse_hub, "_cancel_zone_safety_jobs") as cancel,
        patch.object(sse_hub, "mark_zone_stopped") as mark_stopped,
        patch.object(sse_hub, "broadcast") as broadcast,
    ):
        sse_hub._process_mqtt_message(1, None, "/relay/K1", "0", retained=False)

    verifier.apply_live_confirmation.assert_called_once_with(
        7,
        "off",
        received_at=None,
        db_instance=database,
        scheduler_getter=sse_hub._get_scheduler_fn,
    )
    persist.assert_not_called()
    finish.assert_not_called()
    cancel.assert_not_called()
    mark_stopped.assert_not_called()
    broadcast.assert_not_called()


def test_sse_confirmed_off_preserves_fault_and_closes_its_run_failed_after_cas():
    from services import sse_hub

    fault = _sse_zone(state="fault")
    confirmed = {**fault, "version": 4, "observed_state": "off"}
    database = Mock()
    database.get_zone.side_effect = [fault, fault, confirmed]
    verifier = Mock()
    verifier.command_registered_at.return_value = None
    verifier.apply_live_confirmation.return_value = True

    with (
        patch.object(sse_hub, "_db", database),
        patch.object(sse_hub, "state_verifier", verifier),
        patch.object(sse_hub, "_app_config", {}),
        patch.object(sse_hub, "_SSE_HUB_ZONE_TOPICS", {1: {"/relay/K1": [7]}}),
        patch.object(sse_hub, "_SSE_HUB_MV_TOPICS", {}),
        patch.object(sse_hub, "_SSE_HUB_MQTT", {}),
        patch.object(
            sse_hub,
            "_audited_zone_update",
            return_value=True,
        ) as persist,
        patch.object(
            sse_hub,
            "_finish_observed_run",
            return_value=True,
        ) as finish,
        patch.object(
            sse_hub,
            "_cancel_zone_safety_jobs",
            return_value=True,
        ),
        patch.object(sse_hub, "mark_zone_stopped"),
        patch.object(sse_hub, "broadcast"),
    ):
        sse_hub._process_mqtt_message(1, None, "/relay/K1", "0", retained=False)

    verifier.apply_live_confirmation.assert_called_once_with(
        7,
        "off",
        received_at=None,
        db_instance=database,
        scheduler_getter=sse_hub._get_scheduler_fn,
    )
    persist.assert_not_called()
    finish.assert_not_called()
