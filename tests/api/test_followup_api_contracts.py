"""Regression tests for Phase 4 API review follow-ups."""

import io
import json
import os
import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


class TestNextWateringBulkBounds:
    @pytest.mark.parametrize("zone_ids", [[True], [1.0], ["1"], [-1], [0]])
    def test_zone_ids_must_be_strict_positive_integers(self, admin_client, zone_ids):
        with patch("routes.zones_crud_api.compute_next_watering") as compute:
            response = admin_client.post("/api/zones/next-watering-bulk", json={"zone_ids": zone_ids})

        assert response.status_code == 400
        assert response.get_json()["success"] is False
        compute.assert_not_called()

    def test_too_many_ids_is_rejected_before_computation(self, admin_client):
        with patch("routes.zones_crud_api.compute_next_watering") as compute:
            response = admin_client.post(
                "/api/zones/next-watering-bulk",
                json={"zone_ids": list(range(1, 514))},
            )

        assert response.status_code == 413
        assert response.get_json()["success"] is False
        compute.assert_not_called()

    def test_oversized_body_is_rejected_before_json_or_computation(self, admin_client):
        body = json.dumps({"zone_ids": [1], "padding": "x" * 70_000})

        with patch("routes.zones_crud_api.compute_next_watering") as compute:
            response = admin_client.post(
                "/api/zones/next-watering-bulk",
                data=body,
                content_type="application/json",
            )

        assert response.status_code == 413
        assert response.get_json()["success"] is False
        compute.assert_not_called()

    def test_ids_are_deduplicated_and_poll_does_not_write_durable_audit(self, admin_client, app):
        with patch("routes.zones_crud_api.compute_next_watering", return_value={}) as compute:
            response = admin_client.post(
                "/api/zones/next-watering-bulk",
                json={"zone_ids": [2, 1, 2, 1]},
            )

        assert response.status_code == 200
        compute.assert_called_once_with([2, 1])
        assert app.db.get_audit_logs(action_type="zones_next_watering_bulk") == []


