"""WMO Weather Code to emoji icon and Russian description mapping.

Used by both backend (API responses) and can be referenced by frontend.
Python 3.9 compatible.
"""
from typing import Optional

# WMO Weather Interpretation Codes (WW)
# https://open-meteo.com/en/docs — "WMO Weather interpretation codes"
WEATHER_CODES = {
    0:  ("☀️", "Ясно"),
    1:  ("🌤️", "Малооблачно"),
    2:  ("⛅", "Переменная облачность"),
    3:  ("☁️", "Пасмурно"),
    45: ("🌫️", "Туман"),
    48: ("🌫️", "Изморозь"),
    51: ("🌦️", "Слабая морось"),
    53: ("🌦️", "Морось"),
    55: ("🌧️", "Сильная морось"),
    56: ("🌧️", "Морось с заморозком"),
    57: ("🌧️", "Сильная морось с заморозком"),
    61: ("🌧️", "Небольшой дождь"),
    63: ("🌧️", "Дождь"),
    65: ("🌧️", "Сильный дождь"),
    66: ("🌧️", "Дождь с заморозком"),
    67: ("🌧️", "Сильный дождь с заморозком"),
    71: ("❄️", "Небольшой снег"),
    73: ("❄️", "Снег"),
    75: ("❄️", "Сильный снег"),
    77: ("❄️", "Снежная крупа"),
    80: ("🌦️", "Кратковр. дождь"),
    81: ("🌧️", "Ливень"),
    82: ("🌧️", "Сильный ливень"),
    85: ("🌨️", "Снегопад"),
    86: ("🌨️", "Сильный снегопад"),
    95: ("⛈️", "Гроза"),
    96: ("⛈️", "Гроза с градом"),
    99: ("⛈️", "Сильная гроза с градом"),
}  # type: dict


def get_weather_icon(code):
    # type: (Optional[int]) -> str
    """Return emoji icon for WMO weather code."""
    if code is None:
        return "🌡️"
    entry = WEATHER_CODES.get(code)
    return entry[0] if entry else "🌡️"


def get_weather_desc(code):
    # type: (Optional[int]) -> str
    """Return Russian description for WMO weather code."""
    if code is None:
        return ""
    entry = WEATHER_CODES.get(code)
    return entry[1] if entry else ""
