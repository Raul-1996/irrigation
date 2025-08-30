from flask import Blueprint, render_template
from services.security import admin_required
from database import db


settings_bp = Blueprint('settings_bp', __name__)


@settings_bp.route('/settings')
@admin_required
def settings_page():
    # Передаём текущее название системы в шаблон
    name = db.get_setting_value('system_name') or ''
    return render_template('settings.html', system_name=name)


