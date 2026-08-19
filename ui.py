#ui.py
"""
LiteUI — the app's window/HUD facade.

Renders the 3D holographic HUD (Three.js/WebGL, hologram/index.html) inside
a QWebEngineView, bridged to Python via QWebChannel.

Public API (every method/property that main.py and actions/*.py rely on):
  muted (get/set), toggle_mute(), set_muted(), current_file,
  on_text_command / on_remote_clicked / on_interrupt (get/set callbacks),
  notify_phone_connected(), set_state(), write_log(), wait_for_api_key(),
  show_content(), prompt_reconfig(), show_camera_frame(),
  start_camera_stream() / stop_camera_stream(), show_weather_panel() /
  hide_weather_panel(), assistant_name, start_speaking() / stop_speaking(),
  root (with .mainloop()), _win (with ._ready for one direct access in
  main.py's reconnect loop).
"""
import base64
import functools
import http.server
import json
import os
import sys
import threading
import time
from pathlib import Path

from PyQt6.QtCore import QObject, Qt, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication, QFileDialog, QMainWindow

# Qt's documented requirement for QtWebEngine: this attribute must be set
# before any QApplication is constructed. In the real app's execution order
# (main.py imports `ui` before constructing QApplication), this already
# happens naturally — set explicitly here too as a defensive safety net in
# case this module is ever imported in a different order.
QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)

from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebChannel import QWebChannel


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


BASE_DIR    = _get_base_dir()
CONFIG_DIR  = BASE_DIR / "config"
API_FILE    = CONFIG_DIR / "api_keys.json"
HOLOGRAM_DIR = BASE_DIR / "hologram"
HTML_PATH   = HOLOGRAM_DIR / "index.html"


