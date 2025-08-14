from flask import Blueprint, render_template, request, jsonify, session
from flask_wtf.csrf import CSRFProtect
from services.auth_service import verify_admin

auth_bp = Blueprint('auth_bp', __name__)


@auth_bp.route('/login', methods=['GET'])
def login_page():
    return render_template('login.html')


@auth_bp.route('/api/login', methods=['POST'])
def api_login():
    data = request.get_json() or {}
    password = data.get('password', '')
    if verify_admin(password):
        session['logged_in'] = True
        session['role'] = 'admin'
        return jsonify({'success': True})
    return jsonify({'success': False, 'message': 'Неверный пароль'}), 401


