"""Static-lint test for configs/logrotate.d/wb-irrigation (Wave 2 F5).

We don't invoke the `logrotate` binary (not always available in CI/dev);
instead we validate structural invariants that, if violated, would cause
production data loss or double-rotation races.
"""

import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG_PATH = os.path.join(ROOT, "configs", "logrotate.d", "wb-irrigation")


@pytest.fixture(scope="module")
def config_text():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return f.read()


def test_config_file_exists():
    assert os.path.isfile(CONFIG_PATH), f"missing {CONFIG_PATH}"


def test_managed_marker_header_present(config_text):
    """First-line comment identifies repo origin."""
    assert "Managed by wb-irrigation repo" in config_text


def test_blocks_are_balanced(config_text):
    """The managed marker must remain target-free after retiring file logs."""
    assert "{" not in config_text
    assert "}" not in config_text


def test_mosquitto_package_log_is_not_redeclared(config_text):
    """Debian's mosquitto package owns this target in its own config.

    Declaring the same path twice makes the complete logrotate run fail with
    ``duplicate log entry`` and prevents the complete rotation run.
    """
    non_comment = "\n".join(line for line in config_text.splitlines() if not line.lstrip().startswith("#"))
    assert "/var/log/mosquitto/mosquitto.log" not in non_comment


def test_retired_telegram_block_is_absent(config_text):
    non_comment = "\n".join(line for line in config_text.splitlines() if not line.lstrip().startswith("#"))
    assert "telegram.txt" not in non_comment


def test_app_log_NOT_in_config(config_text):
    """CRITICAL: app.log is rotated by Python handler; double-rotation races
    would lose data. Config must NOT declare it as a rotation target.

    We strip comment lines (starting with '#') before checking, because the
    header explains *why* app.log is excluded and would otherwise self-trip.
    """
    non_comment = "\n".join(line for line in config_text.splitlines() if not line.lstrip().startswith("#"))
    forbidden = ["/backups/app.log", "app.log", "import-export.log"]
    for needle in forbidden:
        assert needle not in non_comment, (
            f"{needle} is rotated by Python TimedRotatingFileHandler; putting it in logrotate.d causes races."
        )


def test_managed_file_contains_no_active_directives(config_text):
    active_lines = [
        line.strip() for line in config_text.splitlines() if line.strip() and not line.lstrip().startswith("#")
    ]
    assert active_lines == []
