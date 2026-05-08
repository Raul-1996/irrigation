"""Security regression tests for /api/zones/import (B1 BLOCKER fix).

bulk_upsert_zones() previously accepted `state` (and other state-machine
fields) through its UPDATE whitelist, allowing /api/zones/import to bypass
the state-machine guard, optimistic-locking, and audit trail.  Even though
the endpoint is admin-only, this broke audit integrity — zone_state_change
rows were never written for state changes that came in through the import
pathway.

These tests pin the new contract:
  * payloads with `state` MUST NOT change zone state in DB.
  * payloads with other state-machine fields are similarly stripped.
  * non-state fields (name, duration, topic, …) keep working.
"""
from __future__ import annotations

import json
import os

import pytest

os.environ['TESTING'] = '1'


class TestBulkImportRejectsStateField:
    def test_state_in_payload_does_not_change_state(self, admin_client, app):
        """POST /api/zones/import with state='on' must NOT flip the zone on."""
        zone = app.db.create_zone({'name': 'B1', 'duration': 10, 'group_id': 1})
        # Pre-condition: brand-new zones default to 'off'.
        assert zone['state'] == 'off'

        resp = admin_client.post(
            '/api/zones/import',
            data=json.dumps({'zones': [{'id': zone['id'], 'state': 'on'}]}),
            content_type='application/json',
        )
        assert resp.status_code == 200, resp.data

        after = app.db.get_zone(zone['id'])
        assert after['state'] == 'off', (
            f"bulk-import bypassed state-machine guard: zone now {after['state']!r}"
        )

    def test_other_state_machine_fields_stripped(self, admin_client, app):
        """commanded_state / observed_state / fault_count / last_fault stripped too."""
        zone = app.db.create_zone({'name': 'B1b', 'duration': 10, 'group_id': 1})
        before = app.db.get_zone(zone['id'])

        resp = admin_client.post(
            '/api/zones/import',
            data=json.dumps({'zones': [{
                'id': zone['id'],
                'commanded_state': 'on',
                'observed_state': 'on',
                'fault_count': 99,
                'last_fault': 'forged',
                # piggyback an allowed mutation to verify it still applies
                'name': 'Renamed-OK',
            }]}),
            content_type='application/json',
        )
        assert resp.status_code == 200, resp.data

        after = app.db.get_zone(zone['id'])
        # State-machine fields untouched
        for field in ('commanded_state', 'observed_state', 'fault_count', 'last_fault'):
            if field in before and field in after:
                assert before[field] == after[field], (
                    f"bulk-import leaked state-machine field {field!r}: "
                    f"{before[field]!r} -> {after[field]!r}"
                )
        # Non-state mutation went through
        assert after['name'] == 'Renamed-OK'

    def test_clean_payload_still_works(self, admin_client, app):
        """Sanity check: a payload with only allowed columns updates as expected."""
        zone = app.db.create_zone({'name': 'B1c-old', 'duration': 5, 'group_id': 1})

        resp = admin_client.post(
            '/api/zones/import',
            data=json.dumps({'zones': [{
                'id': zone['id'],
                'name': 'B1c-new',
                'duration': 30,
            }]}),
            content_type='application/json',
        )
        assert resp.status_code == 200, resp.data

        after = app.db.get_zone(zone['id'])
        assert after['name'] == 'B1c-new'
        assert int(after['duration']) == 30
