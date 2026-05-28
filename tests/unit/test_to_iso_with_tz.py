"""Unit tests for utils.to_iso_with_tz — the TZ-aware ISO converter
introduced for issue #47.

The helper takes a naive controller-local "YYYY-MM-DD HH:MM:SS" string
(as stored in zones.planned_end_time / zones.watering_start_time) and
returns an ISO-8601 string with the system local TZ offset, so the
browser's ``new Date(...)`` parses it correctly even when the device
TZ differs from the controller's.
"""

import re
from datetime import datetime

from utils import to_iso_with_tz

# Regex matching ISO 8601 with explicit TZ offset (e.g. "+05:00" or "-03:30")
# OR a 'Z' (UTC) suffix. The helper must always emit one of these — never a
# bare "YYYY-MM-DDTHH:MM:SS" without a TZ marker, otherwise the bug reappears.
ISO_WITH_TZ = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}([+-]\d{2}:\d{2}|Z)$")


class TestToIsoWithTz:
    def test_none_returns_none(self):
        assert to_iso_with_tz(None) is None

    def test_empty_returns_empty(self):
        assert to_iso_with_tz("") == ""

    def test_naive_db_format_emits_tz(self):
        """The core fix: naive "YYYY-MM-DD HH:MM:SS" gains a TZ suffix."""
        out = to_iso_with_tz("2026-05-28 00:45:52")
        assert out is not None
        assert ISO_WITH_TZ.match(out), f"output {out!r} missing TZ suffix"

    def test_preserves_wall_clock(self):
        """The wall-clock time must NOT shift — only the TZ marker is added.

        This guards against the most natural bug: converting "as if it were
        UTC" instead of "as if it were local". Round-tripping the result
        with .astimezone(None).replace(tzinfo=None) must return the input.
        """
        out = to_iso_with_tz("2026-05-28 00:45:52")
        parsed = datetime.fromisoformat(out)
        local_naive = parsed.astimezone().replace(tzinfo=None)
        assert local_naive == datetime(2026, 5, 28, 0, 45, 52)

    def test_already_tz_aware_unchanged(self):
        """Idempotent for already-ISO-with-offset input."""
        already = "2026-05-28T00:45:52+05:00"
        assert to_iso_with_tz(already) == already

    def test_already_utc_z_unchanged(self):
        already = "2026-05-28T00:45:52Z"
        assert to_iso_with_tz(already) == already

    def test_invalid_input_returned_as_is(self):
        """Malformed strings are returned unchanged — better than raising and
        breaking the whole zones-list endpoint over one bad row."""
        assert to_iso_with_tz("not a date") == "not a date"

    def test_javascript_compat(self):
        """Smoke check: the output is parseable by datetime.fromisoformat
        (which mirrors what the browser's ``new Date(...)`` does for ISO
        8601). If this passes for any local TZ, the JS code in status.js
        will compute remaining time correctly regardless of device TZ."""
        out = to_iso_with_tz("2026-05-28 00:45:52")
        dt = datetime.fromisoformat(out)
        # Must be TZ-aware — that is the whole point.
        assert dt.tzinfo is not None
        assert dt.utcoffset() is not None
