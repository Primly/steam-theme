"""Stage 4: apply the Windows theme.

Wallpaper strategy: compose ONE span-style image over the bounding box of all
monitors, with each monitor's region center-cropped from its designated art
(hero / logo / icon). Windows' "Span" wallpaper style maps it 1:1, which gives
correct per-monitor crops without needing the IDesktopWallpaper COM interface.

Then: registry accent + dark/light flags are written (so even a partial theme
apply keeps the colors), a .theme INI is generated, and Windows applies it as
if the user double-clicked it.
"""

import ctypes
import os
import subprocess
import winreg

# shared compositing lives in wallpaper.py (platform-agnostic); re-exported
# here so existing imports (main.py, tests) keep working
from wallpaper import (  # noqa: F401
    assign_roles, compose_monitor_tile, compose_wallpaper, _ini_safe,
)

ABGR_OPAQUE = 0xFF000000
THEME_FILENAME = "theme.theme"


# ------------------------------------------------------------ monitor layout

def _friendly_monitor_names():
    """Map PNP model code (e.g. 'SAM7419') -> friendly name (e.g. 'LS49AG95')
    via WMI root\\wmi WmiMonitorID. Returns {} on any failure."""
    try:
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()  # required when called from a worker thread
        try:
            locator = win32com.client.Dispatch("WbemScripting.SWbemLocator")
            svc = locator.ConnectServer(".", "root\\wmi")
            out = {}
            for m in svc.ExecQuery("SELECT InstanceName, UserFriendlyName FROM WmiMonitorID"):
                try:
                    name = "".join(chr(c) for c in (m.UserFriendlyName or []) if c)
                    parts = (m.InstanceName or "").split("\\")
                    if name and len(parts) >= 2:
                        out[parts[1].upper()] = name
                except Exception:
                    pass
            return out
        finally:
            pythoncom.CoUninitialize()
    except Exception:
        return {}


def enumerate_monitors():
    """Return [{'device', 'desc', 'rect': (l,t,r,b), 'primary'}]."""
    import win32api
    friendly = _friendly_monitor_names()
    monitors = []
    for hmon, _hdc, rect in win32api.EnumDisplayMonitors():
        info = win32api.GetMonitorInfo(hmon)
        device = info.get("Device", "")
        desc = ""
        try:
            desc = win32api.EnumDisplayDevices(device, 0).DeviceString
            # EDD_GET_DEVICE_INTERFACE_NAME=1 gives DeviceID like 'MONITOR\\SAM7419\\...'
            dev_id = win32api.EnumDisplayDevices(device, 0, 1).DeviceID or ""
            # '\\?\DISPLAY#RTK0003#5&...' -> normalize separators, grab 'RTK0003'
            parts = dev_id.replace("#", "\\").split("\\")
            if len(parts) >= 5 and parts[4].upper() in friendly:
                desc = friendly[parts[4].upper()]
        except Exception:
            pass
        monitors.append({
            "device": device,
            "desc": desc,
            "rect": rect,
            "primary": bool(info.get("Flags", 0) & 1),
        })
    return monitors


# ------------------------------------------------- original-look snapshot

_BACKUP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "windows_backup.json")


def _reg_get(root, path, name):
    try:
        with winreg.OpenKey(root, path) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def snapshot_windows_look(log=print):
    """Save the user's current wallpaper + colors once, before the first
    theme apply, so restore_windows_look() can undo the app later. Never
    overwrites an existing snapshot (that would capture one of OUR themes)."""
    if os.path.exists(_BACKUP_PATH):
        return
    import json
    snap = {
        "dwm": {n: _reg_get(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\DWM", n)
                for n in ("AccentColor", "AccentColorInactive",
                          "ColorPrevalence", "EnableWindowColorization")},
        "personalize": {n: _reg_get(winreg.HKEY_CURRENT_USER,
                                    r"Software\Microsoft\Windows\CurrentVersion"
                                    r"\Themes\Personalize", n)
                        for n in ("AppsUseLightTheme", "SystemUsesLightTheme",
                                  "ColorPrevalence")},
        "desktop": {n: _reg_get(winreg.HKEY_CURRENT_USER,
                                r"Control Panel\Desktop", n)
                    for n in ("Wallpaper", "WallpaperStyle", "TileWallpaper")},
    }
    try:
        with open(_BACKUP_PATH, "w", encoding="utf-8") as f:
            json.dump(snap, f, indent=2)
        log("  [backup] snapshot of the previous Windows look saved "
            "(windows_backup.json)")
    except OSError as e:
        log(f"  [backup] snapshot failed (restore unavailable): {e}")


def restore_windows_look(log=print):
    """Write the snapshot back: registry colors/modes + original wallpaper."""
    import json
    with open(_BACKUP_PATH, encoding="utf-8") as f:
        snap = json.load(f)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                          r"Software\Microsoft\Windows\DWM") as k:
        for n, v in snap.get("dwm", {}).items():
            if v is not None:
                winreg.SetValueEx(k, n, 0, winreg.REG_DWORD, int(v))
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                          r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
        for n, v in snap.get("personalize", {}).items():
            if v is not None:
                winreg.SetValueEx(k, n, 0, winreg.REG_DWORD, int(v))
    desktop = snap.get("desktop", {})
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                          r"Control Panel\Desktop") as k:
        for n in ("WallpaperStyle", "TileWallpaper"):
            if desktop.get(n) is not None:
                winreg.SetValueEx(k, n, 0, winreg.REG_SZ, str(desktop[n]))
    wallpaper = desktop.get("Wallpaper")
    if wallpaper and os.path.exists(wallpaper):
        # SPI_SETDESKWALLPAPER=20, SPIF_UPDATEINIFILE|SPIF_SENDCHANGE=3
        ctypes.windll.user32.SystemParametersInfoW(20, 0, wallpaper, 3)
    HWND_BROADCAST, WM_SETTINGCHANGE = 0xFFFF, 0x001A
    SMTO_ABORTIFHUNG = 0x0002
    result = ctypes.c_ulong()
    ctypes.windll.user32.SendMessageTimeoutW(
        HWND_BROADCAST, WM_SETTINGCHANGE, 0, "ImmersiveColorSet",
        SMTO_ABORTIFHUNG, 500, ctypes.byref(result))
    log("  [restore] previous Windows look restored")


