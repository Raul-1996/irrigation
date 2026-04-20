from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for
from services.auth_service import verify_password
from services.rate_limiter import login_limiter

auth_bp = Blueprint('auth_bp', __name__)

# Will be set by app.py after csrf is created
csrf = None


def _regenerate_session(new_values: dict) -> None:
    """Invalidate the current session and issue a fresh session id.

    Mitigates SEC-006 (session fixation): an attacker who planted a
    pre-known session cookie on the victim (via phishing, `?guest=1`,
    XSS, MITM) cannot escalate to the authenticated victim's session
    because login clears the previous session identifier.
    """
    # Capture anything we want to preserve *before* clear(), but by design
    # for a privilege-changing event we preserve nothing except what the
    # caller explicitly passes in.
    session.clear()
    # Flask signs the session cookie using app.secret_key; modifying the
    # session after clear() forces a new signed cookie on the response.
    session.permanent = False
    for k, v in new_values.items():
        session[k] = v


@auth_bp.route('/login', methods=['GET'])
def login_page():
    # Поддержка гостевого входа (viewer — только чтение, без мутаций)
    if request.args.get('guest') == '1':
        # Regenerate session id even for guest login — prevents a stored
        # unauthenticated sid from being re-used later when the same
        # browser authenticates as admin.
        _regenerate_session({'logged_in': True, 'role': 'viewer'})
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
        # SEC-006 fix: regenerate session id on successful authentication.
        # `session.clear()` + setting new keys forces Flask to emit a new
        # signed cookie, breaking any fixation attempt.
        _regenerate_session({'logged_in': True, 'role': role})
        return jsonify({'success': True, 'role': role})

    login_limiter.record_failure(ip)
    return jsonify({'success': False, 'message': 'Неверный пароль'}), 401


