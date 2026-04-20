"""Static-lint test for configs/logrotate.d/wb-irrigation (Wave 2 F5).

We don't invoke the `logrotate` binary (not always available in CI/dev);
instead we validate structural invariants that, if violated, would cause
production data loss or double-rotation races.
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG_PATH = os.path.join(ROOT, 'configs', 'logrotate.d', 'wb-irrigation')


@pytest.fixture(scope='module')
def config_text():
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        return f.read()


def test_config_file_exists():
    assert os.path.isfile(CONFIG_PATH), f"missing {CONFIG_PATH}"


def test_managed_marker_header_present(config_text):
    """First-line comment identifies repo origin."""
    assert 'Managed by wb-irrigation repo' in config_text


def test_blocks_are_balanced(config_text):
    """Each opening `{` must have a matching `}` at block-end."""
    opens = config_text.count('{')
    closes = config_text.count('}')
    assert opens == closes and opens >= 2, (
        f"brace count mismatch: {{ {opens} vs }} {closes}"
    )


def test_mosquitto_block_present(config_text):
    assert '/var/log/mosquitto/mosquitto.log' in config_text


def test_telegram_block_present(config_text):
    assert '/opt/wb-irrigation/irrigation/services/logs/telegram.txt' in config_text


def test_app_log_NOT_in_config(config_text):
    """CRITICAL: app.log is rotated by Python handler; double-rotation races
    would lose data. Config must NOT declare it as a rotation target.

    We strip comment lines (starting with '#') before checking, because the
    header explains *why* app.log is excluded and would otherwise self-trip.
    """
    non_comment = '\n'.join(
        line for line in config_text.splitlines() if not line.lstrip().startswith('#')
    )
    forbidden = ['/backups/app.log', 'app.log', 'import-export.log']
    for needle in forbidden:
        assert needle not in non_comment, (
            f"{needle} is rotated by Python TimedRotatingFileHandler; "
            f"putting it in logrotate.d causes races."
        )


def test_copytruncate_used_on_all_blocks(config_text):
    """Neither mosquitto nor telegram bot supports SIGHUP reopen contract;
    both blocks must use `copytruncate`."""
    blocks = re.findall(r'\{([^}]+)\}', config_text, flags=re.DOTALL)
    assert len(blocks) >= 2
    for i, block in enumerate(blocks):
        assert 'copytruncate' in block, (
            f"block #{i+1} is missing `copytruncate`: {block.strip()[:120]}"
        )


def test_compression_is_delayed(config_text):
    """`compress` without `delaycompress` breaks live tailing of .log.1 —
    enforce both always appear together."""
    blocks = re.findall(r'\{([^}]+)\}', config_text, flags=re.DOTALL)
    for i, block in enumerate(blocks):
        if 'compress' in block and 'nocompress' not in block:
            assert 'delaycompress' in block, (
                f"block #{i+1} uses compress without delaycompress"
            )


def test_missingok_set_on_all_blocks(config_text):
    """Fresh-install safety: if the file doesn't exist yet, logrotate must
    not fail the cron job for other files."""
    blocks = re.findall(r'\{([^}]+)\}', config_text, flags=re.DOTALL)
    for i, block in enumerate(blocks):
        assert 'missingok' in block, f"block #{i+1} is missing `missingok`"


def test_rotate_count_sane(config_text):
    """Guard against accidental `rotate 0` (immediate delete) or missing."""
    blocks = re.findall(r'\{([^}]+)\}', config_text, flags=re.DOTALL)
    for i, block in enumerate(blocks):
        m = re.search(r'\brotate\s+(\d+)\b', block)
        assert m, f"block #{i+1} missing `rotate N` directive"
        count = int(m.group(1))
        assert 1 <= count <= 60, (
            f"block #{i+1} has suspicious rotate count: {count}"
        )