class TestZoneCrudOwnership:
    @pytest.mark.parametrize("field", ["postpone_until", "postpone_reason"])
    @pytest.mark.parametrize("method", ["create", "update", "import"])
    def test_postpone_fields_are_rejected_outside_postpone_api(self, admin_client, app, field, method):
        zone = app.db.create_zone({"name": "Owned postpone", "group_id": 1, "duration": 10})
        payload = {field: "attacker-controlled"}

        if method == "create":
            response = admin_client.post("/api/zones", json={"name": "New", **payload})
        elif method == "update":
            response = admin_client.put(
                f"/api/zones/{zone['id']}", json={**payload, "expected_version": zone["version"]}
            )
        else:
            response = admin_client.post(
                "/api/zones/import",
                json={"zones": [{"id": zone["id"], **payload}]},
            )

        assert response.status_code == 400
        persisted = app.db.get_zone(zone["id"])
        assert persisted["postpone_until"] is None
        assert persisted["postpone_reason"] is None

    def test_default_group_is_validated_after_defaulting(self, admin_client, app):
        with patch.object(app.db, "get_groups_strict", return_value=[{"id": 999}], create=True):
            response = admin_client.post("/api/zones", json={"name": "No default group"})

        assert response.status_code == 400
        assert not any(zone["name"] == "No default group" for zone in app.db.get_zones())

    @pytest.mark.parametrize("source", ["bulk", "csv"])
    def test_import_default_group_is_validated_before_any_mutation(self, admin_client, app, source):
        headers = {"X-Import-Op": "csv"} if source == "csv" else None
        with (
            patch.object(app.db, "get_groups_strict", return_value=[{"id": 999}], create=True),
            patch.object(app.db, "bulk_upsert_zones") as bulk_upsert,
        ):
            response = admin_client.post(
                "/api/zones/import",
                json={"zones": [{"name": "No default group", "duration": 10}]},
                headers=headers,
            )

        assert response.status_code == 400
        assert "group_id" in response.get_json()["message"]
        bulk_upsert.assert_not_called()

    @pytest.mark.parametrize("field", ["photo_path", "photo_thumb"])
    @pytest.mark.parametrize("method", ["create", "update", "import"])
    def test_photo_metadata_is_rejected_outside_photo_api(self, admin_client, app, field, method):
        zone = app.db.create_zone({"name": "Photo owner", "group_id": 1, "duration": 10})
        payload = {field: "media/zones/ZONE_999.webp"}

        if method == "create":
            response = admin_client.post("/api/zones", json={"name": "Must not exist", **payload})
        elif method == "update":
            response = admin_client.put(
                f"/api/zones/{zone['id']}", json={**payload, "expected_version": zone["version"]}
            )
        else:
            response = admin_client.post(
                "/api/zones/import",
                json={"zones": [{"id": zone["id"], **payload}]},
            )

        assert response.status_code == 400
        persisted = app.db.get_zone(zone["id"])
        assert persisted["photo_path"] is None
        assert persisted["photo_thumb"] is None
        assert not any(candidate["name"] == "Must not exist" for candidate in app.db.get_zones())

    @pytest.mark.parametrize("field", ["command_id", "sequence_id"])
    @pytest.mark.parametrize("method", ["create", "update", "bulk", "csv"])
    def test_internal_command_fields_are_rejected_by_public_crud(self, admin_client, app, field, method):
        zone = app.db.create_zone({"name": "Internal state owner", "group_id": 1, "duration": 10})
        payload = {field: "attacker-controlled"}

        if method == "create":
            response = admin_client.post("/api/zones", json={"name": "Must not exist", **payload})
        elif method == "update":
            response = admin_client.put(
                f"/api/zones/{zone['id']}",
                json={"name": "Must not commit", **payload, "expected_version": zone["version"]},
            )
        else:
            headers = {"X-Import-Op": "csv"} if method == "csv" else None
            response = admin_client.post(
                "/api/zones/import",
                json={"zones": [{"id": zone["id"], "name": "Must not commit", **payload}]},
                headers=headers,
            )

        assert response.status_code == 400
        assert field in response.get_json()["message"]
        assert app.db.get_zone(zone["id"])["name"] == "Internal state owner"
        assert not any(candidate["name"] == "Must not exist" for candidate in app.db.get_zones())

    @pytest.mark.parametrize("topic", ["", "/", "///", "/relay/+", "/relay/#", "/relay/on", "//relay/on"])
    @pytest.mark.parametrize("method", ["create", "update", "bulk", "csv"])
    def test_invalid_physical_actuator_topics_are_rejected(self, admin_client, app, topic, method):
        server = app.db.create_mqtt_server({"name": "Actuator broker", "host": "127.0.0.1", "port": 1883})
        zone = app.db.create_zone(
            {
                "name": "Actuator topology",
                "group_id": 1,
                "duration": 10,
                "mqtt_server_id": server["id"],
                "topic": "/relay/original",
            }
        )
        app.db.update_zone(
            zone["id"],
            {"state": "off", "commanded_state": "off", "observed_state": "off"},
        )
        zone = app.db.get_zone(zone["id"])
        assert zone is not None
        mutation = {"mqtt_server_id": server["id"], "topic": topic}

        if method == "create":
            response = admin_client.post("/api/zones", json={"name": "Must not exist", **mutation})
        elif method == "update":
            response = admin_client.put(
                f"/api/zones/{zone['id']}", json={**mutation, "expected_version": zone["version"]}
            )
        else:
            headers = {"X-Import-Op": "csv"} if method == "csv" else None
            response = admin_client.post(
                "/api/zones/import",
                json={"zones": [{"id": zone["id"], **mutation}]},
                headers=headers,
            )

        assert response.status_code == 400
        assert "topic" in response.get_json()["message"].lower()
        assert app.db.get_zone(zone["id"])["topic"] == "/relay/original"
        assert not any(candidate["name"] == "Must not exist" for candidate in app.db.get_zones())

    def test_physical_actuator_topic_is_stored_canonically(self, admin_client, app):
        server = app.db.create_mqtt_server({"name": "Canonical broker", "host": "127.0.0.1", "port": 1883})

        response = admin_client.post(
            "/api/zones",
            json={"name": "Canonical actuator", "mqtt_server_id": server["id"], "topic": "devices/relay/K1"},
        )

        assert response.status_code == 201
        assert response.get_json()["topic"] == "/devices/relay/K1"

    def test_virtual_zone_never_inherits_the_only_enabled_broker(self, admin_client, app):
        app.db.create_mqtt_server({"name": "Must stay unassigned", "host": "127.0.0.1", "port": 1883})

        response = admin_client.post("/api/zones", json={"name": "Explicitly virtual"})

        assert response.status_code == 201
        persisted = next(zone for zone in app.db.get_zones() if zone["name"] == "Explicitly virtual")
        assert persisted["topic"] == ""
        assert persisted["mqtt_server_id"] is None

    def test_import_rejects_duplicate_ids_before_partial_topologies_can_compose(self, admin_client, app):
        server = app.db.create_mqtt_server({"name": "Duplicate broker", "host": "127.0.0.1", "port": 1883})
        zone = app.db.create_zone(
            {
                "name": "Duplicate target",
                "group_id": 1,
                "mqtt_server_id": server["id"],
                "topic": "/relay/original",
            }
        )
        app.db.update_zone(zone["id"], {"state": "off", "commanded_state": "off", "observed_state": "off"})

        response = admin_client.post(
            "/api/zones/import",
            json={
                "zones": [
                    {"id": zone["id"], "mqtt_server_id": None, "topic": ""},
                    {"id": zone["id"], "topic": "/relay/new"},
                ]
            },
        )

        assert response.status_code == 400
        persisted = app.db.get_zone(zone["id"])
        assert persisted["mqtt_server_id"] == server["id"]
        assert persisted["topic"] == "/relay/original"


