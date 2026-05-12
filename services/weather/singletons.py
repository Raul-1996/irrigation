"""Process-wide accessors for ``WeatherService`` and ``WeatherAdjustment``.

Single responsibility: cache one instance of each per process and hand it
out on demand. Factored out of ``service.py`` / ``adjustment.py`` because
both modules want to call the other's accessor; keeping the state here
avoids a circular import.

The singleton is keyed on process (not on ``db_path``) — matching the
pre-split behaviour. Changing ``db_path`` at runtime after first call has
never been supported.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from services.weather.adjustment import WeatherAdjustment
    from services.weather.service import WeatherService


_weather_service: "WeatherService | None" = None
_adjustment: "WeatherAdjustment | None" = None


def get_weather_service(db_path: str = "irrigation.db") -> "WeatherService":
    """Get or create the process-wide ``WeatherService`` singleton."""
    global _weather_service
    if _weather_service is None:
        # Imported here (rather than at module top) to break the cycle
        # adjustment.py -> singletons.py -> service.py -> adjustment.py.
        from services.weather.service import WeatherService

        _weather_service = WeatherService(db_path)
    return _weather_service


def get_weather_adjustment(db_path: str = "irrigation.db") -> "WeatherAdjustment":
    """Get or create the process-wide ``WeatherAdjustment`` singleton."""
    global _adjustment
    if _adjustment is None:
        from services.weather.adjustment import WeatherAdjustment

        _adjustment = WeatherAdjustment(db_path)
    return _adjustment
