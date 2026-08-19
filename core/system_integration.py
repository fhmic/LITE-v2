#system_integration.py
"""
OS-level integration: desktop shortcuts, launch-on-startup registration, and
the LITE arc-reactor .ico generator. Ported unchanged from the old
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
    """Renders a LITE arc-reactor icon at 4x resolution and saves a multi-res .ico."""
    try:
        import math
        import PIL.Image
        import PIL.ImageDraw
        import PIL.ImageFilter
    except ImportError:
        return False

    CYAN, DIM, DARK, GLOW, WHITE = (0, 212, 255), (0, 100, 140), (0, 6, 10), (0, 160, 200), (220, 240, 255)

    def _render(sz: int):
        S = sz * 4
        img = PIL.Image.new("RGBA", (S, S), (0, 0, 0, 0))
        d = PIL.ImageDraw.Draw(img)
        cx = cy = S // 2
        R = S // 2 - 2
        d.ellipse([cx-R, cy-R, cx+R, cy+R], fill=(*DARK, 255))
        lw = max(2, S // 40)
        d.ellipse([cx-R, cy-R, cx+R, cy+R], outline=(*CYAN, 220), width=lw)
        R2 = int(R * 0.72)
        d.ellipse([cx-R2, cy-R2, cx+R2, cy+R2], outline=(*DIM, 180), width=max(1, lw // 2))
        R_inner, R_outer = int(R * 0.30), int(R * 0.62)
        spoke_w = max(1, S // 80)
        for i in range(6):
            angle = math.radians(i * 60 - 30)
            x1 = cx + int(R_inner * math.cos(angle)); y1 = cy + int(R_inner * math.sin(angle))
            x2 = cx + int(R_outer * math.cos(angle)); y2 = cy + int(R_outer * math.sin(angle))
            d.line([x1, y1, x2, y2], fill=(*GLOW, 200), width=spoke_w)
        Ri = int(R * 0.26)
        d.ellipse([cx-Ri, cy-Ri, cx+Ri, cy+Ri], outline=(*CYAN, 255), width=max(2, lw))
        glow_layer = PIL.Image.new("RGBA", (S, S), (0, 0, 0, 0))
        gd = PIL.ImageDraw.Draw(glow_layer)
        Rc = int(R * 0.13)
        gd.ellipse([cx-Rc*2, cy-Rc*2, cx+Rc*2, cy+Rc*2], fill=(*CYAN, 110))
        glow_layer = glow_layer.filter(PIL.ImageFilter.GaussianBlur(S // 14))
        img = PIL.Image.alpha_composite(img, glow_layer)
        d = PIL.ImageDraw.Draw(img)
        d.ellipse([cx-Rc, cy-Rc, cx+Rc, cy+Rc], fill=(*WHITE, 255))
        return img.resize((sz, sz), PIL.Image.LANCZOS)

    try:
        sizes = [256, 128, 64, 48, 32, 16]
        frames = [_render(s) for s in sizes]
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