class TestMasterTopicOwnership:
    @pytest.mark.parametrize("topic", ["", "/", "///", "/master/+", "/master/#", "/master/on", "//master/on"])
    def test_enabled_master_rejects_unsafe_actuator_topic(self, admin_client, app, topic):
        group = app.db.create_group("Unsafe master topic")
        server = app.db.create_mqtt_server({"name": "Master broker", "host": "127.0.0.1", "port": 1883})

        response = admin_client.put(
            f"/api/groups/{group['id']}",
            json={
                "use_master_valve": True,
                "master_mqtt_server_id": server["id"],
                "master_mqtt_topic": topic,
            },
        )

        assert response.status_code == 400
        assert "топик" in response.get_json()["message"].lower() or "topic" in response.get_json()["message"].lower()
        persisted = next(candidate for candidate in app.db.get_groups() if candidate["id"] == group["id"])
        assert int(persisted["use_master_valve"] or 0) == 0

    def test_master_actuator_topic_is_stored_canonically(self, admin_client, app):
        group = app.db.create_group("Canonical master topic")
        server = app.db.create_mqtt_server({"name": "Master broker", "host": "127.0.0.1", "port": 1883})

        with (
            patch(
                "routes.groups_api._close_master_valve_confirmed",
                side_effect=lambda _sid, _topic, _mode, publish_command: bool(publish_command()),
            ),
            patch("routes.groups_api._publish_mqtt_value", return_value=True),
        ):
            response = admin_client.put(
                f"/api/groups/{group['id']}",
                json={
                    "use_master_valve": True,
                    "master_mqtt_server_id": server["id"],
                    "master_mqtt_topic": "devices/master/K1",
                },
            )

        assert response.status_code == 200
        persisted = next(candidate for candidate in app.db.get_groups() if candidate["id"] == group["id"])
        assert persisted["master_mqtt_topic"] == "/devices/master/K1"


