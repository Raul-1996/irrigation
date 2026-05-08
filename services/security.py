from functools import wraps
from flask import session, redirect, url_for, current_app, request, jsonify


def _is_api_path() -> bool:
    """True if the current request targets an /api/* endpoint.

    Centralised so admin_required / user_required / role_required all use the
    same content-negotiation rule.  Browser-rendered pages keep the existing
    302-to-login UX; XHR/fetch callers get JSON 401/403 instead — see S2.
    """
    try:
        return (request.path or '').startswith('/api/')
    except RuntimeError:  # outside request context (shouldn't happen in views)
        return False


def admin_required(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if current_app.config.get('TESTING'):
            return view_func(*args, **kwargs)
        if session.get('role') != 'admin':
            # S2 FIX: admin_required redirected non-admin users to /login (HTML)
            # for every protected route, including /api/*. fetch('/api/audit')
            # in templates/logs.html silently followed the 302 and tripped on
            # SyntaxError when parsing the login HTML as JSON — the audit page
            # was broken for non-admin viewers.  Distinguish content type:
            #   * /api/*   -> structured JSON 401 (anon) / 403 (logged-in non-admin)
            #   * non-API  -> keep the legacy 302 redirect to login
            if _is_api_path():
                if not session.get('role'):
                    return jsonify({'success': False, 'error_code': 'UNAUTHENTICATED'}), 401
                return jsonify({'success': False, 'error_code': 'FORBIDDEN'}), 403
            return redirect(url_for('auth_bp.login_page'))
        return view_func(*args, **kwargs)
    return wrapper


def user_required(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if current_app.config.get('TESTING'):
            return view_func(*args, **kwargs)
        # Разрешаем доступ гостю для пользовательских страниц (Статус, карта),
        # а также для всех действий на странице Статус по требованию.
        if session.get('role') not in ['guest', 'user', 'admin']:
            if _is_api_path():
                return jsonify({'success': False, 'error_code': 'UNAUTHENTICATED'}), 401
            return redirect(url_for('auth_bp.login_page'))
        return view_func(*args, **kwargs)
    return wrapper


def role_required(*roles):
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(*args, **kwargs):
            if current_app.config.get('TESTING'):
                return view_func(*args, **kwargs)
            if session.get('role') in roles:
                return view_func(*args, **kwargs)
            if _is_api_path():
                if not session.get('role'):
                    return jsonify({'success': False, 'error_code': 'UNAUTHENTICATED'}), 401
                return jsonify({'success': False, 'error_code': 'FORBIDDEN'}), 403
            return redirect(url_for('auth_bp.login_page'))
        return wrapper
    return decorator


