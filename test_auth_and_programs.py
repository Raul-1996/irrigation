import unittest
import json
import tempfile
import os
import shutil
from database import IrrigationDB
from app import app


class TestAuthAndPrograms(unittest.TestCase):
    def setUp(self):
        self.test_db_path = tempfile.mktemp(suffix='.db')
        self.test_backup_dir = tempfile.mkdtemp()

        self.db = IrrigationDB()
        self.db.db_path = self.test_db_path
        self.db.backup_dir = self.test_backup_dir
        self.db.init_database()

        app.config['TESTING'] = True
        self.client = app.test_client()

        import database
        database.db = self.db
        import app as app_module
        app_module.db = self.db

    def tearDown(self):
        if os.path.exists(self.test_db_path):
            os.remove(self.test_db_path)
        if os.path.exists(self.test_backup_dir):
            shutil.rmtree(self.test_backup_dir)

    def test_auth_status_and_login(self):
        # unauthenticated by default (but TESTING=True returns True for convenience)
        resp = self.client.get('/api/auth/status')
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn('authenticated', data)

        # wrong password
        resp = self.client.post('/api/login', json={'password': 'wrong'})
        self.assertEqual(resp.status_code, 401)

        # correct password (default 1234)
        resp = self.client.post('/api/login', json={'password': '1234'})
        self.assertEqual(resp.status_code, 200)

    def test_program_crud_and_conflicts(self):
        # Prepare group and zones: create new group and move few zones into it
        group_resp = self.client.post('/api/groups', json={'name': 'Группа 1'})
        self.assertEqual(group_resp.status_code, 201)
        group = json.loads(group_resp.data)
        group_id = group['id']

        zones = json.loads(self.client.get('/api/zones').data)
        for z in zones[:3]:
            upd = self.client.put(f"/api/zones/{z['id']}", json={'group_id': group_id})
            self.assertIn(upd.status_code, (200, 204))

        # Create program
        payload = {
            'name': 'Тестовая',
            'time': '06:00',
            'days': [0, 2, 4],
            'zones': [zones[0]['id'], zones[1]['id']]
        }
        resp = self.client.post('/api/programs', json=payload)
        self.assertEqual(resp.status_code, 201)
        program = json.loads(resp.data)

        # Check conflicts with overlapping program
        conflict_resp = self.client.post('/api/programs/check-conflicts', json={
            'program_id': None,
            'time': '06:00',
            'days': [0, 2],
            'zones': [zones[1]['id']]
        })
        self.assertEqual(conflict_resp.status_code, 200)
        conflicts = json.loads(conflict_resp.data)
        self.assertTrue(conflicts['success'])

        # Update program
        upd = self.client.put(f"/api/programs/{program['id']}", json={
            'name': 'Обновленная',
            'time': '07:00',
            'days': [1, 3],
            'zones': [zones[0]['id']]
        })
        self.assertEqual(upd.status_code, 200)

        # Delete program
        dele = self.client.delete(f"/api/programs/{program['id']}")
        self.assertEqual(dele.status_code, 204)


if __name__ == '__main__':
    unittest.main(verbosity=2)


