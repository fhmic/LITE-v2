#system_integration.py
"""
OS-level integration: desktop shortcuts, launch-on-startup registration, and
the LITE circuit/hologram .ico generator. Ported unchanged from the old
QPainter-based ui.py — this logic is OS/filesystem work, not GUI-specific,
so it lives here now and both the new web-based HUD and anything else can
call into it directly.
"""
from __future__ import annotations

import ctypes
import os
import platform
import subprocess
import sys
from pathlib import Path

_OS = platform.system()
BASE_DIR = Path(__file__).resolve().parent.parent


# ── Desktop directory resolution ────────────────────────────────────────────

def get_desktop_dir() -> Path:
    """
    Resolve the user's REAL desktop directory instead of assuming
    ~/Desktop, which breaks when:
      • OneDrive "Known Folder Move" relocates the desktop
        (C:/Users/x/OneDrive/Desktop) — very common on Win 10/11;
      • the XDG desktop is localized on Linux (~/Masaüstü, ~/Schreibtisch, ~/Bureau, …).
    Falls back to ~/Desktop only as a last resort.
    """
    home = Path.home()

    if _OS == "Windows":
        try:
            from ctypes import wintypes

            class _GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                            ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

            fid = _GUID(0xB4BFCC3A, 0xDB2C, 0x424C,
                        (ctypes.c_ubyte * 8)(0xB0, 0x29, 0x7F, 0xE9, 0x9A, 0x87, 0xC6, 0x41))
            buf = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(
                    ctypes.byref(fid), 0, None, ctypes.byref(buf)) == 0:
                p = Path(buf.value)
                ctypes.windll.ole32.CoTaskMemFree(buf)
                if p.is_dir():
                    return p
        except Exception:
            pass
        try:
            import winreg
            with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
                val, _t = winreg.QueryValueEx(key, "Desktop")
            p = Path(os.path.expandvars(val))
            if p.is_dir():
                return p
        except Exception:
            pass

    elif _OS == "Linux":
        try:
            out = subprocess.run(["xdg-user-dir", "DESKTOP"],
                                  capture_output=True, text=True, timeout=5)
            p = Path(out.stdout.strip())
            if out.stdout.strip() and p != home and p.is_dir():
                return p
        except Exception:
            pass
        try:
            cfg = home / ".config" / "user-dirs.dirs"
            for line in cfg.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("XDG_DESKTOP_DIR"):
                    val = line.split("=", 1)[1].strip().strip('"')
                    p = Path(val.replace("$HOME", str(home)))
                    if p != home and p.is_dir():
                        return p
        except Exception:
            pass

    return home / "Desktop"


# ── Icon generation ──────────────────────────────────────────────────────────

