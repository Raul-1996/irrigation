import json
import logging

from flask import Blueprint, render_template

from services.security import user_required

logger = logging.getLogger(__name__)
status_bp = Blueprint("status_bp", __name__)


def _get_inline_data():
    """Pre-fetch zones + groups for instant SSR render."""
    try:
        from database import db
        from routes.zones_crud_api import _zone_ts_to_iso

        zones = db.zones.get_zones()
        # Same TZ normalisation as /api/zones — see issue #47.
        zones = [_zone_ts_to_iso(z) for z in (zones or [])]
        groups = db.groups.get_groups()
        return {
            "inline_zones": json.dumps(zones or [], ensure_ascii=False, default=str),
            "inline_groups": json.dumps(groups or [], ensure_ascii=False, default=str),
        }
    except Exception as e:
        logger.debug("SSR data prefetch failed: %s", e)
        return {"inline_zones": "[]", "inline_groups": "[]"}


@status_bp.route("/")
@user_required
def index():
    return render_template("status.html", **_get_inline_data())


@status_bp.route("/status")
@user_required
def status():
    return render_template("status.html", **_get_inline_data())
