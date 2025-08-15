from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for
from flask_wtf.csrf import CSRFProtect
from services.auth_service import verify_password

auth_bp = Blueprint('auth_bp', __name__)


@auth_bp.route('/login', methods=['GET'])
def login_page():
    # Поддержка гостевого входа
    if request.args.get('guest') == '1':
        session['logged_in'] = True
        session['role'] = 'user'
        return redirect(url_for('status.index')) if 'status' in url_for.__globals__ else redirect('/')
    return render_template('login.html')


@auth_bp.route('/api/login', methods=['POST'])
def api_login():
    data = request.get_json() or {}
    password = data.get('password', '')
    
    success, role = verify_password(password)
    
    if success:
        session['logged_in'] = True
        session['role'] = role
        return jsonify({'success': True, 'role': role})
    
    return jsonify({'success': False, 'message': 'Неверный пароль'}), 401