class TestZonePhotoMutationSafety:
    def test_nonexistent_zone_is_rejected_before_image_processing_or_writes(self, admin_client):
        import routes.zones_photo_api as photo_api

        with (
            patch.object(photo_api, "render_two_variants") as render,
            patch.object(photo_api, "_atomic_write") as atomic_write,
        ):
            response = admin_client.post(
                "/api/zones/424242/photo",
                data={"photo": (io.BytesIO(b"image"), "photo.png")},
                content_type="multipart/form-data",
            )

        assert response.status_code == 404
        render.assert_not_called()
        atomic_write.assert_not_called()

    def test_failed_db_update_removes_new_photo_files(self, admin_client, app, tmp_path):
        import routes.zones_photo_api as photo_api

        zone = app.db.create_zone({"name": "Photo rollback", "group_id": 1, "duration": 10})
        with (
            patch.object(photo_api, "UPLOAD_FOLDER", str(tmp_path)),
            patch.object(photo_api, "render_two_variants", return_value=(b"main", b"thumb")),
            patch.object(photo_api.db, "update_zone_photo", return_value=False),
        ):
            response = admin_client.post(
                f"/api/zones/{zone['id']}/photo",
                data={"photo": (io.BytesIO(b"image"), "photo.png")},
                content_type="multipart/form-data",
            )

        assert response.status_code == 500
        assert list(tmp_path.glob(f"ZONE_{zone['id']}*")) == []

    def test_archive_failure_never_deletes_unmoved_previous_files(self, admin_client, app, tmp_path):
        import routes.zones_photo_api as photo_api

        zone = app.db.create_zone({"name": "Photo archive failure", "group_id": 1, "duration": 10})
        main_name = f"ZONE_{zone['id']}.webp"
        thumb_name = f"ZONE_{zone['id']}_thumb.webp"
        main_path = tmp_path / main_name
        thumb_path = tmp_path / thumb_name
        main_path.write_bytes(b"old-main")
        thumb_path.write_bytes(b"old-thumb")
        assert app.db.update_zone_photo(
            zone["id"],
            f"media/zones/{main_name}",
            photo_thumb=f"media/zones/{thumb_name}",
            update_thumb=True,
        )

        with (
            patch.object(photo_api, "UPLOAD_FOLDER", str(tmp_path)),
            patch.object(photo_api, "render_two_variants", return_value=(b"new-main", b"new-thumb")),
            patch.object(photo_api, "_archive_old_zone_file", side_effect=OSError("archive denied")),
        ):
            response = admin_client.post(
                f"/api/zones/{zone['id']}/photo",
                data={"photo": (io.BytesIO(b"image"), "photo.png")},
                content_type="multipart/form-data",
            )

        assert response.status_code == 500
        assert main_path.read_bytes() == b"old-main"
        assert thumb_path.read_bytes() == b"old-thumb"
        current = app.db.get_zone(zone["id"])
        assert current["photo_path"] == f"media/zones/{main_name}"
        assert current["photo_thumb"] == f"media/zones/{thumb_name}"

    @pytest.mark.parametrize("angle", ["bad", None, True, 45, 360, -360, 999999999])
    def test_rotate_requires_exact_angle_allowlist(self, admin_client, app, angle):
        zone = app.db.create_zone({"name": "Rotate", "group_id": 1, "duration": 10})
        app.db.update_zone_photo(zone["id"], "media/zones/missing.webp")

        response = admin_client.post(f"/api/zones/{zone['id']}/photo/rotate", json={"angle": angle})

        assert response.status_code == 400
        assert response.get_json()["error_code"] == "INVALID_ANGLE"

    def test_rotate_second_variant_failure_restores_original_pair(self, admin_client, app, tmp_path):
        import routes.zones_photo_api as photo_api

        zone = app.db.create_zone({"name": "Atomic rotate", "group_id": 1, "duration": 10})
        main_name = f"ZONE_{zone['id']}.webp"
        thumb_name = f"ZONE_{zone['id']}_thumb.webp"
        main_path = tmp_path / main_name
        thumb_path = tmp_path / thumb_name
        for path, color in ((main_path, "red"), (thumb_path, "blue")):
            image = photo_api.Image.new("RGB", (3, 2), color=color)
            image.save(path, format="WEBP", quality=90)
        original_main = main_path.read_bytes()
        original_thumb = thumb_path.read_bytes()
        assert app.db.update_zone_photo(
            zone["id"],
            f"media/zones/{main_name}",
            photo_thumb=f"media/zones/{thumb_name}",
            update_thumb=True,
        )

        real_atomic_write = photo_api._atomic_write
        write_count = 0

        def fail_second_write(path, data):
            nonlocal write_count
            write_count += 1
            if write_count == 2:
                raise OSError("thumb replace denied")
            return real_atomic_write(path, data)

        with (
            patch.object(photo_api, "UPLOAD_FOLDER", str(tmp_path)),
            patch.object(
                photo_api,
                "safe_zone_photo_path",
                side_effect=lambda rel, **_kwargs: str(tmp_path / rel.rsplit("/", 1)[-1]),
            ),
            patch.object(photo_api, "_atomic_write", side_effect=fail_second_write),
        ):
            response = admin_client.post(
                f"/api/zones/{zone['id']}/photo/rotate",
                json={"angle": 90},
            )

        assert response.status_code == 500
        assert main_path.read_bytes() == original_main
        assert thumb_path.read_bytes() == original_thumb

    def test_delete_validates_entire_pair_before_mutating_files_or_metadata(self, admin_client, app, tmp_path):
        import routes.zones_photo_api as photo_api

        zone = app.db.create_zone({"name": "Validate delete pair", "group_id": 1, "duration": 10})
        main_name = f"ZONE_{zone['id']}.webp"
        main_rel = f"media/zones/{main_name}"
        unsafe_thumb = f"media/zones/../ZONE_{zone['id']}_thumb.webp"
        main_path = tmp_path / main_name
        main_path.write_bytes(b"main")
        assert app.db.update_zone_photo(
            zone["id"],
            main_rel,
            photo_thumb=unsafe_thumb,
            update_thumb=True,
        )

        real_safe_zone_photo_path = photo_api.safe_zone_photo_path

        def resolve(rel, **kwargs):
            real_safe_zone_photo_path(rel, **kwargs)
            return str(tmp_path / rel.rsplit("/", 1)[-1])

        with patch.object(photo_api, "safe_zone_photo_path", side_effect=resolve):
            response = admin_client.delete(f"/api/zones/{zone['id']}/photo")

        assert response.status_code == 400
        assert response.get_json()["error_code"] == "INVALID_PHOTO_PATH"
        assert main_path.read_bytes() == b"main"
        current = app.db.get_zone(zone["id"])
        assert current["photo_path"] == main_rel
        assert current["photo_thumb"] == unsafe_thumb

    def test_delete_metadata_failure_preserves_original_pair(self, admin_client, app, tmp_path):
        import routes.zones_photo_api as photo_api

        zone = app.db.create_zone({"name": "Delete rollback", "group_id": 1, "duration": 10})
        main_name = f"ZONE_{zone['id']}.webp"
        thumb_name = f"ZONE_{zone['id']}_thumb.webp"
        main_rel = f"media/zones/{main_name}"
        thumb_rel = f"media/zones/{thumb_name}"
        main_path = tmp_path / main_name
        thumb_path = tmp_path / thumb_name
        main_path.write_bytes(b"main")
        thumb_path.write_bytes(b"thumb")
        assert app.db.update_zone_photo(
            zone["id"],
            main_rel,
            photo_thumb=thumb_rel,
            update_thumb=True,
        )

        with (
            patch.object(
                photo_api,
                "safe_zone_photo_path",
                side_effect=lambda rel, **_kwargs: str(tmp_path / rel.rsplit("/", 1)[-1]),
            ),
            patch.object(app.db, "update_zone_photo", return_value=False),
        ):
            response = admin_client.delete(f"/api/zones/{zone['id']}/photo")

        assert response.status_code == 500
        assert main_path.read_bytes() == b"main"
        assert thumb_path.read_bytes() == b"thumb"
        current = app.db.get_zone(zone["id"])
        assert current["photo_path"] == main_rel
        assert current["photo_thumb"] == thumb_rel


