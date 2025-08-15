from functools import wraps
from flask import session, redirect, url_for, current_app


def admin_required(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if current_app.config.get('TESTING'):
            return view_func(*args, **kwargs)
        if session.get('role') != 'admin':
            return redirect(url_for('auth_bp.login_page'))
        return view_func(*args, **kwargs)
    return wrapper


def user_required(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if current_app.config.get('TESTING'):
            return view_func(*args, **kwargs)
        if session.get('role') not in ['user', 'admin']:
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
            return redirect(url_for('auth_bp.login_page'))
        return wrapper
    return decorator


