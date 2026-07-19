"""Bounded, atomic retention contract for uploaded zone maps."""

from __future__ import annotations

import io
import os
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from routes import system_config_api


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


def test_map_api_lists_only_the_newest_bounded_set(admin_client, tmp_path, monkeypatch) -> None:
    seeded = _seed_maps(tmp_path, system_config_api.MAX_MAP_FILES + 7)
    monkeypatch.setattr(system_config_api, "MAP_DIR", str(tmp_path))

    response = admin_client.get("/api/map")

    assert response.status_code == 200
    items = response.get_json()["items"]
    assert len(items) == system_config_api.MAX_MAP_FILES
    assert [item["name"] for item in items] == [
        path.name for path in reversed(seeded[-system_config_api.MAX_MAP_FILES :])
    ]


def test_successful_atomic_upload_prunes_oldest_after_write(admin_client, tmp_path, monkeypatch) -> None:
    _seed_maps(tmp_path, system_config_api.MAX_MAP_FILES + 3)
    (tmp_path / ".zones_map_interrupted.tmp").write_bytes(b"partial")
    monkeypatch.setattr(system_config_api, "MAP_DIR", str(tmp_path))

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
    monkeypatch.setattr(system_config_api, "MAP_DIR", str(tmp_path))

    with patch.object(system_config_api.os, "replace", side_effect=OSError("disk failure")):
        response = admin_client.post(
            "/api/map",
            data={"file": (io.BytesIO(_png_bytes()), "latest.png")},
            content_type="multipart/form-data",
        )

    assert response.status_code == 500
    assert {path.name for path in tmp_path.glob("zones_map_*")} == {path.name for path in seeded}
    assert not list(tmp_path.glob(".zones_map_*.tmp"))