class TestMapRetention:
    def test_listing_and_upload_keep_only_newest_regular_maps(self, admin_client, tmp_path):
        import routes.system_config_api as system_api

        for index in range(system_api.MAX_MAP_FILES + 5):
            path = tmp_path / f"old_{index:02d}.png"
            path.write_bytes(b"old")
            os.utime(path, (index + 1, index + 1))
        ignored = tmp_path / "do-not-delete.txt"
        ignored.write_text("keep", encoding="utf-8")

        with (
            patch.object(system_api, "_TRUSTED_MAP_DIR", str(tmp_path.resolve())),
            patch.object(system_api, "optimize_uploaded_image", return_value=(b"new", ".webp")),
        ):
            response = admin_client.post(
                "/api/map",
                data={"file": (io.BytesIO(b"image"), "map.png")},
                content_type="multipart/form-data",
            )
            listed = admin_client.get("/api/map")

        assert response.status_code == 200
        current_name = response.get_json()["path"].rsplit("/", 1)[-1]
        map_files = [path for path in tmp_path.iterdir() if path.suffix in {".png", ".webp"}]
        assert len(map_files) == system_api.MAX_MAP_FILES
        assert (tmp_path / current_name).read_bytes() == b"new"
        assert ignored.read_text(encoding="utf-8") == "keep"
        items = listed.get_json()["items"]
        assert len(items) == system_api.MAX_MAP_FILES
        assert [item["mtime"] for item in items] == sorted((item["mtime"] for item in items), reverse=True)

    def test_failed_atomic_upload_does_not_prune_existing_maps(self, admin_client, tmp_path):
        import routes.system_config_api as system_api

        existing = []
        for index in range(system_api.MAX_MAP_FILES + 1):
            path = tmp_path / f"map_{index:02d}.png"
            path.write_bytes(b"old")
            existing.append(path.name)

        with (
            patch.object(system_api, "_TRUSTED_MAP_DIR", str(tmp_path.resolve())),
            patch.object(system_api, "optimize_uploaded_image", return_value=(b"new", ".webp")),
            patch.object(system_api.os, "replace", side_effect=OSError("disk full")),
        ):
            response = admin_client.post(
                "/api/map",
                data={"file": (io.BytesIO(b"image"), "map.png")},
                content_type="multipart/form-data",
            )

        assert response.status_code == 500
        assert sorted(path.name for path in tmp_path.glob("map_*.png")) == sorted(existing)
        assert not list(tmp_path.glob("zones_map_*"))

    def test_pruning_fails_closed_when_old_file_cannot_be_removed(self, tmp_path):
        import routes.system_config_api as system_api

        for index in range(system_api.MAX_MAP_FILES + 2):
            path = tmp_path / f"map_{index:02d}.png"
            path.write_bytes(b"map")
            os.utime(path, (index + 1, index + 1))
        current = tmp_path / f"map_{system_api.MAX_MAP_FILES + 1:02d}.png"
        undeletable = tmp_path / "map_00.png"
        real_unlink = os.unlink

        def fail_only_oldest(path, *args, **kwargs):
            if path == undeletable.name:
                raise PermissionError("read-only map")
            return real_unlink(path, *args, **kwargs)

        with (
            patch.object(system_api, "_TRUSTED_MAP_DIR", str(tmp_path.resolve())),
            patch.object(system_api.os, "unlink", side_effect=fail_only_oldest),
        ):
            with pytest.raises(PermissionError, match="read-only map"):
                with system_api._open_map_directory(create=False) as (_, directory_fd):
                    system_api._prune_map_items_locked(directory_fd, new_filename=current.name)

        assert undeletable.exists()
        assert current.exists()


