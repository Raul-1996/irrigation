"""Bounded, atomic retention contract for uploaded zone maps."""

from __future__ import annotations

import io
import os
import stat
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from routes import system_config_api


def _set_map_dir(monkeypatch, directory: Path) -> None:
    monkeypatch.setattr(system_config_api, "_TRUSTED_MAP_DIR", str(directory.resolve()))


def _png_bytes() -> bytes:
    image = Image.new("RGB", (8, 8), color="green")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _seed_maps(directory: Path, count: int) -> list[Path]:
    files = []
    for index in range(count):
        path = directory / f"zones_map_seed_{index:02d}.webp"
        path.write_bytes(b"existing")
        os.utime(path, (1_000 + index, 1_000 + index))
        files.append(path)
    return files


def test_default_map_directory_follows_runtime_working_directory(tmp_path) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment.update(TESTING="1", PYTHONPATH=str(repository_root))

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from routes.system_config_api import _TRUSTED_MAP_DIR; print(_TRUSTED_MAP_DIR)",
        ],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    assert Path(result.stdout.strip()) == tmp_path / "static" / "media" / "maps"


def test_map_api_lists_only_the_newest_bounded_set(admin_client, tmp_path, monkeypatch) -> None:
    seeded = _seed_maps(tmp_path, system_config_api.MAX_MAP_FILES + 7)
    _set_map_dir(monkeypatch, tmp_path)

    response = admin_client.get("/api/map")

    assert response.status_code == 200
    items = response.get_json()["items"]
    assert len(items) == system_config_api.MAX_MAP_FILES
    assert [item["name"] for item in items] == [
        path.name for path in reversed(seeded[-system_config_api.MAX_MAP_FILES :])
    ]
    assert len(list(tmp_path.glob("zones_map_*"))) == system_config_api.MAX_MAP_FILES
    assert not seeded[0].exists()