def _read_full_config() -> dict:
    """Read api_keys.json config dict. Returns {} on any error."""
    try:
        return json.loads(API_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


class _HologramRequestHandler(http.server.SimpleHTTPRequestHandler):
    """
    Serves hologram/ as normal, with one special case: /favicon.ico is
    served from config/lite.ico (outside the hologram/ root) so the
    browser's automatic favicon request doesn't 404 — no need to keep a
    duplicate icon file in sync inside hologram/.
    """
    def do_GET(self):
        if self.path == "/favicon.ico":
            ico_path = CONFIG_DIR / "lite.ico"
            if ico_path.exists():
                data = ico_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/x-icon")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
        super().do_GET()

    def log_message(self, fmt, *args):
        pass  # keep the console clean — file-serving noise isn't useful signal


class _HologramHTTPServer:
    """
    Serves hologram/ over local HTTP instead of loading it as a bare
    file:// page. This matters because Chromium (which powers QtWebEngine)
    refuses to execute `import` statements inside <script type="module">
    tags when the page's own origin is file:// — even importing a normal
    https:// CDN URL gets blocked by its CORS check. Since the whole
    Three.js scene, the QWebChannel bridge setup, and the live clock/
    weather-update code all live inside one such module script, loading
    over file:// made the entire thing silently die at the first `import`,
    before anything after it ever ran (no 3D scene, no live bridge — the
    page just showed its static placeholder HTML forever). A plain local
    HTTP origin has no such restriction.

    Binds to 127.0.0.1 on an OS-assigned free port — not reachable from
    the network, just gives the page a real http:// origin to run under.
    """
    def __init__(self, root: Path):
        handler = functools.partial(_HologramRequestHandler, directory=str(root))
        # port=0 -> OS picks any free local port
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def url(self, path: str = "index.html") -> str:
        return f"http://127.0.0.1:{self.port}/{path}"

    def shutdown(self):
        try:
            self.server.shutdown()
        except Exception:
            pass


class _RootShim:
    """Matches the old ui.py's `.root.mainloop()` contract used by main.py."""
    def __init__(self, app: QApplication):
        self._app = app

    def mainloop(self):
        self._app.exec()


class Bridge(QObject):
    """
    JS -> Python direction. The HUD's JS calls these via
    window.pybridge.<method>(...) once the QWebChannel handshake completes.
    """
    textCommand       = pyqtSignal(str)
    muteToggleRequest = pyqtSignal()
    interruptRequest  = pyqtSignal()
    fileUploadRequest = pyqtSignal()
    weatherRequest     = pyqtSignal(str)
    setupSubmitted     = pyqtSignal(str)   # JSON string
    customizeSubmitted = pyqtSignal(str)   # JSON string
    toggleBriefRequest     = pyqtSignal()
    toggleAutostartRequest = pyqtSignal()
    createShortcutRequest  = pyqtSignal()
    toggleFullscreenRequest = pyqtSignal()
    remoteConnectRequest    = pyqtSignal()
    settingsOpenRequest     = pyqtSignal()
    hudReady          = pyqtSignal()

    @pyqtSlot(str)
    def sendTextCommand(self, text: str):
        self.textCommand.emit(text)

    @pyqtSlot()
    def requestMuteToggle(self):
        self.muteToggleRequest.emit()

    @pyqtSlot()
    def requestInterrupt(self):
        self.interruptRequest.emit()

    @pyqtSlot()
    def requestFileUpload(self):
        self.fileUploadRequest.emit()

    @pyqtSlot(str)
    def requestWeather(self, city: str):
        self.weatherRequest.emit(city)

    @pyqtSlot(str)
    def submitSetup(self, cfg_json: str):
        self.setupSubmitted.emit(cfg_json)

    @pyqtSlot(str)
    def submitCustomize(self, cfg_json: str):
        self.customizeSubmitted.emit(cfg_json)

    @pyqtSlot()
    def requestToggleBrief(self):
        self.toggleBriefRequest.emit()

    @pyqtSlot()
    def requestToggleAutostart(self):
        self.toggleAutostartRequest.emit()

    @pyqtSlot()
    def requestCreateShortcut(self):
        self.createShortcutRequest.emit()

    @pyqtSlot()
    def requestToggleFullscreen(self):
        self.toggleFullscreenRequest.emit()

    @pyqtSlot()
    def requestRemoteConnect(self):
        self.remoteConnectRequest.emit()

    @pyqtSlot()
    def requestSettingsOpen(self):
        self.settingsOpenRequest.emit()

    @pyqtSlot()
    def notifyHudReady(self):
        self.hudReady.emit()


class HudWindow(QMainWindow):
    # Python -> JS direction. Emitting these from any thread is safe; each
    # is connected to a GUI-thread slot that pushes into the page via
    # runJavaScript(). This mirrors the old ui.py's thread-safety pattern
    # exactly (_log_sig, _state_sig, etc. existed there too).
    _log_sig          = pyqtSignal(str)
    _state_sig        = pyqtSignal(str)
    _content_sig      = pyqtSignal(str, str)
    _reconfig_sig     = pyqtSignal()
    _camera_sig       = pyqtSignal(bytes)
    _cam_stream_sig   = pyqtSignal(bool)
    _weather_show_sig = pyqtSignal(str)
    _weather_hide_sig = pyqtSignal()
    _mute_toggle_sig  = pyqtSignal()
    _mute_set_sig     = pyqtSignal(bool)
    _toast_sig        = pyqtSignal(str, str)
    _run_js_sig       = pyqtSignal(str)   # raw JS push — the ONE safe entry point, see _run_js() below

    def __init__(self, face_path: str = ""):
        super().__init__()
        self.setWindowTitle("L.I.T.E.")
        self.resize(1360, 840)
        self.setStyleSheet("background: #00060a;")

        try:
            ico_path = CONFIG_DIR / "lite.ico"
            if ico_path.exists():
                self.setWindowIcon(QIcon(str(ico_path)))
        except Exception:
            pass

        self._view = QWebEngineView(self)
        self.setCentralWidget(self._view)

        self._bridge  = Bridge()
        self._channel = QWebChannel()
        self._channel.registerObject("pybridge", self._bridge)
        self._view.page().setWebChannel(self._channel)

        # Serve the HUD over local HTTP rather than file:// — see
        # _HologramHTTPServer's docstring for why this matters.
        self._http_server = _HologramHTTPServer(HOLOGRAM_DIR)

        # ---- state (source of truth lives here, not in JS) ----
        self._muted           = False
        self._ready           = self._check_config()
        self._assistant_name  = (_read_full_config().get("assistant_name") or "LITE").strip()
        self._current_file: str | None = None
        self._last_city        = "Lagos"
        self._hud_ready         = False
        self._pending_js: list[str] = []   # queued pushes until the page/bridge is actually ready

        self.on_text_command   = None
        self.on_remote_clicked = None
        self.on_interrupt      = None

        # ---- wire signals -> JS pushes (thread-safe entry points) ----
        self._log_sig.connect(self._js_log)
        self._state_sig.connect(self._js_state)
        self._content_sig.connect(self._js_content)
        self._reconfig_sig.connect(self._js_setup_needed)
        self._camera_sig.connect(self._js_camera_frame)
        self._cam_stream_sig.connect(self._js_camera_stream)
        self._weather_show_sig.connect(self._on_weather_show)
        self._weather_hide_sig.connect(self._js_weather_hide)
        self._mute_toggle_sig.connect(self._toggle_mute)
        self._mute_set_sig.connect(self._set_muted)
        self._toast_sig.connect(self._js_toast)
        self._run_js_sig.connect(self._run_js_on_gui_thread)

        # ---- wire bridge (JS -> Python) ----
        self._bridge.textCommand.connect(self._on_text_command_recv)
        self._bridge.muteToggleRequest.connect(self._toggle_mute)
        self._bridge.interruptRequest.connect(self._on_interrupt_recv)
        self._bridge.fileUploadRequest.connect(self._on_file_upload_recv)
        self._bridge.weatherRequest.connect(self._on_weather_request)
        self._bridge.setupSubmitted.connect(self._on_setup_submitted)
        self._bridge.customizeSubmitted.connect(self._on_customize_submitted)
        self._bridge.toggleBriefRequest.connect(self._on_toggle_brief)
        self._bridge.toggleAutostartRequest.connect(self._on_toggle_autostart)
        self._bridge.createShortcutRequest.connect(self._on_create_shortcut)
        self._bridge.toggleFullscreenRequest.connect(self._on_toggle_fullscreen)
        self._bridge.remoteConnectRequest.connect(self._on_remote_connect)
        self._bridge.settingsOpenRequest.connect(self._js_setup_needed)
        self._bridge.hudReady.connect(self._on_hud_ready)

        self._view.load(QUrl(self._http_server.url("index.html")))

    # ---------- JS push plumbing ----------
    def _run_js(self, script: str):
        """
        Thread-safe — safe to call from ANY thread, not just the GUI thread.
        This one bug (a background thread calling page().runJavaScript()
        directly, bypassing thread-safety) was real: the weather-fetch
        background thread did exactly that, which is undefined behavior in
        Qt and could crash or hang the app. Routing every call through this
        signal, unconditionally, closes that off for every caller —
        present and future — not just the one that got caught.
        """
        self._run_js_sig.emit(script)

    def _run_js_on_gui_thread(self, script: str):
        if not self._hud_ready:
            self._pending_js.append(script)
            return
        self._view.page().runJavaScript(script)

    def _flush_pending_js(self):
        pending, self._pending_js = self._pending_js, []
        for script in pending:
            self._view.page().runJavaScript(script)

    def _js_log(self, text: str):
        self._run_js(f"window.LiteHud && window.LiteHud.onLog({json.dumps(text)})")

    def _js_state(self, state: str):
        self._run_js(f"window.LiteHud && window.LiteHud.onState({json.dumps(state)})")

    def _js_content(self, title: str, text: str):
        self._run_js(
            f"window.LiteHud && window.LiteHud.onContent({json.dumps(title)}, {json.dumps(text)})"
        )

    def _js_setup_needed(self):
        from memory.config_manager import get_brief_enabled
        from core.system_integration import check_autostart

        cfg = _read_full_config()
        payload = dict(cfg)
        payload["brief_enabled"]     = get_brief_enabled()
        payload["autostart_enabled"] = check_autostart()
        self._run_js(
            f"window.LiteHud && window.LiteHud.onSetupNeeded({json.dumps(json.dumps(payload))})"
        )

    def _js_camera_frame(self, img_bytes: bytes):
        b64 = base64.b64encode(img_bytes).decode("ascii")
        self._run_js(f"window.LiteHud && window.LiteHud.onCameraFrame({json.dumps(b64)})")

    def _js_camera_stream(self, active: bool):
        fn = "onCameraStreamStart" if active else "onCameraStreamStop"
        self._run_js(f"window.LiteHud && window.LiteHud.{fn}()")

    def _js_weather_hide(self):
        self._run_js("window.LiteHud && window.LiteHud.onWeatherHide()")

    def _js_toast(self, kicker: str, text: str):
        self._run_js(
            f"window.LiteHud && window.LiteHud.onToast({json.dumps(kicker)}, {json.dumps(text)})"
        )

    # ---------- weather (background fetch, same data source as before) ----------
    def _on_weather_show(self, city: str):
        target = (city or self._last_city or "Lagos").strip()
        self._last_city = target
        threading.Thread(target=self._fetch_weather, args=(target,), daemon=True).start()

    def _on_weather_request(self, city: str):
        self._on_weather_show(city)

    def _fetch_weather(self, city: str):
        try:
            from actions.weather_api import get_weather_for_city
            data = get_weather_for_city(city)
            self._run_js(
                f"window.LiteHud && window.LiteHud.onWeather({json.dumps(json.dumps(data))})"
            )
        except Exception as e:
            self._toast_sig.emit("WEATHER ERROR", str(e)[:120])

    # ---------- mute ----------
    def _toggle_mute(self):
        self._muted = not self._muted
        self._run_js(f"window.LiteHud && window.LiteHud.onMuted({'true' if self._muted else 'false'})")
        if self._muted:
            self._state_sig.emit("MUTED")
            self._log_sig.emit("SYS: Microphone muted.")
        else:
            self._state_sig.emit("LISTENING")
            self._log_sig.emit("SYS: Microphone active.")

    def _set_muted(self, value: bool):
        if value != self._muted:
            self._toggle_mute()

    # ---------- bridge receivers (JS -> Python) ----------
    def _on_text_command_recv(self, text: str):
        if self.on_text_command:
            try:
                self.on_text_command(text)
            except Exception as e:
                self._log_sig.emit(f"ERR: text command failed: {e}")

    def _on_interrupt_recv(self):
        if self.on_interrupt:
            try:
                self.on_interrupt()
            except Exception as e:
                self._log_sig.emit(f"ERR: interrupt failed: {e}")

    def _on_file_upload_recv(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select a file for LITE")
        if path:
            self._current_file = path
            name = Path(path).name
            self._run_js(f"window.LiteHud && window.LiteHud.onFileSelected({json.dumps(name)})")
            self._log_sig.emit(f"SYS: File selected — {name}")

    def _on_setup_submitted(self, cfg_json: str):
        try:
            cfg = json.loads(cfg_json)
        except Exception:
            cfg = {}
        os.makedirs(CONFIG_DIR, exist_ok=True)
        existing = _read_full_config()
        # Password fields are never echoed back into the form (correctly, for
        # security) — so re-saving this section without retyping a key must
        # NOT wipe out a previously saved one. Only overwrite fields that
        # were actually given a new non-empty value; keep existing otherwise.
        for key in ("gemini_api_key", "anthropic_api_key", "groq_api_key", "fallback_api_url"):
            new_val = (cfg.get(key) or "").strip()
            if new_val:
                existing[key] = new_val
        if cfg.get("os_system"):
            existing["os_system"] = cfg["os_system"]
        existing.setdefault("os_system", "")
        API_FILE.write_text(json.dumps(existing, indent=4), encoding="utf-8")
        self._ready = True
        has_voice = bool(existing.get("gemini_api_key"))
        note = "" if has_voice else " (voice not configured — add a Gemini key anytime in Settings)"
        self._log_sig.emit(f"SYS: Initialised.{note}")
        self._state_sig.emit("LISTENING")
        self._run_js("window.LiteHud && window.LiteHud.onReady()")

    def _on_customize_submitted(self, cfg_json: str):
        try:
            cfg = json.loads(cfg_json)
        except Exception:
            cfg = {}
        os.makedirs(CONFIG_DIR, exist_ok=True)
        existing = _read_full_config()
        name = (cfg.get("assistant_name") or "").strip() or "LITE"
        existing["assistant_name"] = name
        existing["user_name"]      = (cfg.get("user_name") or "").strip()
        existing["ui_color"]       = (cfg.get("ui_color") or "").strip()
        API_FILE.write_text(json.dumps(existing, indent=4), encoding="utf-8")
        self._assistant_name = name
        self._run_js(f"window.LiteHud && window.LiteHud.onAssistantName({json.dumps(name)})")
        self._log_sig.emit(f"SYS: Assistant settings saved.")

    def _on_toggle_brief(self):
        from memory.config_manager import get_brief_enabled, save_brief_enabled
        new_val = not get_brief_enabled()
        save_brief_enabled(new_val)
        self._log_sig.emit(f"SYS: Morning brief {'enabled' if new_val else 'disabled'}.")
        self._run_js(
            f"window.LiteHud && window.LiteHud.onSettingsState({json.dumps(json.dumps({'brief_enabled': new_val}))})"
        )

    def _on_toggle_autostart(self):
        from core.system_integration import toggle_autostart
        enabled, msg = toggle_autostart(self._assistant_name)
        self._log_sig.emit(f"SYS: {msg}")
        self._run_js(
            f"window.LiteHud && window.LiteHud.onSettingsState({json.dumps(json.dumps({'autostart_enabled': enabled}))})"
        )

    def _on_create_shortcut(self):
        def _run():
            from core.system_integration import create_desktop_shortcut
            msg = create_desktop_shortcut()
            self._log_sig.emit(f"SYS: {msg}")
            self._toast_sig.emit("SHORTCUT", msg)
        threading.Thread(target=_run, daemon=True).start()

    def _on_toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def _on_remote_connect(self):
        if not self.on_remote_clicked:
            self._toast_sig.emit(
                "REMOTE", "Dashboard unavailable — check requirements (fastapi, uvicorn, cryptography)."
            )
            return
        try:
            result = self.on_remote_clicked()
        except Exception as e:
            self._toast_sig.emit("REMOTE ERROR", str(e)[:150])
            return
        if not result:
            self._toast_sig.emit("REMOTE", "Dashboard unavailable.")
            return
        url, key, auto_login_url, manual = result

        qr_b64 = ""
        try:
            import qrcode
            from io import BytesIO
            qr = qrcode.QRCode(box_size=8, border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
            qr.add_data(auto_login_url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = BytesIO()
            img.save(buf, format="PNG")
            qr_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        except ImportError:
            self._log_sig.emit(
                "SYS: QR code needs the 'qrcode' package (pip install qrcode[pil]) — showing link only."
            )
        except Exception as e:
            self._log_sig.emit(f"SYS: QR generation failed ({e}) — showing link only.")

        payload = {"qr": qr_b64, "auto_login_url": auto_login_url, "manual": manual, "key": key}
        self._run_js(
            f"window.LiteHud && window.LiteHud.onRemoteInfo({json.dumps(json.dumps(payload))})"
        )

    def _on_hud_ready(self):
        self._hud_ready = True
        self._flush_pending_js()
        self._run_js(f"window.LiteHud && window.LiteHud.onAssistantName({json.dumps(self._assistant_name)})")
        if not self._ready:
            self._reconfig_sig.emit()
        else:
            # Weather is a persistent HUD element, not something the user
            # should have to ask for — populate it with real data immediately.
            self._weather_show_sig.emit("")

    def _check_config(self) -> bool:
        """
        Only the OS selection is required to consider the app configured —
        the Gemini key (voice) and every fallback provider are optional,
        the app should never block first run on any of them.
        """
        return bool(_read_full_config().get("os_system"))

    def closeEvent(self, event):
        try:
            self._http_server.shutdown()
        except Exception:
            pass
        super().closeEvent(event)


class LiteUI:
    def __init__(self, face_path: str = "face.png", size=None):
        self._app = QApplication.instance() or QApplication(sys.argv)
        self._win = HudWindow(face_path)
        self._win.show()
        self.root = _RootShim(self._app)

    # ---------- mute ----------
    @property
    def muted(self) -> bool:
        return self._win._muted

    @muted.setter
    def muted(self, v: bool):
        if v != self._win._muted:
            self._win._toggle_mute()

    def toggle_mute(self):
        """Thread-safe — safe to call from any thread (e.g. a global hotkey listener)."""
        self._win._mute_toggle_sig.emit()

    def set_muted(self, value: bool):
        """Thread-safe explicit set (mute or unmute)."""
        self._win._mute_set_sig.emit(bool(value))

    # ---------- files ----------
    @property
    def current_file(self) -> str | None:
        return self._win._current_file

    # ---------- callbacks ----------
    @property
    def on_text_command(self):
        return self._win.on_text_command

    @on_text_command.setter
    def on_text_command(self, cb):
        self._win.on_text_command = cb

    @property
    def on_remote_clicked(self):
        return self._win.on_remote_clicked

    @on_remote_clicked.setter
    def on_remote_clicked(self, cb):
        self._win.on_remote_clicked = cb

    @property
    def on_interrupt(self):
        return self._win.on_interrupt

    @on_interrupt.setter
    def on_interrupt(self, cb):
        self._win.on_interrupt = cb

    def notify_phone_connected(self) -> None:
        self._win._toast_sig.emit("REMOTE", "Phone connected.")

    # ---------- core HUD ----------
    def set_state(self, state: str):
        self._win._state_sig.emit(state)

    def write_log(self, text: str):
        self._win._log_sig.emit(text)

    def wait_for_api_key(self):
        while not self._win._ready:
            time.sleep(0.1)

    def show_content(self, title: str, text: str):
        """Thread-safe: display content in the dismissible panel."""
        self._win._content_sig.emit(title[:48], text[:4000])

    def prompt_reconfig(self):
        """Thread-safe: show the setup modal again (e.g. after an auth error)."""
        self._win._ready = False
        self._win._reconfig_sig.emit()

    # ---------- camera ----------
    def show_camera_frame(self, img_bytes: bytes):
        """Thread-safe: push a webcam/screen-capture frame to the preview overlay."""
        self._win._camera_sig.emit(img_bytes)

    def start_camera_stream(self) -> None:
        self._win._cam_stream_sig.emit(True)

    def stop_camera_stream(self) -> None:
        self._win._cam_stream_sig.emit(False)

    # ---------- weather ----------
    def show_weather_panel(self, city: str = "") -> None:
        """Thread-safe: refresh the weather panel, optionally for a specific city."""
        self._win._weather_show_sig.emit(city or "")

    def hide_weather_panel(self) -> None:
        self._win._weather_hide_sig.emit()

    # ---------- misc ----------
    @property
    def assistant_name(self) -> str:
        return self._win._assistant_name

    def start_speaking(self):
        self.set_state("SPEAKING")

    def stop_speaking(self):
        if not self.muted:
            self.set_state("LISTENING")
