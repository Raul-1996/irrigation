from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for
from services.auth_service import verify_password
from services.rate_limiter import login_limiter

auth_bp = Blueprint('auth_bp', __name__)

# Will be set by app.py after csrf is created
csrf = None


@auth_bp.route('/login', methods=['GET'])
def login_page():
    # Поддержка гостевого входа (viewer — только чтение, без мутаций)
    if request.args.get('guest') == '1':
        session['logged_in'] = True
        session['role'] = 'viewer'
        return redirect(url_for('status_bp.index'))
    return render_template('login.html')


@auth_bp.route('/api/login', methods=['POST'])
def api_login():
    data = request.get_json(silent=True) or {}
    password = (data.get('password') or '').strip()

    # IP-based rate limiting (TASK-009)
    ip = request.remote_addr or '0.0.0.0'
    allowed, retry_after = login_limiter.check(ip)
    if not allowed:
        return jsonify({'success': False, 'message': f'Слишком много попыток. Повторите через {retry_after}с'}), 429

    success, role = verify_password(password)

    if success:
        login_limiter.reset(ip)
        session['logged_in'] = True
        session['role'] = role
        return jsonify({'success': True, 'role': role})

    login_limiter.record_failure(ip)
    return jsonify({'success': False, 'message': 'Неверный пароль'}), 401