def test_successful_atomic_upload_prunes_oldest_after_write(admin_client, tmp_path, monkeypatch) -> None:
    _seed_maps(tmp_path, system_config_api.MAX_MAP_FILES + 3)
    stale_temp = tmp_path / ".zones_map_interrupted.tmp"
    stale_temp.write_bytes(b"partial")
    old = time.time() - system_config_api.MAP_TEMP_STALE_SECONDS - 10
    os.utime(stale_temp, (old, old))
    _set_map_dir(monkeypatch, tmp_path)

    response = admin_client.post(
        "/api/map",
        data={"file": (io.BytesIO(_png_bytes()), "latest.png")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    uploaded = Path(response.get_json()["path"]).name
    maps = sorted(tmp_path.glob("zones_map_*"))
    assert len(maps) == system_config_api.MAX_MAP_FILES
    assert (tmp_path / uploaded).is_file()
    assert not list(tmp_path.glob(".zones_map_*.tmp"))


def test_failed_atomic_replace_does_not_prune_existing_maps(admin_client, tmp_path, monkeypatch) -> None:
    seeded = _seed_maps(tmp_path, system_config_api.MAX_MAP_FILES + 3)
    _set_map_dir(monkeypatch, tmp_path)

    with patch.object(system_config_api.os, "replace", side_effect=OSError("disk failure")):
        response = admin_client.post(
            "/api/map",
            data={"file": (io.BytesIO(_png_bytes()), "latest.png")},
            content_type="multipart/form-data",
        )

    assert response.status_code == 500
    assert {path.name for path in tmp_path.glob("zones_map_*")} == {path.name for path in seeded}
    assert not list(tmp_path.glob(".zones_map_*.tmp"))


def test_current_temp_is_not_removed_by_get_or_concurrent_upload(admin_client, tmp_path, monkeypatch) -> None:
    active_temp = tmp_path / ".zones_map_active.tmp"
    active_temp.write_bytes(b"in-flight")
    _set_map_dir(monkeypatch, tmp_path)

    response = admin_client.get("/api/map")

    assert response.status_code == 200
    assert active_temp.read_bytes() == b"in-flight"


def test_map_uploads_are_serialized_and_both_publish(admin_client, tmp_path, monkeypatch) -> None:
    _set_map_dir(monkeypatch, tmp_path)
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    call_count = 0
    real_publish = system_config_api._atomic_publish_map_locked

    def controlled_publish(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            first_entered.set()
            assert release_first.wait(timeout=5)
        else:
            second_entered.set()
        return real_publish(*args, **kwargs)

    monkeypatch.setattr(system_config_api, "_atomic_publish_map_locked", controlled_publish)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(system_config_api._store_map_bytes, b"first", ".webp")
        assert first_entered.wait(timeout=5)
        second = executor.submit(system_config_api._store_map_bytes, b"second", ".webp")
        assert not second_entered.wait(timeout=0.2)
        release_first.set()
        names = {first.result(timeout=5), second.result(timeout=5)}

    assert second_entered.is_set()
    assert len(names) == 2
    assert all((tmp_path / name).is_file() for name in names)
    assert not list(tmp_path.glob(".zones_map_*.tmp"))


def test_symlink_maps_are_removed_and_never_served(admin_client, tmp_path, monkeypatch) -> None:
    outside = tmp_path.parent / f"outside-map-{tmp_path.name}.webp"
    outside.write_bytes(b"secret outside map root")
    link = tmp_path / "zones_map_link.webp"
    link.symlink_to(outside)
    _set_map_dir(monkeypatch, tmp_path)
    try:
        listing = admin_client.get("/api/map")
        assert listing.status_code == 200
        assert "zones_map_link.webp" not in {item["name"] for item in listing.get_json()["items"]}
        assert not os.path.lexists(link)

        link.symlink_to(outside)
        direct = admin_client.get("/static/media/maps/zones_map_link.webp")
        assert direct.status_code == 404
        assert not os.path.lexists(link)
        assert direct.data != outside.read_bytes()
    finally:
        outside.unlink(missing_ok=True)


def test_regular_map_is_served_through_safe_legacy_and_api_routes(admin_client, tmp_path, monkeypatch) -> None:
    image = tmp_path / "zones_map_regular.webp"
    image.write_bytes(b"trusted map bytes")
    _set_map_dir(monkeypatch, tmp_path)

    legacy = admin_client.get("/static/media/maps/zones_map_regular.webp")
    direct = admin_client.get("/api/map/file/zones_map_regular.webp")

    assert legacy.status_code == 200
    assert direct.status_code == 200
    assert legacy.data == direct.data == b"trusted map bytes"
    assert legacy.content_type == direct.content_type == "image/webp"


def test_map_directory_symlink_is_rejected(admin_client, tmp_path, monkeypatch) -> None:
    real_directory = tmp_path / "real"
    real_directory.mkdir()
    linked_directory = tmp_path / "linked"
    linked_directory.symlink_to(real_directory, target_is_directory=True)
    monkeypatch.setattr(system_config_api, "_TRUSTED_MAP_DIR", str(linked_directory))

    response = admin_client.get("/api/map")

    assert response.status_code == 500


def test_get_retention_failure_is_truthful_and_preserves_newest(admin_client, tmp_path, monkeypatch) -> None:
    seeded = _seed_maps(tmp_path, system_config_api.MAX_MAP_FILES + 2)
    newest = seeded[-1]
    oldest = seeded[0]
    _set_map_dir(monkeypatch, tmp_path)
    real_unlink = system_config_api.os.unlink

    def fail_oldest(path, *args, **kwargs):
        if path == oldest.name:
            raise OSError("read-only filesystem")
        return real_unlink(path, *args, **kwargs)

    with patch.object(system_config_api.os, "unlink", side_effect=fail_oldest):
        response = admin_client.get("/api/map")

    assert response.status_code == 500
    assert newest.is_file()
    assert oldest.is_file()


def test_upload_fsyncs_directory_before_prune_and_after_deletes(admin_client, tmp_path, monkeypatch) -> None:
    _seed_maps(tmp_path, system_config_api.MAX_MAP_FILES)
    _set_map_dir(monkeypatch, tmp_path)
    events: list[str] = []
    real_replace = system_config_api.os.replace
    real_unlink = system_config_api.os.unlink
    real_fsync = system_config_api.os.fsync

    def record_replace(*args, **kwargs):
        events.append("replace")
        return real_replace(*args, **kwargs)

    def record_unlink(*args, **kwargs):
        events.append("unlink")
        return real_unlink(*args, **kwargs)

    def record_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            events.append("dir-fsync")
        return real_fsync(fd)

    with (
        patch.object(system_config_api.os, "replace", side_effect=record_replace),
        patch.object(system_config_api.os, "unlink", side_effect=record_unlink),
        patch.object(system_config_api.os, "fsync", side_effect=record_fsync),
    ):
        response = admin_client.post(
            "/api/map",
            data={"file": (io.BytesIO(_png_bytes()), "latest.png")},
            content_type="multipart/form-data",
        )

    assert response.status_code == 200
    replace_index = events.index("replace")
    unlink_index = events.index("unlink", replace_index)
    assert "dir-fsync" in events[replace_index + 1 : unlink_index]
    assert "dir-fsync" in events[unlink_index + 1 :]
