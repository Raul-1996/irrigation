from flask import Blueprint, render_template
from services.security import admin_required

groups_bp = Blueprint('groups_bp', __name__)


@groups_bp.route('/logs')
@admin_required
def logs_page():
    return render_template('logs.html')


