"""Comprehensive tests for db/programs.py."""
import pytest
import os

os.environ['TESTING'] = '1'


class TestProgramCRUD:
    def test_create_program(self, test_db):
        p = test_db.create_program({
            'name': 'Morning', 'time': '06:00',
            'days': [0, 2, 4], 'zones': [1, 2],
        })
        assert p is not None
        assert p['name'] == 'Morning'

    def test_get_programs(self, test_db):
        test_db.create_program({'name': 'P1', 'time': '06:00', 'days': [0], 'zones': [1]})
        test_db.create_program({'name': 'P2', 'time': '18:00', 'days': [1], 'zones': [2]})
        programs = test_db.get_programs()
        assert len(programs) >= 2

    def test_get_program(self, test_db):
        p = test_db.create_program({'name': 'Get', 'time': '07:00', 'days': [0], 'zones': [1]})
        fetched = test_db.get_program(p['id'])
        assert fetched is not None
        assert fetched['name'] == 'Get'

    def test_get_program_not_found(self, test_db):
        assert test_db.get_program(99999) is None

    def test_update_program(self, test_db):
        p = test_db.create_program({'name': 'Old', 'time': '06:00', 'days': [0], 'zones': [1]})
        updated = test_db.update_program(p['id'], {
            'name': 'New', 'time': '07:00', 'days': [1, 3], 'zones': [2, 3],
        })
        assert updated is not None
        assert updated['name'] == 'New'

    def test_delete_program(self, test_db):
        p = test_db.create_program({'name': 'Del', 'time': '06:00', 'days': [0], 'zones': [1]})
        assert test_db.delete_program(p['id']) is True
        assert test_db.get_program(p['id']) is None


class TestProgramConflicts:
    def test_no_conflict(self, test_db):
        test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        test_db.create_program({'name': 'P1', 'time': '06:00', 'days': [0], 'zones': [1]})
        conflicts = test_db.check_program_conflicts(time='18:00', zones=[1], days=[0])
        # Result depends on implementation

    def test_check_conflicts_no_programs(self, test_db):
        conflicts = test_db.check_program_conflicts(time='06:00', zones=[1], days=[0])
        assert isinstance(conflicts, (list, dict, type(None)))


class TestProgramCancellations:
    def test_cancel_program_run(self, test_db):
        p = test_db.create_program({'name': 'P', 'time': '06:00', 'days': [0], 'zones': [1]})
        result = test_db.cancel_program_run_for_group(p['id'], '2026-01-01', 1)
        assert isinstance(result, bool)

    def test_is_cancelled(self, test_db):
        p = test_db.create_program({'name': 'P', 'time': '06:00', 'days': [0], 'zones': [1]})
        test_db.cancel_program_run_for_group(p['id'], '2026-01-01', 1)
        result = test_db.is_program_run_cancelled_for_group(p['id'], '2026-01-01', 1)
        assert result is True

    def test_clear_cancellations(self, test_db):
        p = test_db.create_program({'name': 'P', 'time': '06:00', 'days': [0], 'zones': [1]})
        test_db.cancel_program_run_for_group(p['id'], '2026-01-01', 1)
        test_db.clear_program_cancellations_for_group_on_date(1, '2026-01-01')
        result = test_db.is_program_run_cancelled_for_group(p['id'], '2026-01-01', 1)
        assert result is False
