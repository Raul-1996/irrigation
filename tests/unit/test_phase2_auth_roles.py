"""Regression tests for the guest-viewer authentication flow."""

from flask import Flask

from routes.auth import auth_bp
from services.security import user_required


def _make_auth_app() -> Flask:
    app = Flask(__name__)
    app.config.update(SECRET_KEY="test-secret", TESTING=False)

    @app.route("/", endpoint="status_bp.index")
    @user_required
    def status_page():
        return "status"

    app.register_blueprint(auth_bp)
    return app


def test_guest_login_viewer_can_open_status_page():
    """Finding #96: the explicit viewer login must reach the status page."""
    client = _make_auth_app().test_client()

    response = client.get("/login?guest=1")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")
    assert client.get("/").status_code == 200
    with client.session_transaction() as flask_session:
        assert flask_session["logged_in"] is True
        assert flask_session["role"] == "viewer"


def test_guest_login_does_not_downgrade_authenticated_admin():
    """Finding #57: a GET guest link must not replace an admin session."""
    client = _make_auth_app().test_client()
    with client.session_transaction() as flask_session:
        flask_session["logged_in"] = True
        flask_session["role"] = "admin"

    response = client.get("/login?guest=1")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")
    with client.session_transaction() as flask_session:
        assert flask_session["logged_in"] is True
        assert flask_session["role"] == "admin"
