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

from PIL import Image, ImageEnhance, ImageFilter

ABGR_OPAQUE = 0xFF000000


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


def assign_roles(monitors, mapping, log=print):
    """Attach a 'role' (hero/logo/icon) to each monitor.

    Config matches win by substring against the monitor description;
    unmatched monitors default by size: largest=hero, second=logo, third=icon.
    """
    defaults = ["hero", "logo", "icon"]
    by_area = sorted(monitors, key=lambda m: (m["rect"][2] - m["rect"][0])
                     * (m["rect"][3] - m["rect"][1]), reverse=True)
    for i, m in enumerate(by_area):
        m["role"] = defaults[i] if i < len(defaults) else "hero"
    for entry in mapping:
        needle = entry["match"].lower()
        for m in monitors:
            if needle in m["desc"].lower() or needle in m["device"].lower():
                m["role"] = entry["role"]
    for m in monitors:
        log(f"  [mon] {m['device']} {m['desc']!r} "
            f"{m['rect'][2]-m['rect'][0]}x{m['rect'][3]-m['rect'][1]} -> {m['role']}")
    return monitors


# ------------------------------------------------------------- compositing

def _cover(img, w, h):
    """Resize+center-crop so img exactly fills w x h."""
    iw, ih = img.size
    scale = max(w / iw, h / ih)
    img = img.resize((max(1, round(iw * scale)), max(1, round(ih * scale))),
                     Image.LANCZOS)
    x = (img.size[0] - w) // 2
    y = (img.size[1] - h) // 2
    return img.crop((x, y, x + w, y + h))


def _fit(img, w, h, max_upscale=3.0):
    """Resize to fit inside w x h, preserving aspect (limited upscaling)."""
    iw, ih = img.size
    scale = min(min(w / iw, h / ih), max_upscale)
    return img.resize((max(1, round(iw * scale)), max(1, round(ih * scale))),
                     Image.LANCZOS)


def _has_transparency(img):
    if img.mode in ("RGBA", "LA"):
        lo, _hi = img.getchannel("A").getextrema()
        return lo < 250
    return img.mode == "P" and "transparency" in img.info


def _centered_on_backdrop(img, hero, w, h):
    """For logos/icons: darkened blurred hero backdrop + art centered."""
    backdrop = _cover(hero.copy(), w, h).filter(ImageFilter.GaussianBlur(24))
    backdrop = ImageEnhance.Brightness(backdrop).enhance(0.4)
    fg = _fit(img.convert("RGBA"), round(w * 0.8), round(h * 0.7))
    x, y = (w - fg.size[0]) // 2, (h - fg.size[1]) // 2
    backdrop.paste(fg, (x, y), fg)
    return backdrop.convert("RGB")


def compose_wallpaper(monitors, art, out_path, log=print):
    min_x = min(m["rect"][0] for m in monitors)
    min_y = min(m["rect"][1] for m in monitors)
    max_x = max(m["rect"][2] for m in monitors)
    max_y = max(m["rect"][3] for m in monitors)
    W, H = max_x - min_x, max_y - min_y

    hero = art.get("hero") or next(iter(art.values()))
    canvas = _cover(Image.open(hero).convert("RGB"), W, H)  # fills any gaps

    for m in monitors:
        src = art.get(m["role"]) or hero
        l, t, r, b = m["rect"]
        w, h = r - l, b - t
        img = Image.open(src)
        iw, ih = img.size
        # scene art covers its monitor; logos/icons (transparent, oddly
        # proportioned, or small) get centered on a blurred hero backdrop
        covers = (iw >= w * 0.6 and ih >= h * 0.6
                  and abs((iw / ih) / (w / h) - 1) < 0.35)
        centered = (m["role"] in ("logo", "icon") and src != hero
                    and (_has_transparency(img) or not covers))
        if centered:
            log(f"  [wall] {m['role']} art is transparent/small -> centered treatment")
            tile = _centered_on_backdrop(img, Image.open(hero).convert("RGB"), w, h)
        else:
            tile = _cover(img.convert("RGB"), w, h)
        canvas.paste(tile, (l - min_x, t - min_y))

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    canvas.save(out_path, "JPEG", quality=92)
    log(f"  [wall] composed {W}x{H} span wallpaper -> {out_path}")
    return out_path


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


def write_registry_colors(palette, log=print):
    accent = _hex_to_argb(palette["accent"])
    light = 1 if palette["appearance"] == "light" else 0
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                          r"Software\Microsoft\Windows\DWM") as k:
        winreg.SetValueEx(k, "AccentColor", 0, winreg.REG_DWORD, accent)
        winreg.SetValueEx(k, "AccentColorInactive", 0, winreg.REG_DWORD,
                          0xFF444444 if not light else 0xFFCCCCCC)
        winreg.SetValueEx(k, "ColorPrevalence", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(k, "EnableWindowColorization", 0, winreg.REG_DWORD, 1)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                          r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
        winreg.SetValueEx(k, "AppsUseLightTheme", 0, winreg.REG_DWORD, light)
        winreg.SetValueEx(k, "SystemUsesLightTheme", 0, winreg.REG_DWORD, light)
        winreg.SetValueEx(k, "ColorPrevalence", 0, winreg.REG_DWORD, 1)
    # notify running apps of the personalization change
    HWND_BROADCAST, WM_SETTINGCHANGE = 0xFFFF, 0x001A
    SMTO_ABORTIFHUNG = 0x0002
    result = ctypes.c_ulong()
    ctypes.windll.user32.SendMessageTimeoutW(
        HWND_BROADCAST, WM_SETTINGCHANGE, 0, "ImmersiveColorSet",
        SMTO_ABORTIFHUNG, 500, ctypes.byref(result))
    log(f"  [reg] accent {palette['accent']} written, "
        f"appearance={palette['appearance']}")


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
    display_name = display_name.encode("ascii", "replace").decode()
    content = THEME_TEMPLATE.format(
        display_name=display_name,
        wallpaper=os.path.abspath(wallpaper_path),
        colorization=_hex_to_abgr(palette["accent"]),
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def apply_theme(theme_path, log=print):
    """Apply by 'double-clicking' the .theme file; Windows performs the full
    visual transition. Note: applying while the workstation is LOCKED can
    produce a half-applied 'hybrid Custom' theme — the scheduled task includes
    an on-unlock trigger that re-runs with --reapply."""
    # A lingering Settings window (from a previous .theme launch) silently
    # swallows subsequent launches — close it first so the apply lands.
    subprocess.run(["taskkill", "/F", "/IM", "SystemSettings.exe"],
                   capture_output=True)
    subprocess.Popen(["cmd", "/c", "start", "", os.path.abspath(theme_path)],
                     shell=False)
    log(f"  [theme] launched {theme_path}")
