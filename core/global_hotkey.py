#global_hotkey.py
"""
System-wide hotkey to mute/unmute LITE — works even when LITE's own window
isn't focused, which is the point: the in-app mute button and F4 shortcut
only work while you're looking at LITE, but the moment you're actually
worth muting for is usually when you're doing something else entirely (on
a call, talking to someone in the room, etc.) and LITE is sitting in the
background.

Uses pynput's global keyboard hook — optional dependency. If it's not
installed, this silently does nothing and the in-app button/F4 still work
fine; nothing else in the app depends on this.

Configure the combo in config/api_keys.json:
    "mute_hotkey": "<ctrl>+<alt>+m"     (default)
pynput's format: https://pynput.readthedocs.io/en/latest/keyboard.html#global-hotkeys
"""
import json
import sys
import threading
from pathlib import Path


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR         = _get_base_dir()
API_CONFIG_PATH  = BASE_DIR / "config" / "api_keys.json"
DEFAULT_COMBO    = "<ctrl>+<alt>+m"


def _load_config() -> dict:
    try:
        return json.loads(API_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _notify(muted: bool):
    """
    Best-effort desktop toast — useful specifically because this fires from
    the background/unfocused case where the HUD's own visual mute state
    isn't necessarily visible. Silently does nothing if unavailable
    (non-Windows, or win10toast not installed).
    """
    if sys.platform != "win32":
        return
    try:
        from win10toast import ToastNotifier
        msg = "Microphone muted — LITE isn't listening." if muted else "Microphone active — LITE is listening again."
        ToastNotifier().show_toast("LITE", msg, duration=4, threaded=True)
    except Exception:
        pass


class GlobalMuteHotkey:
    """
    Starts a background listener for a global mute/unmute hotkey combo.
    Call start() once; stop() to tear it down (e.g. on app exit).
    """
    def __init__(self, ui, combo: str | None = None):
        self.ui       = ui
        self.combo    = combo or _load_config().get("mute_hotkey") or DEFAULT_COMBO
        self._listener = None

    def _on_trigger(self):
        try:
            # Determine the target state before emitting the (async, queued)
            # toggle signal — reading ui.muted right after would race the
            # Qt event loop actually processing it.
            target = not self.ui.muted
            self.ui.toggle_mute()
            _notify(muted=target)
        except Exception as e:
            print(f"[GlobalHotkey] Toggle failed: {e}")

    def start(self) -> bool:
        """Returns True if the listener actually started."""
        try:
            from pynput import keyboard
        except Exception as e:
            print(
                f"[GlobalHotkey] pynput not available ({e}) — global mute hotkey "
                f"disabled. The in-app mute button and F4 (while LITE is focused) "
                f"still work. Install with: pip install pynput"
            )
            return False

        try:
            self._listener = keyboard.GlobalHotKeys({self.combo: self._on_trigger})
            self._listener.start()
            print(f"[GlobalHotkey] Listening for {self.combo} (mute toggle, works system-wide)")
            return True
        except Exception as e:
            print(f"[GlobalHotkey] Failed to register {self.combo!r}: {e}")
            return False

    def stop(self):
        if self._listener:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None


def start_global_mute_hotkey(ui) -> GlobalMuteHotkey | None:
    """Convenience entry point — starts the listener in the background, returns
    the handle (or None if it couldn't start) so the caller can stop() it later."""
    hk = GlobalMuteHotkey(ui)
    hk.start()
    return hk
