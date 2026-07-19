"""Canonical bounds and parsing for durable application entity IDs."""

import re
from typing import Any

# Application identifiers are exchanged with browsers, CSV files and device
# configuration. Keep them in the conventional signed 32-bit positive range,
# far below SQLite's 64-bit ROWID limit, so a malicious import cannot poison
# sqlite_sequence with an effectively unallocatable high watermark.
# Highest automatic ID.  Explicit/imported IDs must stay below this value so
# one final signed 32-bit identifier remains available to AUTOINCREMENT.
MAX_ENTITY_ID = 2_147_483_647
DURABLE_ENTITIES = (
    ("zones", "zone"),
    ("groups", "group"),
    ("programs", "program"),
    ("mqtt_servers", "mqtt_server"),
)
_CANONICAL_DECIMAL_ID = re.compile(r"[1-9][0-9]*\Z")


def parse_explicit_entity_id(value: Any) -> int:
    """Return a canonical explicit ID or raise ``ValueError``.

    JSON integer values and canonical base-10 CSV strings are accepted.
    ``None``, booleans, floats, signs, whitespace and leading zeroes are not
    canonical and must never be silently converted into an auto-allocation.
    """

    if isinstance(value, bool):
        raise ValueError("entity id must be a canonical positive integer")
    if isinstance(value, int):
        normalized = value
    elif isinstance(value, str) and _CANONICAL_DECIMAL_ID.fullmatch(value):
        normalized = int(value)
    else:
        raise ValueError("entity id must be a canonical positive integer")
    if not 1 <= normalized < MAX_ENTITY_ID:
        raise ValueError("entity id is outside the supported range")
    return normalized
