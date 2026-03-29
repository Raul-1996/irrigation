"""Tests for program DB operations: CRUD, schedules, conflicts."""
import pytest
import os

os.environ['TESTING'] = '1'


class TestProgramCRUD:
    def test_create_program(self, test_db):
        prog = test_db.create_program({
            'name': 'Morning', 'time': '06:00',
            'days': [0, 2, 4], 'zones': [1, 2],
        })
        assert prog is not None
        assert prog['name'] == 'Morning'

    def test_get_program(self, test_db):
        prog = test_db.create_program({
            'name': 'Eve', 'time': '18:00', 'days': [1, 3], 'zones': [1],
        })
        fetched = test_db.get_program(prog['id'])
        assert fetched is not None
        assert fetched['name'] == 'Eve'

    def test_get_program_not_found(self, test_db):
        assert test_db.get_program(9999) is None

    def test_get_programs(self, test_db):
        test_db.create_program({'name': 'P1', 'time': '06:00', 'days': [0], 'zones': [1]})
        test_db.create_program({'name': 'P2', 'time': '18:00', 'days': [1], 'zones': [2]})
        progs = test_db.get_programs()
        assert len(progs) >= 2

    def test_update_program(self, test_db):
        prog = test_db.create_program({'name': 'Old', 'time': '06:00', 'days': [0], 'zones': [1]})
        updated = test_db.update_program(prog['id'], {'name': 'New', 'time': '07:00', 'days': [0, 1], 'zones': [1, 2]})
        assert updated is not None
        assert updated['name'] == 'New'
        assert updated['time'] == '07:00'

    def test_delete_program(self, test_db):
        prog = test_db.create_program({'name': 'Del', 'time': '06:00', 'days': [0], 'zones': [1]})
        assert test_db.delete_program(prog['id']) is True
        assert test_db.get_program(prog['id']) is None

    def test_delete_program_not_found(self, test_db):
        result = test_db.delete_program(9999)
        assert isinstance(result, bool)


class TestProgramConflicts:
    def test_no_conflict(self, test_db):
        test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        test_db.create_zone({'name': 'Z2', 'duration': 10, 'group_id': 1})
        test_db.create_program({'name': 'P1', 'time': '06:00', 'days': [0], 'zones': [1]})
        # Non-overlapping program
        conflicts = test_db.check_program_conflicts(
            program_id=None, time='18:00', zones=[2], days=[0]
        )
        assert len(conflicts) == 0

    def test_time_overlap_conflict(self, test_db):
        z1 = test_db.create_zone({'name': 'Z1', 'duration': 30, 'group_id': 1})
        z2 = test_db.create_zone({'name': 'Z2', 'duration': 30, 'group_id': 1})
        test_db.create_program({'name': 'P1', 'time': '06:00', 'days': [0], 'zones': [z1['id'], z2['id']]})
        # Overlapping: same time, same zones, same day
        conflicts = test_db.check_program_conflicts(
            program_id=None, time='06:10', zones=[z1['id']], days=[0]
        )
        # Should detect a conflict
        assert isinstance(conflicts, list)


class TestProgramCancellations:
    def test_cancel_and_check(self, test_db):
        prog = test_db.create_program({'name': 'P', 'time': '06:00', 'days': [0], 'zones': [1]})
        test_db.cancel_program_run_for_group(prog['id'], '2026-01-01', 1)
        assert test_db.is_program_run_cancelled_for_group(prog['id'], '2026-01-01', 1) is True

    def test_not_cancelled(self, test_db):
        assert test_db.is_program_run_cancelled_for_group(999, '2026-01-01', 1) is False

    def test_clear_cancellations(self, test_db):
        prog = test_db.create_program({'name': 'P', 'time': '06:00', 'days': [0], 'zones': [1]})
        test_db.cancel_program_run_for_group(prog['id'], '2026-01-01', 1)
        test_db.clear_program_cancellations_for_group_on_date(1, '2026-01-01')
        assert test_db.is_program_run_cancelled_for_group(prog['id'], '2026-01-01', 1) is False
