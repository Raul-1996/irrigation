from flask import Blueprint, render_template

status_bp = Blueprint('status_bp', __name__)


@status_bp.route('/')
def index():
    return render_template('status.html')


@status_bp.route('/status')
def status():
    return render_template('status.html')


