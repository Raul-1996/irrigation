"""Service-level work bounds for next-watering projection."""

import pytest

from services.next_watering import compute_next_watering, normalize_requested_zone_ids


def _compute(zone_ids, **kwargs):
    return compute_next_watering(
        zone_ids,
        all_zones=[{"id": 1, "group_id": 1, "duration": 10, "state": "off"}],
        programs=[],
        skip_today=False,
        **kwargs,
    )


def test_service_deduplicates_requested_zone_ids():
    assert normalize_requested_zone_ids([2, 1, 2, 1]) == [2, 1]
    assert list(_compute([1, 1, 1])) == [1]


@pytest.mark.parametrize("zone_ids", [[True], [1.0], ["1"], [-1]])
def test_service_rejects_noncanonical_zone_ids(zone_ids):
    with pytest.raises((TypeError, ValueError)):
        _compute(zone_ids)


def test_service_rejects_unbounded_explicit_requests():
    with pytest.raises(ValueError, match="too many zone ids"):
        _compute(list(range(1, 514)))


def test_trusted_internal_snapshot_can_disable_request_limit():
    assert list(_compute(list(range(1, 514)), enforce_limit=False)) == list(range(1, 514))
