"""Backward-compatibility stub — re-exports from consolidated services.weather module.

All functionality has been moved to services.weather.
This file exists only so that existing imports continue to work.
"""
from services.weather import WeatherAdjustment, get_weather_adjustment  # noqa: F401

__all__ = ['WeatherAdjustment', 'get_weather_adjustment']
