"""
actions/weather_api.py

Real weather data for L.I.T.E.'s weather panel — current conditions plus a
5-day forecast for any city.

Uses Open-Meteo (https://open-meteo.com), a free weather API that requires
NO API key and has no rate-limit signup step. Two calls per lookup:

  1. Geocoding — resolve a free-text city name to lat/lon.
  2. Forecast   — current conditions + daily highs/lows/weather codes.

This is intentionally separate from actions/weather_report.py (the existing
voice-command action that just opens a Google weather search) — this module
returns structured data for the PyQt6 WeatherPanel to render directly,
rather than opening a browser tab.
"""

from __future__ import annotations

import requests

_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT = 8

# WMO weather interpretation codes -> (short label, emoji)
# https://open-meteo.com/en/docs — "weathercode" section
_WMO_CODES: dict[int, tuple[str, str]] = {
    0:  ("Clear sky",            "☀"),
    1:  ("Mainly clear",         "🌤"),
    2:  ("Partly cloudy",        "⛅"),
    3:  ("Overcast",             "☁"),
    45: ("Fog",                  "🌫"),
    48: ("Depositing rime fog",  "🌫"),
    51: ("Light drizzle",        "🌦"),
    53: ("Drizzle",              "🌦"),
    55: ("Dense drizzle",        "🌦"),
    56: ("Light freezing drizzle", "🌧"),
    57: ("Freezing drizzle",     "🌧"),
    61: ("Slight rain",          "🌧"),
    63: ("Rain",                 "🌧"),
    65: ("Heavy rain",           "🌧"),
    66: ("Light freezing rain",  "🌧"),
    67: ("Freezing rain",        "🌧"),
    71: ("Slight snow",          "🌨"),
    73: ("Snow",                 "🌨"),
    75: ("Heavy snow",           "🌨"),
    77: ("Snow grains",          "🌨"),
    80: ("Slight rain showers",  "🌦"),
    81: ("Rain showers",         "🌦"),
    82: ("Violent rain showers", "⛈"),
    85: ("Slight snow showers",  "🌨"),
    86: ("Heavy snow showers",   "🌨"),
    95: ("Thunderstorm",         "⛈"),
    96: ("Thunderstorm + hail",  "⛈"),
    99: ("Thunderstorm + heavy hail", "⛈"),
}


def _describe(code: int) -> tuple[str, str]:
    return _WMO_CODES.get(int(code), ("Unknown", "❓"))


class WeatherLookupError(Exception):
    """Raised when a city can't be resolved or the forecast call fails."""


def _geocode(city: str) -> dict:
    resp = requests.get(
        _GEOCODE_URL,
        params={"name": city, "count": 1, "language": "en", "format": "json"},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    results = data.get("results") or []
    if not results:
        raise WeatherLookupError(f"Couldn't find a city matching '{city}'.")
    r = results[0]
    return {
        "name":      r.get("name", city),
        "country":   r.get("country_code", "").upper(),
        "admin1":    r.get("admin1", ""),
        "latitude":  r["latitude"],
        "longitude": r["longitude"],
        "timezone":  r.get("timezone", "auto"),
    }


def get_weather_for_city(city: str) -> dict:
    """
    Returns:
        {
            "city": "London", "country": "GB", "region": "England",
            "current": {"temp_c": 14.2, "wind_kph": 11.0, "description": "Overcast", "icon": "☁"},
            "forecast": [
                {"date": "2026-08-15", "temp_max_c": 16.1, "temp_min_c": 10.4,
                 "description": "Partly cloudy", "icon": "⛅"},
                ... up to 5 days ...
            ],
        }
    Raises WeatherLookupError on a bad city name or network failure — callers
    should catch this and show it in the UI rather than letting it propagate.
    """
    if not city or not city.strip():
        raise WeatherLookupError("No city specified.")

    try:
        loc = _geocode(city.strip())

        resp = requests.get(
            _FORECAST_URL,
            params={
                "latitude":  loc["latitude"],
                "longitude": loc["longitude"],
                "current_weather": "true",
                "daily": "weathercode,temperature_2m_max,temperature_2m_min",
                "forecast_days": 5,
                "timezone": loc["timezone"] or "auto",
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        raise WeatherLookupError(f"Weather service unreachable: {e}") from e
    except WeatherLookupError:
        raise
    except Exception as e:
        raise WeatherLookupError(f"Unexpected error fetching weather: {e}") from e

    cw = data.get("current_weather") or {}
    cur_desc, cur_icon = _describe(cw.get("weathercode", -1))

    daily = data.get("daily") or {}
    dates    = daily.get("time", [])
    tmax     = daily.get("temperature_2m_max", [])
    tmin     = daily.get("temperature_2m_min", [])
    codes    = daily.get("weathercode", [])

    forecast = []
    for i in range(min(5, len(dates))):
        desc, icon = _describe(codes[i]) if i < len(codes) else ("Unknown", "❓")
        forecast.append({
            "date":        dates[i],
            "temp_max_c":  tmax[i] if i < len(tmax) else None,
            "temp_min_c":  tmin[i] if i < len(tmin) else None,
            "description": desc,
            "icon":        icon,
        })

    return {
        "city":    loc["name"],
        "country": loc["country"],
        "region":  loc["admin1"],
        "current": {
            "temp_c":      cw.get("temperature"),
            "wind_kph":    cw.get("windspeed"),
            "description": cur_desc,
            "icon":        cur_icon,
        },
        "forecast": forecast,
    }