# ----------------------------------------------------------- registry colors

def _hex_to_abgr(hexcolor):
    """0XAABBGGRR — used by .theme ColorizationColor."""
    hexcolor = hexcolor.lstrip("#")
    r, g, b = (int(hexcolor[i:i + 2], 16) for i in (0, 2, 4))
    return ABGR_OPAQUE | (b << 16) | (g << 8) | r


def _hex_to_argb(hexcolor):
    """0xAARRGGBB — used by HKCU\\...\\DWM AccentColor."""
    hexcolor = hexcolor.lstrip("#")
    r, g, b = (int(hexcolor[i:i + 2], 16) for i in (0, 2, 4))
    return ABGR_OPAQUE | (r << 16) | (g << 8) | b


def _resolve_mode(mode, appearance):
    """mode: 'match' | 'dark' | 'light' -> concrete 'dark' | 'light'."""
    return appearance if mode in (None, "match", "") else mode


def write_registry_colors(palette, log=print, system_mode="match", app_mode="match"):
    accent = _hex_to_argb(palette["accent"])
    sys_light = 1 if _resolve_mode(system_mode, palette["appearance"]) == "light" else 0
    app_light = 1 if _resolve_mode(app_mode, palette["appearance"]) == "light" else 0
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                          r"Software\Microsoft\Windows\DWM") as k:
        winreg.SetValueEx(k, "AccentColor", 0, winreg.REG_DWORD, accent)
        winreg.SetValueEx(k, "AccentColorInactive", 0, winreg.REG_DWORD,
                          0xFF444444 if not sys_light else 0xFFCCCCCC)
        winreg.SetValueEx(k, "ColorPrevalence", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(k, "EnableWindowColorization", 0, winreg.REG_DWORD, 1)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                          r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
        winreg.SetValueEx(k, "AppsUseLightTheme", 0, winreg.REG_DWORD, app_light)
        winreg.SetValueEx(k, "SystemUsesLightTheme", 0, winreg.REG_DWORD, sys_light)
        winreg.SetValueEx(k, "ColorPrevalence", 0, winreg.REG_DWORD, 1)
    # notify running apps of the personalization change
    HWND_BROADCAST, WM_SETTINGCHANGE = 0xFFFF, 0x001A
    SMTO_ABORTIFHUNG = 0x0002
    result = ctypes.c_ulong()
    ctypes.windll.user32.SendMessageTimeoutW(
        HWND_BROADCAST, WM_SETTINGCHANGE, 0, "ImmersiveColorSet",
        SMTO_ABORTIFHUNG, 500, ctypes.byref(result))
    log(f"  [reg] accent {palette['accent']} written, "
        f"appearance={palette['appearance']} "
        f"(system={'light' if sys_light else 'dark'}, "
        f"apps={'light' if app_light else 'dark'})")


# ---------------------------------------------------------------- .theme file

THEME_TEMPLATE = """\
[Theme]
DisplayName={display_name}

[Control Panel\\Desktop]
Wallpaper={wallpaper}
TileWallpaper=0
WallpaperStyle=22
Pattern=
MultimonBackgrounds=0
WindowsSpotlight=0

[VisualStyles]
Path=%SystemRoot%\\resources\\themes\\Aero\\Aero.msstyles
ColorStyle=NormalColor
Size=NormalSize
AutoColorization=0
ColorizationColor=0X{colorization:08X}
VisualStyleVersion=10

[MasterThemeSelector]
MTSM=DABJDKT
"""


def write_theme_file(path, display_name, wallpaper_path, palette):
    # .theme INI is safest as ASCII; strip any fancy characters from game names
    display_name = _ini_safe(display_name.encode("ascii", "replace").decode())
    content = THEME_TEMPLATE.format(
        display_name=display_name,
        wallpaper=os.path.abspath(wallpaper_path),
        colorization=_hex_to_abgr(palette["accent"]),
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def apply_theme(theme_path, log=print, cfg=None):
    """Apply by 'double-clicking' the .theme file; Windows performs the full
    visual transition. Note: applying while the workstation is LOCKED can
    produce a half-applied 'hybrid Custom' theme — the scheduled task includes
    an on-unlock trigger that re-runs with --reapply.

    cfg is unused on Windows (linux_theme needs it for monitor role mapping).
    """
    # A lingering Settings window (from a previous .theme launch) silently
    # swallows subsequent launches — close it first so the apply lands.
    subprocess.run(["taskkill", "/F", "/IM", "SystemSettings.exe"],
                   capture_output=True)
    # ShellExecute 'open' — same as double-clicking the file, but with no
    # cmd.exe in between to misparse shell metacharacters in the path
    os.startfile(os.path.abspath(theme_path))
    log(f"  [theme] launched {theme_path}")
