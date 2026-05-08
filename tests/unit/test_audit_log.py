"""Unit tests for the audit-log subsystem.

Covers:
  * AuditRepository: add / get / count / cleanup / distinct types.
  * services/audit.py: _redact (secret stripping, truncation, recursion).
  * services/audit.py: record_audit() helper.

These tests use the per-test isolated SQLite DB fixture (``test_db``) so the
production audit_log table is never touched.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta

import pytest

os.environ['TESTING'] = '1'


# --------------------------------------------------------------------------- #
# AuditRepository                                                              #
# --------------------------------------------------------------------------- #

class TestAuditRepository:

    def test_add_and_get_basic(self, test_db):
        rid = test_db.add_audit(
            action_type='zone_modify',
            source='api',
            actor='admin',
            target='zone:5',
            payload={'duration': 15},
            result='success',
            ip='10.0.0.1',
            duration_ms=42,
        )
        assert rid is not None and rid > 0

        rows = test_db.get_audit_logs(limit=10)
        assert len(rows) == 1
        row = rows[0]
        assert row['action_type'] == 'zone_modify'
        assert row['source'] == 'api'
        assert row['actor'] == 'admin'
        assert row['target'] == 'zone:5'
        assert row['result'] == 'success'
        assert row['ip'] == '10.0.0.1'
        assert int(row['duration_ms']) == 42
        assert '"duration": 15' in (row['payload_json'] or '')

    def test_add_handles_none_payload(self, test_db):
        """A None payload must NOT raise — just store NULL."""
        rid = test_db.add_audit(action_type='ping', source='api', payload=None)
        assert rid is not None
        rows = test_db.get_audit_logs(limit=1)
        assert rows[0]['payload_json'] in (None, '')

    def test_add_payload_str_passthrough(self, test_db):
        """Already-JSON string payload must remain as-is."""
        test_db.add_audit(action_type='x', payload='{"k":1}')
        rows = test_db.get_audit_logs(limit=1)
        assert rows[0]['payload_json'] == '{"k":1}'

    def test_count_with_filters(self, test_db):
        for i in range(5):
            test_db.add_audit(action_type='zone_modify', actor='admin')
        for i in range(3):
            test_db.add_audit(action_type='program_create', actor='viewer')

        assert test_db.count_audit_logs() == 8
        assert test_db.count_audit_logs(action_type='zone_modify') == 5
        assert test_db.count_audit_logs(actor='viewer') == 3
        assert test_db.count_audit_logs(actor='nobody') == 0

    def test_get_pagination(self, test_db):
        for i in range(10):
            test_db.add_audit(action_type=f'act_{i}')
        page1 = test_db.get_audit_logs(limit=4, offset=0)
        page2 = test_db.get_audit_logs(limit=4, offset=4)
        assert len(page1) == 4
        assert len(page2) == 4
        # Newest first → distinct ids per page
        ids1 = {r['id'] for r in page1}
        ids2 = {r['id'] for r in page2}
        assert ids1.isdisjoint(ids2)

    def test_distinct_action_types(self, test_db):
        test_db.add_audit(action_type='a')
        test_db.add_audit(action_type='b')
        test_db.add_audit(action_type='a')   # duplicate
        test_db.add_audit(action_type='c')
        types = test_db.get_distinct_audit_action_types()
        assert sorted(types) == ['a', 'b', 'c']

    def test_cleanup_old_rows(self, test_db):
        """cleanup_audit_logs must remove rows older than threshold."""
        # Insert 3 rows with manual past timestamps
        import sqlite3
        for n in range(3):
            test_db.add_audit(action_type='old')
        # Backdate two of them to 10 days ago
        with sqlite3.connect(test_db.db_path) as conn:
            conn.execute(
                "UPDATE audit_log SET ts = datetime('now','-10 days') "
                "WHERE id IN (SELECT id FROM audit_log ORDER BY id ASC LIMIT 2)"
            )
            conn.commit()

        deleted = test_db.cleanup_audit_logs(older_than_days=7)
        assert deleted == 2
        assert test_db.count_audit_logs() == 1

    def test_cleanup_clamps_min_days(self, test_db):
        """Sanity: 0 or negative days is clamped to ≥1, never wipes everything."""
        test_db.add_audit(action_type='fresh')
        deleted = test_db.cleanup_audit_logs(older_than_days=0)
        # Fresh row stays — ts is `now`, not <-1 day
        assert deleted == 0
        assert test_db.count_audit_logs() == 1


# --------------------------------------------------------------------------- #
# Redaction helpers                                                            #
# --------------------------------------------------------------------------- #

class TestRedact:

    def test_redacts_password(self):
        from services.audit import _redact
        out = _redact({'username': 'admin', 'password': 's3cr3t'})
        assert out['username'] == 'admin'
        assert out['password'] == '***'

    def test_redacts_nested_token(self):
        from services.audit import _redact
        out = _redact({'config': {'api_token': 'abc', 'host': 'mqtt'}})
        assert out['config']['api_token'] == '***'
        assert out['config']['host'] == 'mqtt'

    def test_redacts_in_list(self):
        from services.audit import _redact
        out = _redact([{'session_id': 'xyz'}, {'name': 'ok'}])
        assert out[0]['session_id'] == '***'
        assert out[1]['name'] == 'ok'

    def test_truncates_long_string(self):
        from services.audit import _redact, _MAX_VALUE_LEN
        long = 'A' * (_MAX_VALUE_LEN + 100)
        out = _redact(long)
        assert isinstance(out, str)
        assert len(out) <= _MAX_VALUE_LEN + 20  # +'…(truncated)'
        assert out.endswith('…(truncated)')

    def test_passes_through_primitives(self):
        from services.audit import _redact
        assert _redact(42) == 42
        assert _redact(3.14) == 3.14
        assert _redact(True) is True
        assert _redact(None) is None


# --------------------------------------------------------------------------- #
# record_audit helper                                                          #
# --------------------------------------------------------------------------- #

class TestRecordAudit:

    def test_writes_row_with_redaction(self, test_db, monkeypatch):
        # Repoint the global db used by services.audit to our test DB.
        import database as db_mod
        monkeypatch.setattr(db_mod, 'db', test_db, raising=False)

        from services.audit import record_audit
        record_audit(
            action_type='unit_test',
            source='unit',
            target='thing:1',
            payload={'username': 'u', 'password': 'p'},
            actor='tester',
            duration_ms=11,
        )
        rows = test_db.get_audit_logs(limit=5)
        assert any(r['action_type'] == 'unit_test' for r in rows)
        row = next(r for r in rows if r['action_type'] == 'unit_test')
        assert row['actor'] == 'tester'
        assert row['source'] == 'unit'
        assert row['target'] == 'thing:1'
        assert '"password": "***"' in row['payload_json']
        assert '"username": "u"' in row['payload_json']

    def test_swallows_db_failure(self, monkeypatch):
        """record_audit must NEVER raise even if DB layer blows up."""
        import database as db_mod

        class _Boom:
            def add_audit(self, **kw):
                raise RuntimeError('disk full')

        monkeypatch.setattr(db_mod, 'db', _Boom(), raising=False)
        from services.audit import record_audit
        # Should not raise
        record_audit(action_type='boom', payload={'k': 1})
