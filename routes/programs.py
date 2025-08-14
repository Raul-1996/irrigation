from flask import Blueprint, render_template
from services.security import admin_required

programs_bp = Blueprint('programs_bp', __name__)


@programs_bp.route('/programs')
@admin_required
def programs_page():
    return render_template('programs.html')


