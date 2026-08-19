"""
actions/weather_widget.py

Lets LITE update or collapse the weather section on its own interface —
a permanent part of the right sidebar in ui.py (alongside ACTIVITY LOG,
FILE UPLOAD, COMMAND INPUT), not a separate popup window.

This is separate from actions/weather_api.py (the data-fetching module the
section itself uses) and actions/weather_report.py (the older voice command
that just opens a browser search) — this file only controls that section's
visibility/city, via the thread-safe show_weather_panel()/
hide_weather_panel() methods on the LiteUI wrapper (see ui.py's LiteUI
class), which marshal onto the GUI thread via Qt signals since this runs
off the GUI thread like every other action.
"""

from __future__ import annotations


def weather_widget(parameters: dict, player=None, session_memory=None, speak=None) -> str:
    action = (parameters or {}).get("action", "show").strip().lower()
    city   = (parameters or {}).get("city", "").strip()

    if player is None:
        return "No display available to show the weather widget on, sir."

    if action in ("hide", "close", "dismiss", "collapse"):
        player.hide_weather_panel()
        return "Weather section collapsed, sir."

    # default: show/update
    player.show_weather_panel(city)
    if city:
        return f"Weather section updated for {city}, sir."
    return "Weather section is visible in the sidebar, sir."
