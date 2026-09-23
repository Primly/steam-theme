"""Extra app theming: terminal color scheme + RGB lighting.

Platform dispatch:
  apply_terminal -> Windows Terminal (Windows) / Konsole (Linux)
  apply_rgb      -> SignalRGB (Windows) / OpenRGB SDK (Linux)

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
import sys


def apply_terminal(cfg, palette, theme_name, wallpaper_path, log=print):
    """Theme the platform's terminal with the palette."""
    if sys.platform == "win32":
        return apply_windows_terminal(cfg, palette, theme_name,
                                      wallpaper_path, log)
    return apply_konsole(cfg, palette, theme_name, log)


def apply_rgb(cfg, palette, log=print):
    """Sync RGB lighting with the palette."""
    if sys.platform == "win32":
        return apply_signalrgb(cfg, palette, log)
    import openrgb_sync
    return openrgb_sync.apply_openrgb(cfg, palette, log)

SRGB_EFFECT_TITLE = "Steam Theme"
SRGB_LEGACY_TITLES = ("Steam Wallpaper",)  # pre-rename; cleaned up on write

_WIN_FORBIDDEN = '<>:"/\\|?*'


def _safe_title_name(name):
    """Filesystem-safe effect-name fragment from a game/theme name."""
    s = "".join("_" if c in _WIN_FORBIDDEN else c for c in str(name))
    return s.strip().strip(".").strip()[:60] or "Game"


def effect_title(cfg, palette):
    """Effect title for this palette. In per_game scope every game gets its
    own 'Steam Theme - <Game>' effect (individually tweakable in SignalRGB);
    in single scope everything shares the one 'Steam Theme' effect."""
    scope = cfg.get("signalrgb", {}).get("effect_scope", "per_game")
    if scope == "per_game":
        game = palette.get("game_name") or palette.get("theme_name")
        if game:
            return f"{SRGB_EFFECT_TITLE} - {_safe_title_name(game)}"
    return SRGB_EFFECT_TITLE

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
    import winreg  # lazy: extras.py must import cleanly on Linux
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion"
                             r"\Explorer\User Shell Folders")
        raw, _ = winreg.QueryValueEx(key, "Personal")
        return os.path.expandvars(raw)
    except OSError:
        return os.path.join(os.path.expanduser("~"), "Documents")


