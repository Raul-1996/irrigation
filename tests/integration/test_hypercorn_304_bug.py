"""Регресс на hypercorn AsyncioWSGIMiddleware 304/HEAD bug.

Werkzeug по умолчанию ставит ETag и Last-Modified на статические файлы.
Hypercorn 0.14-0.18 WSGI-bridge ломается когда Werkzeug отдаёт 304 Not
Modified в ответ на If-None-Match — внутри bridge кидается
UnexpectedMessageError, клиент получает HTTP 500. Браузер на F5 видит
500 на все CSS/JS, вёрстка отваливается.

Мы это лечим through-request middleware, который снимает ETag и
Last-Modified. Тесты гарантируют что validators не утекают.
"""


def test_static_response_has_no_validators(client):
    """Werkzeug по дефолту ставит ETag/Last-Modified — middleware должен снять."""
    r = client.get("/static/css/base.css")
    assert r.status_code == 200
    assert "ETag" not in r.headers, "ETag must be stripped (hypercorn 304 bug)"
    assert "Last-Modified" not in r.headers, "Last-Modified must be stripped"


def test_dynamic_response_has_no_validators(client):
    """Динамические view тоже не должны утекать validators."""
    r = client.get("/")
    assert r.status_code in (200, 302)
    assert "ETag" not in r.headers
    assert "Last-Modified" not in r.headers


def test_conditional_get_returns_200_not_304(client):
    """Если ETag не отдаётся, Werkzeug не может короткозамкнуть на 304.
    Если этот тест провалится в проде → 500 на conditional GET."""
    r = client.get(
        "/static/css/base.css",
        headers={"If-None-Match": '"any-tag-must-not-match"'},
    )
    assert r.status_code == 200
