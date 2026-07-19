"""Concurrency regressions for group rain-configuration transactions."""

import threading
from unittest.mock import patch


def _group(app, name: str) -> dict:
    return app.db.create_group(name)


def _admin_client(app):
    client = app.test_client()
    with client.session_transaction() as session:
        session["logged_in"] = True
        session["role"] = "admin"
    return client


def _available_to_another_thread(lock: threading.RLock) -> bool:
    """Return whether a fresh thread can acquire ``lock`` immediately."""
    result = []
    completed = threading.Event()

    def contend() -> None:
        acquired = lock.acquire(blocking=False)
        result.append(acquired)
        if acquired:
            lock.release()
        completed.set()

    contender = threading.Thread(target=contend)
    contender.start()
    assert completed.wait(timeout=1), "rain-lock contender did not complete"
    contender.join(timeout=1)
    assert not contender.is_alive()
    return result[0]


def test_rain_transaction_lock_is_outer_to_group_lock(app):
    from services.locks import group_lock
    from services.monitors import rain_config_transaction_lock as real_lock_factory
    from services.monitors import rain_monitor

    group = _group(app, "Rain lock order")
    rain_lock = real_lock_factory()
    rain_lock_requested = threading.Event()
    response_holder = []
    error_holder = []

    def traced_lock_factory():
        rain_lock_requested.set()
        return rain_lock

    def update_group() -> None:
        try:
            client = _admin_client(app)
            response_holder.append(
                client.put(
                    f"/api/groups/{group['id']}",
                    json={"use_rain_sensor": True},
                )
            )
        except BaseException as exc:  # pragma: no cover - diagnostic handoff
            error_holder.append(exc)

    request_thread = threading.Thread(target=update_group)
    acquired_group = False
    rain_lock.acquire()
    rain_lock_held = True
    try:
        with (
            patch(
                "services.monitors.rain_config_transaction_lock",
                side_effect=traced_lock_factory,
            ),
            patch.object(rain_monitor, "enforce_group", return_value=True),
        ):
            request_thread.start()
            assert rain_lock_requested.wait(timeout=1), "group PUT did not request the rain transaction lock"

            # The request is now blocked on rain_lock. If it already owns the
            # group lock, the decorators are ordered group -> rain and this
            # cross-thread non-blocking acquire deterministically fails.
            locked_group = group_lock(group["id"])
            acquired_group = locked_group.acquire(blocking=False)
            assert acquired_group, "group lock was acquired before the rain transaction lock"
            locked_group.release()
            acquired_group = False

            rain_lock.release()
            rain_lock_held = False
            request_thread.join(timeout=2)
    finally:
        if acquired_group:
            group_lock(group["id"]).release()
        if rain_lock_held:
            rain_lock.release()
        if request_thread.is_alive():
            request_thread.join(timeout=2)

    assert not request_thread.is_alive()
    assert not error_holder
    assert len(response_holder) == 1
    assert response_holder[0].status_code == 200


def test_rain_lock_covers_commit_enforcement_and_exact_cas_rollback(admin_client, app):
    from services.monitors import rain_config_transaction_lock, rain_monitor

    group = _group(app, "Rain transaction lifetime")
    rain_lock = rain_config_transaction_lock()
    original_commit = app.db.update_group_config_with_snapshot
    original_restore = app.db.restore_group_snapshot
    committed = []
    phases = []

    def guarded_commit(*args, **kwargs):
        assert not _available_to_another_thread(rain_lock)
        phases.append("commit")
        snapshot = original_commit(*args, **kwargs)
        committed.append(snapshot)
        return snapshot

    def fail_enforcement(group_id):
        assert group_id == group["id"]
        assert not _available_to_another_thread(rain_lock)
        phases.append("enforce")
        return False

    def guarded_restore(snapshot, *, expected_current, allow_observed_drift):
        assert committed == [expected_current]
        assert allow_observed_drift is False
        assert not _available_to_another_thread(rain_lock)
        phases.append("rollback")
        return original_restore(
            snapshot,
            expected_current=expected_current,
            allow_observed_drift=allow_observed_drift,
        )

    with (
        patch.object(app.db, "update_group_config_with_snapshot", side_effect=guarded_commit),
        patch.object(app.db, "restore_group_snapshot", side_effect=guarded_restore),
        patch.object(rain_monitor, "enforce_group", side_effect=fail_enforcement),
    ):
        response = admin_client.put(
            f"/api/groups/{group['id']}",
            json={"use_rain_sensor": True},
        )

    assert response.status_code == 409
    assert response.get_json()["error_code"] == "RAIN_GROUP_ENFORCEMENT_FAILED"
    assert phases == ["commit", "enforce", "rollback"]
    persisted = app.db.get_group_storage_snapshot(group["id"])
    assert int(persisted["use_rain_sensor"] or 0) == 0
