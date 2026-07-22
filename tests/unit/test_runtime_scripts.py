"""Runtime shell probes share the validated native HTTP transport contract."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_healthcheck_uses_status_only_readyz_at_resolved_custom_bind(tmp_path: Path):
    repository_root = Path(__file__).resolve().parents[2]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    curl_args = tmp_path / "curl-args.txt"
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$WB_TEST_CURL_ARGS\"\nprintf '200'\n",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)

    environment = os.environ.copy()
    environment.update(
        PATH=f"{fake_bin}:{environment.get('PATH', '')}",
        PORT="9123",
        WB_HTTP_BIND_HOST="127.0.0.2",
        WB_IRRIGATION_APP_DIR=str(repository_root),
        WB_IRRIGATION_ENV_FILE=str(tmp_path / "missing.env"),
        WB_IRRIGATION_PYTHON=sys.executable,
        WB_TEST_CURL_ARGS=str(curl_args),
    )

    result = subprocess.run(
        ["/bin/sh", str(repository_root / "scripts" / "healthcheck.sh")],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    arguments = curl_args.read_text(encoding="utf-8")
    assert "http://127.0.0.2:9123/readyz" in arguments
    assert "/api/status" not in arguments
    assert "--insecure" not in arguments


def test_run_rejects_invalid_tls_before_importing_stateful_app(tmp_path: Path):
    repository_root = Path(__file__).resolve().parents[2]
    cert = tmp_path / "invalid.crt"
    key = tmp_path / "invalid.key"
    cert.write_text("not a certificate", encoding="utf-8")
    key.write_text("not a private key", encoding="utf-8")
    environment = os.environ.copy()
    for name in (
        "WB_HTTP_ALLOW_INSECURE_EXTERNAL",
        "WB_HTTP_TRUSTED_PROXY_HOPS",
    ):
        environment.pop(name, None)
    environment.update(
        PYTHONPATH=str(repository_root),
        SECRET_KEY="runtime-import-order-test",
        TESTING="1",
        WB_HTTP_BIND_HOST="127.0.0.1",
        WB_HTTP_TLS_CERTFILE=str(cert),
        WB_HTTP_TLS_KEYFILE=str(key),
    )

    result = subprocess.run(
        [sys.executable, "-c", "import run"],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "valid TLS certificate/private key pair" in result.stderr
    assert not (tmp_path / "irrigation.db").exists()