def build_lite_icon(out_path: Path) -> bool:
    """
    Renders LITE's circuit/hologram emblem — a HUD-style ring with radiating
    circuit traces around a layered hexagon core, in the app's own accent
    green — and saves a multi-res .ico. This is the fallback generator used
    only when config/lite.ico is missing entirely; it exists so a fresh
    setup (or a wiped config folder) regenerates the SAME icon shipped in
    the repo, not some other placeholder design.
    """
    try:
        import math
        import PIL.Image
        import PIL.ImageDraw
    except ImportError:
        return False

    GREEN_BRIGHT = (0, 255, 65, 255)
    GREEN_MID    = (0, 179, 45, 255)
    GREEN_CORE   = (4, 26, 10, 235)
    TRANSPARENT  = (0, 0, 0, 0)
    SS = 4  # supersample factor for anti-aliasing

    def _hexagon(cx, cy, r, rot=0.0):
        return [
            (cx + r * math.cos(rot + i * (math.pi / 3)), cy + r * math.sin(rot + i * (math.pi / 3)))
            for i in range(6)
        ]

    def _render(sz: int, detailed: bool):
        s = sz * SS
        img = PIL.Image.new("RGBA", (s, s), TRANSPARENT)
        d = PIL.ImageDraw.Draw(img)
        cx = cy = s / 2
        hex_r = s * (0.24 if detailed else 0.26)

        if detailed:
            outer_r, ring_r = s * 0.46, s * 0.38
            tick_w = max(2, int(s * 0.012))
            for i in range(24):
                a = i * (2 * math.pi / 24)
                long_tick = (i % 6 == 0)
                r1, r2 = outer_r, outer_r - (s * 0.045 if long_tick else s * 0.022)
                d.line(
                    [cx + r1 * math.cos(a), cy + r1 * math.sin(a), cx + r2 * math.cos(a), cy + r2 * math.sin(a)],
                    fill=(GREEN_BRIGHT if long_tick else GREEN_MID), width=tick_w,
                )
            d.ellipse([cx - ring_r, cy - ring_r, cx + ring_r, cy + ring_r], outline=GREEN_BRIGHT, width=max(2, int(s * 0.018)))

            for deg in (20, 65, 140, 200, 250, 310):
                a = math.radians(deg)
                x0, y0 = cx + ring_r * math.cos(a), cy + ring_r * math.sin(a)
                elbow_r = ring_r + s * 0.09
                xe, ye = cx + elbow_r * math.cos(a), cy + elbow_r * math.sin(a)
                perp = a + math.pi / 2
                jog = s * 0.045 * (1 if deg in (20, 140, 250) else -1)
                xj, yj = xe + jog * math.cos(perp), ye + jog * math.sin(perp)
                pad_r_out = ring_r + s * 0.16
                xp, yp = cx + pad_r_out * math.cos(a), cy + pad_r_out * math.sin(a)

                line_w = max(2, int(s * 0.014))
                d.line([x0, y0, xe, ye], fill=GREEN_MID, width=line_w)
                d.line([xe, ye, xj, yj], fill=GREEN_MID, width=line_w)
                d.line([xj, yj, xp, yp], fill=GREEN_MID, width=line_w)

                pad_r = s * 0.02
                d.ellipse([xp - pad_r, yp - pad_r, xp + pad_r, yp + pad_r], fill=GREEN_BRIGHT)
                node_r = s * 0.012
                d.ellipse([x0 - node_r, y0 - node_r, x0 + node_r, y0 + node_r], fill=GREEN_BRIGHT)

            d.polygon(_hexagon(cx, cy, hex_r, rot=math.pi / 6), fill=GREEN_CORE, outline=GREEN_BRIGHT, width=max(2, int(s * 0.016)))
            d.polygon(_hexagon(cx, cy, hex_r * 0.62, rot=math.pi / 6), outline=GREEN_MID, width=max(1, int(s * 0.008)))
            dot_r = s * 0.035
        else:
            # Simplified mark for small sizes — bold ring + hex only, no fine
            # traces, so it stays legible instead of turning to mud at 16-48px.
            ring_r = s * 0.40
            d.ellipse([cx - ring_r, cy - ring_r, cx + ring_r, cy + ring_r], outline=GREEN_BRIGHT, width=max(2, int(s * 0.035)))
            d.polygon(_hexagon(cx, cy, hex_r, rot=math.pi / 6), fill=GREEN_CORE, outline=GREEN_BRIGHT, width=max(2, int(s * 0.03)))
            dot_r = s * 0.05

        d.ellipse([cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r], fill=GREEN_BRIGHT)
        return img.resize((sz, sz), PIL.Image.LANCZOS)

    try:
        sizes = [256, 128, 64, 48, 32, 16]
        detailed_cutoff = 64  # 64px and up get the full circuit detail
        frames = [_render(s, detailed=(s >= detailed_cutoff)) for s in sizes]
        frames[0].save(out_path, format="ICO", append_images=frames[1:], sizes=[(s, s) for s in sizes])
        return True
    except Exception as e:
        print(f"[SystemIntegration] Icon generation failed: {e}")
        return False


# ── Desktop shortcut ─────────────────────────────────────────────────────────

def _create_lnk_windows(lnk: str, target: str, args: str, work_dir: str, icon_loc: str) -> None:
    """Creates a Windows .lnk without ever opening a console window."""
    try:
        from win32com.client import Dispatch
        sh = Dispatch("WScript.Shell")
        sc = sh.CreateShortCut(lnk)
        sc.TargetPath = target
        sc.Arguments = f'"{args}"'
        sc.WorkingDirectory = work_dir
        sc.Description = "L.I.T.E. AI Assistant"
        sc.IconLocation = icon_loc
        sc.save()
        return
    except ImportError:
        pass

    vbs = "\n".join([
        'Set ws = CreateObject("WScript.Shell")',
        f'Set sc = ws.CreateShortcut("{lnk}")',
        f'sc.TargetPath = "{target}"',
        f'sc.Arguments = Chr(34) & "{args}" & Chr(34)',
        f'sc.WorkingDirectory = "{work_dir}"',
        'sc.Description = "L.I.T.E. AI Assistant"',
        f'sc.IconLocation = "{icon_loc}"',
        'sc.Save',
    ])
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".vbs")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(vbs)
        proc = subprocess.Popen(
            ["wscript.exe", "/nologo", tmp],
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,
        )
        proc.wait(timeout=10)
    finally:
        try:
            os.unlink(tmp)
        except Exception:
            pass


