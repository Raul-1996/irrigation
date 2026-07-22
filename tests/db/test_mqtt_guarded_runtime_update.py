"""Atomic MQTT runtime update/reference-scope regression tests."""


def test_guarded_runtime_update_allows_only_rain_settings_reference(test_db):
    server = test_db.create_mqtt_server({"name": "Guarded", "host": "old", "port": 1883})
    assert test_db.set_rain_config({"enabled": True, "topic": "/rain", "type": "NO", "server_id": server["id"]})

    allowed = test_db.update_mqtt_server_reference_guarded(
        server["id"],
        {"host": "rain-ready"},
        allowed_settings={"rain.server_id"},
    )

    assert allowed["status"] == "updated"
    assert allowed["references"] == {"settings": ["rain.server_id"]}
    assert allowed["rain_config"] == {
        "enabled": True,
        "topic": "/rain",
        "type": "NO",
        "server_id": server["id"],
    }
    assert allowed["before_snapshot"]["host"] == "old"
    assert allowed["snapshot"] == test_db.get_mqtt_server_storage_snapshot(server["id"])
    assert test_db.get_mqtt_server(server["id"])["host"] == "rain-ready"

    test_db.create_zone(
        {
            "name": "Concurrent hardware reference",
            "duration": 10,
            "group_id": 1,
            "mqtt_server_id": server["id"],
            "topic": "/zone",
        }
    )
    blocked = test_db.update_mqtt_server_reference_guarded(
        server["id"],
        {"host": "must-not-commit"},
        allowed_settings={"rain.server_id"},
    )

    assert blocked["status"] == "blocked"
    assert blocked["references"]["zones"]
    assert test_db.get_mqtt_server(server["id"])["host"] == "rain-ready"


def test_guarded_rollback_detects_reference_added_after_precheck(test_db):
    server = test_db.create_mqtt_server({"name": "Rollback guard", "host": "old", "port": 1883})
    assert test_db.set_rain_config({"enabled": True, "topic": "/rain", "type": "NO", "server_id": server["id"]})
    before = test_db.get_mqtt_server_storage_snapshot(server["id"])
    updated = test_db.update_mqtt_server_reference_guarded(
        server["id"],
        {"host": "new"},
        allowed_settings={"rain.server_id"},
    )
    assert updated["status"] == "updated"
    current = test_db.get_mqtt_server_storage_snapshot(server["id"])

    zone = test_db.create_zone(
        {
            "name": "Late reference",
            "duration": 10,
            "group_id": 1,
            "mqtt_server_id": server["id"],
            "topic": "/late",
        }
    )
    rollback = test_db.restore_mqtt_server_snapshot_reference_guarded(
        before,
        current,
        allowed_settings={"rain.server_id"},
    )

    assert rollback["restored"] is False
    assert rollback["references"]["zones"] == [zone["id"]]
    assert test_db.get_mqtt_server(server["id"])["host"] == "new"


def test_guarded_rollback_detects_concurrent_server_row_update(test_db):
    server = test_db.create_mqtt_server({"name": "CAS", "host": "old", "port": 1883})
    assert test_db.set_rain_config({"enabled": True, "topic": "/rain", "type": "NO", "server_id": server["id"]})
    before = test_db.get_mqtt_server_storage_snapshot(server["id"])
    updated = test_db.update_mqtt_server_reference_guarded(
        server["id"],
        {"host": "new"},
        allowed_settings={"rain.server_id"},
    )
    assert updated["status"] == "updated"
    assert test_db.update_mqtt_server(server["id"], {"name": "Concurrent rename"})

    rollback = test_db.restore_mqtt_server_snapshot_reference_guarded(
        before,
        updated["snapshot"],
        allowed_settings={"rain.server_id"},
    )

    assert rollback["restored"] is False
    current = test_db.get_mqtt_server(server["id"])
    assert current["name"] == "Concurrent rename"
    assert current["host"] == "new"
