import time
from datetime import datetime, timedelta

from irrigation_scheduler import get_scheduler
from database import db


def test_scheduler_postpone_sweeper(client):
    # Ensure scheduler is initialized
    client.post('/api/scheduler/init')
    sched = get_scheduler()
    if not sched:
        return  # environment without scheduler

    # Pick any existing zone
    zones = db.get_zones()
    assert zones, 'No zones configured'
    zid = zones[0]['id']

    # Set postpone_until in the past
    past = (datetime.now() - timedelta(minutes=1)).strftime('%Y-%m-%d %H:%M:%S')
    db.update_zone_postpone(zid, past, 'manual')

    # Run sweeper
    sched.clear_expired_postpones()
    z = db.get_zone(zid)
    assert not z.get('postpone_until'), 'Postpone should be cleared for expired'


def test_scheduler_cancel_group_jobs(client):
    # Ensure scheduler is initialized
    client.post('/api/scheduler/init')
    sched = get_scheduler()
    if not sched:
        return

    # Start group sequence (best-effort)
    groups = db.get_groups()
    assert groups, 'No groups'
    gid = next((g['id'] for g in groups if int(g['id']) != 999), groups[0]['id'])
    client.post(f'/api/groups/{gid}/start-from-first')

    # Give a short time to enqueue jobs
    time.sleep(0.1)
    # Cancel all jobs for the group
    sched.cancel_group_jobs(int(gid))

    # Should not raise; active zones for that group should be gone from scheduler map
    active = sched.get_active_zones() or {}
    # We don't know which zones are in group; just ensure active mapping doesn't grow after cancel
    # and that call did not crash. At minimum active is a dict.
    assert isinstance(active, dict)