class TestGroupStartDebounceTruth:
    def test_failed_start_does_not_poison_immediate_retry(self, admin_client, app):
        import routes.groups_api as groups_api

        group = app.db.create_group("Retry group")
        zone = app.db.create_zone({"name": "Retry zone", "group_id": group["id"], "duration": 10})
        groups_api._GROUP_CHANGE_GUARD.clear()
        app.config["GROUP_DEBOUNCE_IN_TESTS"] = True
        try:
            with patch(
                "services.zone_control.start_zone_orchestrated",
                side_effect=[("failed", {}), ("started", {})],
            ) as start:
                first = admin_client.post(f"/api/groups/{group['id']}/start-zone/{zone['id']}")
                second = admin_client.post(f"/api/groups/{group['id']}/start-zone/{zone['id']}")
        finally:
            app.config.pop("GROUP_DEBOUNCE_IN_TESTS", None)
            groups_api._GROUP_CHANGE_GUARD.clear()

        assert first.status_code == 400
        assert second.status_code == 200
        assert second.get_json()["success"] is True
        assert start.call_count == 2

    def test_stale_throttle_marker_never_returns_false_success(self, admin_client, app):
        import routes.groups_api as groups_api

        group = app.db.create_group("Stale marker")
        zone = app.db.create_zone({"name": "Inactive zone", "group_id": group["id"], "duration": 10})
        groups_api._GROUP_CHANGE_GUARD[group["id"]] = groups_api.time.time()
        app.config["GROUP_DEBOUNCE_IN_TESTS"] = True
        try:
            with patch("services.zone_control.start_zone_orchestrated", return_value=("failed", {})) as start:
                response = admin_client.post(f"/api/groups/{group['id']}/start-zone/{zone['id']}")
        finally:
            app.config.pop("GROUP_DEBOUNCE_IN_TESTS", None)
            groups_api._GROUP_CHANGE_GUARD.clear()

        assert response.status_code == 400
        assert response.get_json()["success"] is False
        start.assert_called_once()


