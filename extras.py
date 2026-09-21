"""Extra app theming: Windows Terminal color scheme + SignalRGB lighting.

Both are best-effort: failures are logged, never fatal.

SignalRGB notes: its REST API can only APPLY existing effects — effect
parameters are read-only over HTTP. So instead we generate a custom effect
file (HTML canvas gradient built from the theme palette) into the user's
WhirlwindFX Effects folder and apply it by name. SignalRGB discovers new
effect files on launch; afterwards, per-game updates rewrite the same file
and apply instantly.
"""

import json
import os
import winreg

SRGB_EFFECT_TITLE = "Steam Wallpaper"

# terminal slot order used by Windows Terminal schemes
_WT_SLOTS = ["black", "red", "green", "yellow", "blue", "purple", "cyan", "white",
             "brightBlack", "brightRed", "brightGreen", "brightYellow",
             "brightBlue", "brightPurple", "brightCyan", "brightWhite"]


def _wt_settings_paths():
    local = os.environ.get("LOCALAPPDATA", "")
    return [
        os.path.join(local, "Packages", "Microsoft.WindowsTerminal_8wekyb3d8bbwe",
                     "LocalState", "settings.json"),
        os.path.join(local, "Microsoft", "Windows Terminal", "settings.json"),
    ]


def apply_windows_terminal(cfg, palette, theme_name, wallpaper_path, log=print):
    wt = cfg.get("windows_terminal", {})
    if not wt.get("enabled"):
        return
    path = next((p for p in _wt_settings_paths() if os.path.exists(p)), None)
    if not path:
        log("  [wt] settings.json not found, skipping")
        return
    try:
        with open(path, encoding="utf-8-sig") as f:
            settings = json.load(f)
    except Exception as e:
        log(f"  [wt] could not parse settings.json (comments/trailing commas?): {e}")
        return

    colors = (palette["colors"] + palette["colors"])[:16]
    scheme = {
        "name": theme_name,
        "background": palette["background"],
        "foreground": "#E8E8E8" if palette["appearance"] == "dark" else "#1A1A1A",
        "cursorColor": palette["accent"],
        "selectionBackground": palette["accent"],
    }
    scheme.update(dict(zip(_WT_SLOTS, colors)))

    schemes = settings.setdefault("schemes", [])
    schemes[:] = [s for s in schemes if s.get("name") != theme_name]
    schemes.append(scheme)

    defaults = settings.setdefault("profiles", {}).setdefault("defaults", {})
    defaults["colorScheme"] = theme_name
    if wt.get("set_background_image") and wallpaper_path:
        defaults["backgroundImage"] = os.path.abspath(wallpaper_path)
        defaults["backgroundImageOpacity"] = wt.get("background_opacity", 0.25)
        defaults["backgroundImageStretchMode"] = "uniformToFill"

    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=4)
        log(f"  [wt] scheme '{theme_name}' applied")
    except Exception as e:
        log(f"  [wt] failed writing settings.json: {e}")


def _documents_dir():
    """Real Documents folder (handles OneDrive redirection)."""
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion"
                             r"\Explorer\User Shell Folders")
        raw, _ = winreg.QueryValueEx(key, "Personal")
        return os.path.expandvars(raw)
    except OSError:
        return os.path.join(os.path.expanduser("~"), "Documents")


def write_signalrgb_effect(cfg, palette, log=print):
    """Write 'Steam Wallpaper.html' in the configured style, skinned with the
    theme palette. Returns the effect title on success, None otherwise."""
    import srgb_effects
    style = cfg.get("signalrgb", {}).get("effect_style", "gradient")
    html = srgb_effects.render_effect(style, SRGB_EFFECT_TITLE, palette)
    effects_dir = (cfg.get("signalrgb", {}).get("effects_dir")
                   or os.path.join(_documents_dir(), "WhirlwindFX", "Effects"))
    try:
        os.makedirs(effects_dir, exist_ok=True)
        path = os.path.join(effects_dir, f"{SRGB_EFFECT_TITLE}.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        log(f"  [srgb] wrote custom effect -> {path}")
        return SRGB_EFFECT_TITLE
    except OSError as e:
        log(f"  [srgb] could not write custom effect: {e}")
        return None


def apply_signalrgb(cfg, palette, mood, log=print):
    """Apply a SignalRGB effect over its local REST API, choosing the effect
    from signalrgb.effect_map by mood keyword. Endpoint layout can vary by
    SignalRGB version — adjust here if your install differs."""
    srgb = cfg.get("signalrgb", {})
    if not srgb.get("enabled"):
        return
    import requests
    base = srgb.get("base_url", "http://localhost:16038").rstrip("/")
    effect_map = srgb.get("effect_map", {})
    wanted = effect_map.get("default", "Solid Color")
    for key, val in effect_map.items():
        if key != "default" and mood and key in mood.lower():
            wanted = val
            break
    # palette-driven custom effect takes priority over the static mood map
    custom_title = None
    if srgb.get("custom_effect", True):
        custom_title = write_signalrgb_effect(cfg, palette, log)
    try:
        r = requests.get(f"{base}/api/v1/lighting/effects", timeout=5)
        r.raise_for_status()
        items = r.json().get("data", {}).get("items", [])
        effects = [(e.get("attributes", {}).get("name", ""),
                    e.get("links", {}).get("apply")) for e in items]

        def find(name):
            return next(((n, link) for n, link in effects
                         if link and name.lower() in n.lower()), None)

        # priority: custom palette effect -> mood-mapped effect -> default
        match = find(custom_title) if custom_title else None
        if custom_title and not match:
            log(f"  [srgb] custom effect not registered yet — restart SignalRGB "
                f"once to discover it; using mood map for now")
        if not match:
            match = find(wanted)
        if not match and wanted != effect_map.get("default"):
            log(f"  [srgb] {wanted!r} not installed, falling back to default")
            match = find(effect_map.get("default", "Solid Color"))
        if not match:
            log(f"  [srgb] no usable effect; available: {[n for n, _ in effects][:8]}")
            return
        name, apply_link = match
        ra = requests.post(base + apply_link, timeout=5)
        ra.raise_for_status()
        log(f"  [srgb] applied effect '{name}'")
    except Exception as e:
        log(f"  [srgb] SignalRGB not reachable or API mismatch: {e}")
