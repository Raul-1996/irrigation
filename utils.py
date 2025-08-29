def normalize_topic(topic: str) -> str:
    try:
        t = str(topic or '').strip()
        if not t:
            return ''
        if not t.startswith('/'):
            t = '/' + t
        # Не используем управляющие суффиксы вида '/on'
        if t.endswith('/on'):
            t = t[:-3]
        return t
    except Exception:
        return '/' + str(topic)
import typing as _t


def normalize_topic(topic: _t.Optional[str]) -> str:
    """Ensure MQTT topic starts with a single leading slash.

    - Trims whitespace
    - Converts None to empty string
    - Collapses multiple leading slashes to one
    """
    s = str(topic or "").strip()
    if not s:
        return ""
    if s.startswith('/'):
        # collapse multiple leading slashes
        i = 0
        n = len(s)
        while i < n and s[i] == '/':
            i += 1
        s = '/' + s[i:]
    else:
        s = '/' + s
    # Стрижём управляющий суффикс '/on' — используем только базовый топик
    if s.endswith('/on'):
        s = s[:-3]
    return s



