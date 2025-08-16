from flask import Blueprint, render_template
from services.security import admin_required

mqtt_bp = Blueprint('mqtt_bp', __name__)


@mqtt_bp.route('/mqtt')
@admin_required
def mqtt_page():
    return render_template('mqtt.html')


