"""Deep tests for programs DB operations."""
import json
import pytest


class TestProgramsDBDeep:
    """Deep tests for program repository."""

    def test_create_program(self, test_db):
        test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        zones = test_db.get_zones()
        result = test_db.create_program({
            'name': 'Morning',
            'time': '06:00',
            'days': [0, 2, 4],
            'zones': [zones[0]['id']],
        })
        assert result is not None

    def test_get_programs(self, test_db):
        test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        zones = test_db.get_zones()
        test_db.create_program({
            'name': 'Morning',
            'time': '06:00',
            'days': [0, 2, 4],
            'zones': [zones[0]['id']],
        })
        programs = test_db.get_programs()
        assert len(programs) >= 1
        p = programs[0]
        assert p['name'] == 'Morning'
        assert p['time'] == '06:00'

    def test_update_program(self, test_db):
        test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        zones = test_db.get_zones()
        test_db.create_program({
            'name': 'Morning',
            'time': '06:00',
            'days': [0, 2, 4],
            'zones': [zones[0]['id']],
        })
        programs = test_db.get_programs()
        pid = programs[0]['id']
        test_db.update_program(pid, {
            'name': 'Evening',
            'time': '18:00',
            'days': [1, 3, 5],
            'zones': [zones[0]['id']],
        })
        programs = test_db.get_programs()
        updated = [p for p in programs if p['id'] == pid][0]
        assert updated['name'] == 'Evening'
        assert updated['time'] == '18:00'

    def test_delete_program(self, test_db):
        test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        zones = test_db.get_zones()
        test_db.create_program({
            'name': 'Morning',
            'time': '06:00',
            'days': [0, 2, 4],
            'zones': [zones[0]['id']],
        })
        programs = test_db.get_programs()
        pid = programs[0]['id']
        test_db.delete_program(pid)
        programs = test_db.get_programs()
        assert all(p['id'] != pid for p in programs)

    def test_program_cancellation(self, test_db):
        """Test program cancellation for a specific group/date."""
        test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        zones = test_db.get_zones()
        test_db.create_program({
            'name': 'Morning',
            'time': '06:00',
            'days': [0, 2, 4],
            'zones': [zones[0]['id']],
        })
        programs = test_db.get_programs()
        pid = programs[0]['id']
        # Cancel program run for today
        test_db.cancel_program_run_for_group(pid, '2024-01-01', 1)
        assert test_db.is_program_run_cancelled_for_group(pid, '2024-01-01', 1)
        assert not test_db.is_program_run_cancelled_for_group(pid, '2024-01-02', 1)
