from flask import Blueprint, render_template

files_bp = Blueprint('files_bp', __name__)


@files_bp.route('/map')
def map_page():
    return render_template('map.html')

@files_bp.route('/water')
def water_page():
    return render_template('water.html')


