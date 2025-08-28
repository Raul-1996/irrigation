from flask import Blueprint, render_template
from services.security import admin_required


settings_bp = Blueprint('settings_bp', __name__)


@settings_bp.route('/settings')
@admin_required
def settings_page():
    return render_template('settings.html')


