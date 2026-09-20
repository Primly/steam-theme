"""Extra app theming: Windows Terminal color scheme + SignalRGB effect.

Both are best-effort: failures are logged, never fatal.
"""

import json
import os

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
    try:
        r = requests.get(f"{base}/api/v1/lighting/effects", timeout=5)
        r.raise_for_status()
        items = r.json().get("data", {}).get("items", [])
        effects = [(e.get("attributes", {}).get("name", ""),
                    e.get("links", {}).get("apply")) for e in items]
        match = next(((n, link) for n, link in effects
                      if link and wanted.lower() in n.lower()), None)
        if not match:
            log(f"  [srgb] no effect matching {wanted!r}; "
                f"available: {[n for n, _ in effects][:8]}")
            return
        name, apply_link = match
        ra = requests.post(base + apply_link, timeout=5)
        ra.raise_for_status()
        log(f"  [srgb] applied effect '{name}'")
    except Exception as e:
        log(f"  [srgb] SignalRGB not reachable or API mismatch: {e}")
