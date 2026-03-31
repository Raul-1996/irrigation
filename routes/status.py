import json
import logging
from flask import Blueprint, render_template
from services.security import user_required

logger = logging.getLogger(__name__)
status_bp = Blueprint('status_bp', __name__)


def _get_inline_data():
    """Pre-fetch zones + groups + status for instant SSR render."""
    try:
        from database import db
        zones = db.zones.get_zones()
        groups = db.groups.get_groups()
        # Build status summary
        status_data = None
        try:
            from routes.system_status_api import build_status_response
            status_data = build_status_response()
        except (ImportError, AttributeError, TypeError):
            pass
        return {
            'inline_zones': json.dumps(zones or [], ensure_ascii=False, default=str),
            'inline_groups': json.dumps(groups or [], ensure_ascii=False, default=str),
            'inline_status': json.dumps(status_data or {}, ensure_ascii=False, default=str),
        }
    except Exception as e:
        logger.debug("SSR data prefetch failed: %s", e)
        return {'inline_zones': '[]', 'inline_groups': '[]', 'inline_status': '{}'}


@status_bp.route('/')
@user_required
def index():
    return render_template('status.html', **_get_inline_data())


@status_bp.route('/status')
@user_required
def status():
    return render_template('status.html', **_get_inline_data())