def create_desktop_shortcut() -> str:
    """Creates a desktop shortcut on Windows/macOS/Linux. Returns a status message."""
    import stat as _stat
    script  = BASE_DIR / "main.py"
    python  = Path(sys.executable)
    desktop = get_desktop_dir()

    ico_path = BASE_DIR / "config" / "lite.ico"
    if not ico_path.exists():
        build_lite_icon(ico_path)

    try:
        if _OS == "Windows":
            pythonw = python.parent / "pythonw.exe"
            target  = str(pythonw if pythonw.exists() else python)
            lnk     = str(desktop / "L.I.T.E.lnk")
            icon_loc = str(ico_path) if ico_path.exists() else f"{target},0"
            _create_lnk_windows(lnk, target, str(script), str(script.parent), icon_loc)

        elif _OS == "Darwin":
            app = desktop / "L.I.T.E.app"
            mac_dir = app / "Contents" / "MacOS"
            res_dir = app / "Contents" / "Resources"
            mac_dir.mkdir(parents=True, exist_ok=True)
            res_dir.mkdir(exist_ok=True)
            launcher = mac_dir / "LITE"
            launcher.write_text(
                "#!/usr/bin/env bash\n"
                f'cd "{script.parent}"\n'
                f'exec "{python}" "{script}"\n'
            )
            launcher.chmod(launcher.stat().st_mode | _stat.S_IEXEC | _stat.S_IXGRP | _stat.S_IXOTH)
            (app / "Contents" / "Info.plist").write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                '<plist version="1.0"><dict>\n'
                '  <key>CFBundleExecutable</key><string>LITE</string>\n'
                '  <key>CFBundleIdentifier</key><string>com.lite.assistant</string>\n'
                '  <key>CFBundleName</key><string>L.I.T.E.</string>\n'
                '  <key>CFBundlePackageType</key><string>APPL</string>\n'
                '  <key>CFBundleVersion</key><string>1.0</string>\n'
                '</dict></plist>\n'
            )
            try:
                import PIL.Image
                icns = res_dir / "AppIcon.icns"
                PIL.Image.open(ico_path).save(icns, format="ICNS")
                plist = app / "Contents" / "Info.plist"
                txt = plist.read_text()
                plist.write_text(txt.replace(
                    '</dict></plist>',
                    '  <key>CFBundleIconFile</key><string>AppIcon</string>\n</dict></plist>\n',
                ))
            except Exception:
                pass

        else:
            png_path = ico_path.with_suffix(".png")
            if not png_path.exists() and ico_path.exists():
                try:
                    import PIL.Image
                    PIL.Image.open(ico_path).resize((256, 256), PIL.Image.LANCZOS).save(png_path, format="PNG")
                except Exception:
                    png_path = ico_path
            icon_line = f"Icon={png_path}\n" if png_path.exists() else ""
            desk = desktop / "L.I.T.E.desktop"
            desk.write_text(
                "[Desktop Entry]\nName=L.I.T.E.\n"
                f"Exec={python} {script}\nPath={script.parent}\n"
                "Type=Application\nTerminal=false\nCategories=Utility;\n" + icon_line
            )
            desk.chmod(desk.stat().st_mode | 0o755)

        return "Desktop shortcut created."
    except Exception as e:
        return f"Shortcut failed — {e}"


# ── Auto-start ───────────────────────────────────────────────────────────────

def check_autostart() -> bool:
    """Returns True if auto-start is currently registered on this OS."""
    try:
        if _OS == "Windows":
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_READ)
            try:
                winreg.QueryValueEx(key, "LITE_AI")
                return True
            except FileNotFoundError:
                return False
            finally:
                winreg.CloseKey(key)
        elif _OS == "Darwin":
            return (Path.home() / "Library" / "LaunchAgents" / "com.lite.assistant.plist").exists()
        else:
            return (Path.home() / ".config" / "autostart" / "lite.desktop").exists()
    except Exception:
        return False


def toggle_autostart(assistant_name: str = "LITE") -> tuple[bool, str]:
    """Toggles auto-start. Returns (new_enabled_state, status_message)."""
    currently_on = check_autostart()
    try:
        script = str(BASE_DIR / "main.py")
        if _OS == "Windows":
            import winreg
            reg = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_ALL_ACCESS)
            if currently_on:
                winreg.DeleteValue(reg, "LITE_AI")
            else:
                pythonw = Path(sys.executable).parent / "pythonw.exe"
                exe = str(pythonw if pythonw.exists() else sys.executable)
                winreg.SetValueEx(reg, "LITE_AI", 0, winreg.REG_SZ, f'"{exe}" "{script}"')
            winreg.CloseKey(reg)
        elif _OS == "Darwin":
            plist_dir = Path.home() / "Library" / "LaunchAgents"
            plist_dir.mkdir(parents=True, exist_ok=True)
            plist = plist_dir / "com.lite.assistant.plist"
            if currently_on:
                plist.unlink(missing_ok=True)
            else:
                plist.write_text(
                    '<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                    '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                    '<plist version="1.0"><dict>\n'
                    '  <key>Label</key><string>com.lite.assistant</string>\n'
                    '  <key>ProgramArguments</key><array>\n'
                    f'    <string>{sys.executable}</string>\n'
                    f'    <string>{script}</string>\n'
                    '  </array>\n  <key>RunAtLoad</key><true/>\n</dict></plist>\n'
                )
        else:
            desk_dir = Path.home() / ".config" / "autostart"
            desk_dir.mkdir(parents=True, exist_ok=True)
            desk = desk_dir / "lite.desktop"
            if currently_on:
                desk.unlink(missing_ok=True)
            else:
                desk.write_text(
                    f"[Desktop Entry]\nName={assistant_name}\n"
                    f"Exec={sys.executable} {script}\n"
                    "Type=Application\nTerminal=false\nX-GNOME-Autostart-enabled=true\n"
                )
        enabled = not currently_on
        return enabled, f"Auto-start {'enabled' if enabled else 'disabled'}."
    except Exception as e:
        return currently_on, f"Auto-start failed — {e}"