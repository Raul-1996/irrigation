from flask import Blueprint, render_template
from services.security import user_required

files_bp = Blueprint('files_bp', __name__)


@files_bp.route('/map')
@user_required
def map_page():
    return render_template('map.html')


@files_bp.route('/water')
@user_required
def water_page():
    # Страница удалена из MVP
    return render_template('404.html'), 404


