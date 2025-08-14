from flask import Blueprint, render_template
from services.security import admin_required

zones_bp = Blueprint('zones_bp', __name__)


@zones_bp.route('/zones')
@admin_required
def zones_page():
    return render_template('zones.html')