def write_signalrgb_effect(cfg, palette, log=print):
    """Write the palette-skinned effect file(s) and return the primary title.

    Per-game scope writes 'Steam Theme - <Game>.html' (yours to customize in
    SignalRGB per game) AND refreshes the shared 'Steam Theme.html' mirror:
    SignalRGB only discovers new effect files at launch, so the already-known
    mirror is what gets applied until the per-game file is discovered.
    """
    import srgb_effects
    srgb = cfg.get("signalrgb", {})
    style = srgb.get("effect_style", "gradient")
    title = effect_title(cfg, palette)
    effects_dir = (srgb.get("effects_dir")
                   or os.path.join(_documents_dir(), "WhirlwindFX", "Effects"))
    try:
        os.makedirs(effects_dir, exist_ok=True)
        for legacy in SRGB_LEGACY_TITLES:  # remove stale pre-rename effects
            old = os.path.join(effects_dir, f"{legacy}.html")
            if os.path.exists(old):
                os.remove(old)
                log(f"  [srgb] removed legacy effect {old}")
        path = os.path.join(effects_dir, f"{title}.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(srgb_effects.render_effect(style, title, palette))
        log(f"  [srgb] wrote custom effect -> {path}")
        if title != SRGB_EFFECT_TITLE:
            # shared mirror, refreshed every game — guaranteed discovered
            mirror = os.path.join(effects_dir, f"{SRGB_EFFECT_TITLE}.html")
            with open(mirror, "w", encoding="utf-8") as f:
                f.write(srgb_effects.render_effect(style, SRGB_EFFECT_TITLE,
                                                   palette))
            log(f"  [srgb] refreshed shared mirror -> {mirror}")
        return title
    except OSError as e:
        log(f"  [srgb] could not write custom effect: {e}")
        return None


# ------------------------------------------------------------------ Konsole

_KONSOLE_DIR = os.path.expanduser("~/.local/share/konsole")
_KONSOLE_PROFILE = "Steam Theme.profile"


def _hex_to_rgb_csv(hexcolor):
    """'#66C0F4' -> '102,192,244'. Raises ValueError on anything else —
    palette data flows into an INI-like file here, so validate hard."""
    import re as _re
    if not _re.fullmatch(r"#[0-9a-fA-F]{6}", str(hexcolor)):
        raise ValueError(f"not a #RRGGBB color: {hexcolor!r}")
    h = str(hexcolor).lstrip("#")
    return f"{int(h[0:2], 16)},{int(h[2:4], 16)},{int(h[4:6], 16)}"


def render_konsole_colorscheme(name, palette):
    """Konsole .colorscheme file content built from the theme palette.
    Slot mapping mirrors the Windows Terminal scheme: Color0-7 from the
    palette's first 8 colors, Intense variants from the next 8."""
    from wallpaper import _ini_safe
    colors = (list(palette.get("colors")) + list(palette.get("colors")))[:16]
    bg = _hex_to_rgb_csv(palette["background"])
    fg = "232,232,232" if palette.get("appearance") == "dark" else "26,26,26"
    lines = ["[Background]", f"Color={bg}", "",
             "[Foreground]", f"Color={fg}", ""]
    for i in range(8):
        lines += [f"[Color{i}]", f"Color={_hex_to_rgb_csv(colors[i])}", ""]
    for i in range(8):
        lines += [f"[Color{i}Intense]",
                  f"Color={_hex_to_rgb_csv(colors[i + 8])}", ""]
    lines += ["[General]", f"Description={_ini_safe(name)}",
              "Opacity=1", "Wallpaper=", ""]
    return "\n".join(lines)


def apply_konsole(cfg, palette, theme_name, log=print):
    """Write a palette colorscheme + a 'Steam Theme' profile pointing at it,
    and (optionally) make that profile Konsole's default. Konsole reloads
    colorscheme files live; new windows pick up the default profile."""
    k = cfg.get("konsole", {})
    if not k.get("enabled"):
        return
    scheme = _safe_title_name(theme_name)
    try:
        content = render_konsole_colorscheme(scheme, palette)
    except (ValueError, KeyError) as e:
        log(f"  [konsole] unusable palette colors: {e}")
        return
    try:
        os.makedirs(_KONSOLE_DIR, exist_ok=True)
        with open(os.path.join(_KONSOLE_DIR, f"{scheme}.colorscheme"),
                  "w", encoding="utf-8") as f:
            f.write(content)
        with open(os.path.join(_KONSOLE_DIR, _KONSOLE_PROFILE),
                  "w", encoding="utf-8") as f:
            f.write("[Appearance]\nColorScheme=" + scheme + "\n\n"
                    "[General]\nName=Steam Theme\nParent=FALLBACK/\n")
    except OSError as e:
        log(f"  [konsole] could not write colorscheme/profile: {e}")
        return
    log(f"  [konsole] colorscheme '{scheme}' applied to the Steam Theme profile")
    if k.get("set_default_profile", True):
        import subprocess
        try:
            subprocess.run(["kwriteconfig6", "--file", "konsolerc",
                            "--group", "Desktop Entry",
                            "--key", "DefaultProfile", _KONSOLE_PROFILE],
                           capture_output=True, timeout=15)
        except (OSError, subprocess.SubprocessError) as e:
            log(f"  [konsole] could not set default profile: {e}")


def apply_signalrgb(cfg, palette, log=print):
    """Write the palette-skinned custom effect and apply it over SignalRGB's
    local REST API. Falls back to the named stock effect when the custom one
    isn't registered yet, or when custom_effect is disabled. Endpoint layout
    can vary by SignalRGB version — adjust here if your install differs."""
    srgb = cfg.get("signalrgb", {})
    if not srgb.get("enabled"):
        return
    import time

    import requests
    base = srgb.get("base_url", "http://localhost:16038").rstrip("/")
    # fallback_effect replaced the old mood->effect map; honor its "default"
    # entry if an old config still has one
    fallback = (srgb.get("fallback_effect")
                or srgb.get("effect_map", {}).get("default")
                or "Solid Color")
    # palette-driven custom effect takes priority over the named fallback
    custom_title = None
    if srgb.get("custom_effect", True):
        custom_title = write_signalrgb_effect(cfg, palette, log)

    # SignalRGB can still be starting up when the pipeline fires (logon race);
    # retry with backoff before giving up
    attempts = int(srgb.get("retry_attempts", 3))
    for attempt in range(1, attempts + 1):
        try:
            r = requests.get(f"{base}/api/v1/lighting/effects", timeout=5)
            r.raise_for_status()
            items = r.json().get("data", {}).get("items", [])
            effects = [(e.get("attributes", {}).get("name", ""),
                        e.get("links", {}).get("apply")) for e in items]

            def find(name):
                # exact match first, then substring ('Steam Theme' must not
                # accidentally match 'Steam Theme - Some Game')
                exact = [(n, link) for n, link in effects
                         if link and n.strip().lower() == name.lower()]
                if exact:
                    return exact[0]
                return next(((n, link) for n, link in effects
                             if link and name.lower() in n.lower()), None)

            # priority: per-game effect -> shared mirror -> named fallback.
            # New files are only discovered by SignalRGB at launch, so the
            # mirror (rewritten every game, discovered long ago) bridges the
            # gap until the per-game file is registered.
            match = find(custom_title) if custom_title else None
            if custom_title and not match:
                log(f"  [srgb] '{custom_title}' not registered yet — restart "
                    f"SignalRGB once to discover it; using the shared mirror "
                    f"for now")
                match = find(SRGB_EFFECT_TITLE)
            if not match:
                match = find(fallback)
            if not match:
                log(f"  [srgb] fallback effect {fallback!r} not installed")
                log(f"  [srgb] no usable effect; available: {[n for n, _ in effects][:8]}")
                return
            name, apply_link = match
            ra = requests.post(base + apply_link, timeout=5)
            ra.raise_for_status()
            log(f"  [srgb] applied effect '{name}'")
            return
        except Exception as e:
            if attempt < attempts:
                log(f"  [srgb] attempt {attempt}/{attempts} failed ({e}); retrying")
                time.sleep(2 * attempt)
            else:
                log(f"  [srgb] SignalRGB not reachable or API mismatch: {e}")