class TestMqttDiagnosticTruthAndBounds:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("post", "/api/mqtt/424242/probe"),
            ("get", "/api/mqtt/424242/status"),
            ("get", "/api/mqtt/424242/scan-sse"),
        ],
    )
    def test_missing_server_is_truthful_404(self, admin_client, method, path):
        response = getattr(admin_client, method)(path)

        assert response.status_code == 404
        assert response.get_json()["success"] is False
        assert response.get_json()["error_code"] == "MQTT_SERVER_NOT_FOUND"

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("post", "/api/mqtt/1/probe"),
            ("get", "/api/mqtt/1/status"),
            ("get", "/api/mqtt/1/scan-sse"),
        ],
    )
    def test_database_failure_is_5xx_and_never_missing_or_connected(self, admin_client, app, method, path):
        with patch.object(
            app.db,
            "get_mqtt_server_strict",
            side_effect=sqlite3.OperationalError("diagnostic read failed"),
            create=True,
        ):
            response = getattr(admin_client, method)(path)

        assert 500 <= response.status_code < 600
        assert response.get_json()["success"] is False
        assert response.get_json()["error_code"] != "MQTT_SERVER_NOT_FOUND"

    @pytest.mark.parametrize("path", ["/api/mqtt/{server_id}/probe", "/api/mqtt/{server_id}/scan-sse"])
    def test_filter_length_is_bounded_before_client_creation(self, admin_client, app, path):
        import routes.mqtt_api as mqtt_api

        server = app.db.create_mqtt_server({"name": "Filter cap", "host": "broker", "port": 1883})
        fake_mqtt = SimpleNamespace(CallbackAPIVersion=SimpleNamespace(VERSION2=2), Client=MagicMock())

        with patch.object(mqtt_api, "mqtt", fake_mqtt):
            if path.endswith("probe"):
                response = admin_client.post(path.format(server_id=server["id"]), json={"filter": "x" * 513})
            else:
                response = admin_client.get(path.format(server_id=server["id"]) + "?filter=" + "x" * 513)

        assert response.status_code == 400
        assert response.get_json()["success"] is False
        fake_mqtt.Client.assert_not_called()

    def test_per_ip_sse_limit_is_enforced_before_server_or_client_work(self, admin_client):
        import routes.mqtt_api as mqtt_api

        mqtt_api._scan_sse_connections["127.0.0.1"] = mqtt_api.MAX_SCAN_SSE_PER_IP
        try:
            with patch.object(mqtt_api.db, "get_mqtt_server_strict") as get_server:
                response = admin_client.get("/api/mqtt/424242/scan-sse")
        finally:
            mqtt_api._scan_sse_connections.clear()

        assert response.status_code == 429
        assert response.get_json()["error_code"] == "SSE_LIMIT"
        get_server.assert_not_called()

    def test_message_count_topic_and_payload_are_bounded(self):
        import routes.mqtt_api as mqtt_api

        received = []
        message = SimpleNamespace(topic="t" * 700, payload=b"p" * 5000)
        for _ in range(mqtt_api.MAX_DIAGNOSTIC_MESSAGES + 20):
            mqtt_api._append_probe_message(received, message)

        assert len(received) <= mqtt_api.MAX_DIAGNOSTIC_MESSAGES
        assert sum(mqtt_api._mqtt_item_size(item) for item in received) <= mqtt_api.MAX_PROBE_TOTAL_BYTES
        item = received[0]
        assert len(item["topic"].encode("utf-8")) <= mqtt_api.MAX_DIAGNOSTIC_TOPIC_BYTES
        assert len(item["payload"].encode("utf-8")) <= mqtt_api.MAX_DIAGNOSTIC_PAYLOAD_BYTES
        assert item["truncated"] is True
        assert mqtt_api.MAX_SCAN_QUEUE_MESSAGES <= mqtt_api.MAX_DIAGNOSTIC_MESSAGES

    def test_huge_raw_payload_is_bounded_before_decode(self):
        import routes.mqtt_api as mqtt_api

        class DecodeBomb(bytes):
            def decode(self, *args, **kwargs):
                raise AssertionError("unbounded raw payload was decoded")

        item = mqtt_api._bounded_mqtt_item("topic", DecodeBomb(b"x" * (10 * 1024 * 1024)))

        assert len(item["payload"].encode("utf-8")) == mqtt_api.MAX_DIAGNOSTIC_PAYLOAD_BYTES
        assert item["truncated"] is True

    def test_scan_buffer_is_byte_bounded_and_reports_drops(self):
        import routes.mqtt_api as mqtt_api

        buffer = mqtt_api._BoundedSseBuffer(max_frames=1000, max_bytes=128)
        frame = "data: " + ("x" * 90) + "\n\n"

        assert buffer.put_nowait(frame) is True
        assert buffer.put_nowait(frame) is False
        assert buffer.queued_bytes <= 128
        overflow = buffer.get(timeout=0.1)
        assert overflow.startswith("event: overflow\n")
        assert '"dropped":1' in overflow
        assert buffer.get(timeout=0.1) == frame

    @pytest.mark.parametrize("endpoint", ["probe", "status"])
    def test_connection_failure_is_non_success_5xx(self, admin_client, app, endpoint):
        import routes.mqtt_api as mqtt_api

        server = app.db.create_mqtt_server({"name": "Offline", "host": "offline", "port": 1883})
        client = MagicMock()
        client.connect.side_effect = ConnectionRefusedError("sensitive broker detail")
        fake_mqtt = SimpleNamespace(CallbackAPIVersion=SimpleNamespace(VERSION2=2), Client=MagicMock())
        app.config["MQTT_DIAGNOSTICS_LIVE_IN_TESTS"] = True
        try:
            with (
                patch.object(mqtt_api, "mqtt", fake_mqtt),
                patch.object(mqtt_api, "_new_diagnostic_client", return_value=client),
            ):
                if endpoint == "probe":
                    response = admin_client.post(f"/api/mqtt/{server['id']}/probe", json={"duration": 0.1})
                else:
                    response = admin_client.get(f"/api/mqtt/{server['id']}/status")
        finally:
            app.config.pop("MQTT_DIAGNOSTICS_LIVE_IN_TESTS", None)

        assert response.status_code == 502
        assert response.get_json()["success"] is False
        assert "sensitive broker detail" not in response.get_data(as_text=True)

    @pytest.mark.parametrize("endpoint", ["probe", "status"])
    def test_broker_rejection_is_not_reported_as_connected(self, admin_client, app, endpoint):
        import routes.mqtt_api as mqtt_api

        server = app.db.create_mqtt_server({"name": "Rejected", "host": "broker", "port": 1883})
        client = MagicMock()

        def reject_from_loop():
            client.on_connect(client, None, {}, 135)

        client.loop_start.side_effect = reject_from_loop
        fake_mqtt = SimpleNamespace(CallbackAPIVersion=SimpleNamespace(VERSION2=2), Client=MagicMock())
        app.config["MQTT_DIAGNOSTICS_LIVE_IN_TESTS"] = True
        try:
            with (
                patch.object(mqtt_api, "mqtt", fake_mqtt),
                patch.object(mqtt_api, "_new_diagnostic_client", return_value=client),
            ):
                if endpoint == "probe":
                    response = admin_client.post(f"/api/mqtt/{server['id']}/probe", json={"duration": 0.1})
                else:
                    response = admin_client.get(f"/api/mqtt/{server['id']}/status")
        finally:
            app.config.pop("MQTT_DIAGNOSTICS_LIVE_IN_TESTS", None)

        assert response.status_code == 502
        assert response.get_json()["success"] is False

    @pytest.mark.parametrize("duration", ["nan", "inf", 0, -1, 31])
    def test_probe_duration_cap_remains_exact(self, admin_client, app, duration):
        import routes.mqtt_api as mqtt_api

        server = app.db.create_mqtt_server({"name": "Duration cap", "host": "broker", "port": 1883})
        fake_mqtt = SimpleNamespace(CallbackAPIVersion=SimpleNamespace(VERSION2=2), Client=MagicMock())

        with patch.object(mqtt_api, "mqtt", fake_mqtt):
            response = admin_client.post(f"/api/mqtt/{server['id']}/probe", json={"duration": duration})

        assert response.status_code == 400
        assert response.get_json()["error_code"] == "INVALID_DURATION"
        fake_mqtt.Client.assert_not_called()
