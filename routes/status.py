from flask import Blueprint, render_template
from services.security import user_required

status_bp = Blueprint('status_bp', __name__)


@status_bp.route('/')
@user_required
def index():
    return render_template('status.html')


@status_bp.route('/status')
@user_required
def status():
    return render_template('status.html')


