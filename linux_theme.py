"""Stage 4 (Linux): apply the theme on KDE Plasma 6 (Wayland) — Bazzite's
desktop. Implements the same interface as theme.py (Windows):

  enumerate_monitors / assign_roles / compose_wallpaper
  write_theme_file / apply_theme / write_registry_colors
  snapshot_windows_look / restore_windows_look   (same names, KDE meanings)

Mechanisms:
  - monitors: `kscreen-doctor -j` (JSON). Linux exposes connector names
    (HDMI-A-1, eDP-1, ...) rather than monitor model names, so config
    `monitors[].match` substrings target those.
  - wallpaper: KDE has no span mode; instead each screen gets its own
    exact-sized tile, pushed per-containment via plasmashell's
    evaluateScript D-Bus call (plasma-apply-wallpaperimage can't do
    per-screen). FillMode=0 (stretch) with exact-sized tiles = pixel perfect.
  - colors: `plasma-apply-colorscheme BreezeDark|BreezeLight --accent-color
    '#rrggbb'`. KDE has ONE global color scheme (apps + shell together), so
    system_mode wins; app_mode is a fallback when system_mode is 'match'.
  - theme file: a JSON manifest (theme.json) listing role -> tile path;
    apply_theme re-derives screen->role mapping and pushes the images.

Session environment: kscreen-doctor/qdbus need the user's Wayland/D-Bus
session, which systemd user services and SSH shells may not fully have —
_session_env() fills in the standard paths for the current uid.
"""

import glob
import json
import os
import re
import shutil
import subprocess
from datetime import datetime

from wallpaper import (  # noqa: F401  (re-exported for main.py parity)
    assign_roles, compose_monitor_tile, _ini_safe,
)

THEME_FILENAME = "theme.json"

_HEX6 = re.compile(r"#[0-9a-fA-F]{6}")


# --------------------------------------------------------------- session env

def _session_env():
    """os.environ + the variables needed to talk to the user's graphical
    session (Wayland display + D-Bus session bus), auto-detected for the
    current uid when missing (SSH shells, systemd units)."""
    env = dict(os.environ)
    uid = os.getuid()
    runtime = env.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}"
    env["XDG_RUNTIME_DIR"] = runtime
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime}/bus")
    if not env.get("WAYLAND_DISPLAY"):
        sockets = sorted(s for s in glob.glob(os.path.join(runtime, "wayland-*"))
                         if not s.endswith(".lock"))
        if sockets:
            env["WAYLAND_DISPLAY"] = os.path.basename(sockets[0])
    # KDE tools shell out to xrdb for XWayland clients; a missing DISPLAY is
    # harmless but noisy. Wayland sessions with XWayland almost always use :0.
    env.setdefault("DISPLAY", ":0")
    return env


def _run(args, timeout=30):
    return subprocess.run(args, env=_session_env(), capture_output=True,
                          text=True, timeout=timeout)


# ------------------------------------------------------------------ monitors

def parse_kscreen_outputs(data):
    """kscreen-doctor -j JSON -> our monitor shape (enabled outputs only).

    rect is in LOGICAL pixels (matching the desktop layout); 'scale' is kept
    separately so wallpaper tiles can be rendered at physical resolution.
    """
    monitors = []
    for o in data.get("outputs", []):
        if not (o.get("connected") and o.get("enabled")):
            continue
        x, y = int(o["pos"]["x"]), int(o["pos"]["y"])
        w, h = int(o["size"]["width"]), int(o["size"]["height"])
        name = str(o.get("name") or "")
        monitors.append({
            "device": name,
            "desc": name,  # kscreen exposes connector names, not model names
            "rect": (x, y, x + w, y + h),
            "primary": o.get("priority") == 1,
            "scale": float(o.get("scale") or 1.0),
        })
    return monitors


def enumerate_monitors():
    r = _run(["kscreen-doctor", "-j"], timeout=20)
    return parse_kscreen_outputs(json.loads(r.stdout))


# ------------------------------------------------------------- compositing

def compose_wallpaper(monitors, art, out_path, log=print):
    """KDE strategy: one exact-sized tile per screen (no span mode exists).

    Tiles are rendered at PHYSICAL resolution (logical size x scale factor)
    and named wallpaper_<role>_<stamp>.jpg so write_theme_file can map them
    back to roles. Returns the hero tile path (the 'primary' wallpaper).

    Hero slideshow mode (art['_rotate'] with >1 candidates): additionally
    renders one tile per candidate per role into slides/<role>/NN.jpg and
    writes slideshow.json — write_theme_file merges it into the manifest and
    apply_theme switches those screens to the org.kde.slideshow plugin.
    """
    import wallpaper as wall

    cache_dir = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(cache_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    hero = wall.first_art_path(art)
    paths, role_mon = {}, {}
    for m in monitors:
        scale = m.get("scale", 1.0)
        w = round((m["rect"][2] - m["rect"][0]) * scale)
        h = round((m["rect"][3] - m["rect"][1]) * scale)
        tile = compose_monitor_tile(m, art, hero, w, h, log)
        p = os.path.join(cache_dir, f"wallpaper_{m['role']}_{stamp}.jpg")
        tile.save(p, "JPEG", quality=92)
        paths[m["role"]] = p
        role_mon[m["role"]] = (m, w, h)  # last monitor of a role wins
        log(f"  [wall] {m['role']} tile {w}x{h} -> {p}")
    wall._prune_old_composites(cache_dir, paths.values())

    ss_path = os.path.join(cache_dir, "slideshow.json")
    slides_root = os.path.join(cache_dir, "slides")
    rotate = art.get("_rotate")
    candidates = [c for c in (rotate.get("candidates") if isinstance(rotate, dict) else [])
                  if isinstance(c, str) and os.path.exists(c)]
    if len(candidates) > 1:
        folders = {}
        for role, (m, w, h) in role_mon.items():
            folder = os.path.join(slides_root, role)
            os.makedirs(folder, exist_ok=True)
            for old in glob.glob(os.path.join(folder, "*.jpg")):
                try:
                    os.remove(old)
                except OSError:
                    pass
            folders[role] = folder
            for i, cand in enumerate(candidates):
                vart = dict(art)
                vart["hero"] = cand
                tile = compose_monitor_tile(m, vart, cand, w, h, log)
                tile.save(os.path.join(folder, f"{i:02d}.jpg"),
                          "JPEG", quality=92)
            log(f"  [wall] {role}: {len(candidates)} slideshow tiles -> {folder}")
        try:
            interval_s = max(60, int(rotate.get("interval_minutes", 30)) * 60)
        except (TypeError, ValueError):
            interval_s = 1800
        with open(ss_path, "w", encoding="utf-8") as f:
            json.dump({"folders": folders, "interval_s": interval_s,
                       "shuffle": bool(rotate.get("shuffle"))}, f, indent=2)
    else:
        # not rotating: clear any previous slideshow state for this game
        try:
            os.remove(ss_path)
        except OSError:
            pass
        if os.path.isdir(slides_root):
            shutil.rmtree(slides_root, ignore_errors=True)
    return paths.get("hero") or next(iter(paths.values()))


# ---------------------------------------------------- theme manifest + apply

def write_theme_file(path, display_name, wallpaper_path, palette,
                     variants=None, interval_minutes=30, shuffle=False):
    """JSON manifest instead of a Windows .theme INI: role -> tile path.

    variants/interval_minutes/shuffle are the Windows .theme slideshow knobs
    and are ignored here — on KDE the slideshow spec travels via the
    slideshow.json that compose_wallpaper wrote next to the tiles (it is
    merged into the manifest so gallery re-applies keep rotating).
    """
    cache_dir = os.path.dirname(os.path.abspath(wallpaper_path))
    walls = {}
    for role in ("hero", "logo", "icon"):
        hits = sorted(glob.glob(os.path.join(cache_dir,
                                             f"wallpaper_{role}_*.jpg")))
        if hits:
            walls[role] = hits[-1]
    manifest = {"name": _ini_safe(display_name), "wallpapers": walls,
                "primary": os.path.abspath(wallpaper_path),
                "accent": palette.get("accent"),
                "appearance": palette.get("appearance")}
    try:
        with open(os.path.join(cache_dir, "slideshow.json"),
                  encoding="utf-8") as f:
            ss = json.load(f)
        if isinstance(ss.get("folders"), dict) and ss["folders"]:
            manifest["slideshow"] = ss
    except (OSError, ValueError):
        pass
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    return path


def _wallpaper_script(screen_wall):
    """plasmashell JS: set each screen containment's wallpaper image.

    desktops()[i].screen is the KWin screen index; disabled screens report -1
    and are skipped by the mapping lookup. Paths go through json.dumps so a
    hostile path can't break out of the JS string literal."""
    mapping = {str(i): "file://" + p for i, p in screen_wall.items()}
    return (
        "var walls = " + json.dumps(mapping) + ";"
        "var ds = desktops();"
        "for (var i = 0; i < ds.length; i++) {"
        "  var d = ds[i]; var p = walls[String(d.screen)];"
        "  if (!p) continue;"
        "  d.wallpaperPlugin = 'org.kde.image';"
        "  d.currentConfigGroup = ['Wallpaper', 'org.kde.image', 'General'];"
        "  d.writeConfig('Image', p);"
        "  d.writeConfig('FillMode', 0);"
        "}"
    )


def _slideshow_script(screen_folder, interval_s, shuffle):
    """plasmashell JS: set each screen containment to a folder slideshow
    (org.kde.slideshow). Same injection discipline as _wallpaper_script:
    paths go through json.dumps. SlidePaths takes a JS array of file://
    folder URLs; SlideInterval is seconds."""
    mapping = {str(i): "file://" + p for i, p in screen_folder.items()}
    return (
        "var folders = " + json.dumps(mapping) + ";"
        "var ds = desktops();"
        "for (var i = 0; i < ds.length; i++) {"
        "  var d = ds[i]; var p = folders[String(d.screen)];"
        "  if (!p) continue;"
        "  d.wallpaperPlugin = 'org.kde.slideshow';"
        "  d.currentConfigGroup = ['Wallpaper', 'org.kde.slideshow', 'General'];"
        "  d.writeConfig('SlidePaths', [p]);"
        "  d.writeConfig('SlideInterval', " + str(int(interval_s)) + ");"
        "  d.writeConfig('Shuffle', " + ("true" if shuffle else "false") + ");"
        "  d.writeConfig('FillMode', 0);"
        "}"
    )


def apply_theme(theme_path, log=print, cfg=None):
    """Push the manifest's per-role tiles to their screens via plasmashell.
    Roles with a slides/ folder (hero slideshow mode) get the
    org.kde.slideshow plugin; the rest get a static org.kde.image tile."""
    with open(theme_path, encoding="utf-8") as f:
        manifest = json.load(f)
    walls = manifest.get("wallpapers") or {}
    ss = manifest.get("slideshow") or {}
    folders = ss.get("folders") or {}
    monitors = enumerate_monitors()
    assign_roles(monitors, (cfg or {}).get("monitors", []), log)
    # enumerate order == kscreen output order == plasma screen indices
    screen_wall, screen_slide = {}, {}
    for i, m in enumerate(monitors):
        folder = folders.get(m["role"])
        if folder and os.path.isdir(folder):
            screen_slide[i] = folder
            continue
        p = walls.get(m["role"]) or manifest.get("primary")
        if p and os.path.exists(p):
            screen_wall[i] = p
    if not screen_wall and not screen_slide:
        log("  [kde] no wallpaper tiles found; manifest stale?")
        return
    if screen_wall:
        _run(["qdbus", "org.kde.plasmashell", "/PlasmaShell",
              "org.kde.PlasmaShell.evaluateScript",
              _wallpaper_script(screen_wall)], timeout=30)
    if screen_slide:
        _run(["qdbus", "org.kde.plasmashell", "/PlasmaShell",
              "org.kde.PlasmaShell.evaluateScript",
              _slideshow_script(screen_slide, ss.get("interval_s", 1800),
                                ss.get("shuffle"))], timeout=30)
    log(f"  [kde] wallpapers applied to "
        f"{len(screen_wall) + len(screen_slide)} screen(s)"
        + (f" ({len(screen_slide)} slideshow)" if screen_slide else ""))


# --------------------------------------------------------------- KDE colors

def write_registry_colors(palette, log=print, system_mode="match",
                          app_mode="match"):
    """KDE equivalent of the Windows registry color writes: one global color
    scheme + accent. KDE has no separate app/system split — system_mode wins,
    falling back to app_mode, then the palette's own appearance."""
    mode = system_mode if system_mode in ("dark", "light") else app_mode
    appearance = mode if mode in ("dark", "light") else palette["appearance"]
    scheme = "BreezeDark" if appearance == "dark" else "BreezeLight"
    # NOTE: scheme and accent must be applied by SEPARATE invocations —
    # passing both to one plasma-apply-colorscheme call silently applies
    # only the accent (observed on Plasma 6.7).
    r = _run(["plasma-apply-colorscheme", scheme], timeout=30)
    if r.returncode != 0:
        log(f"  [kde] plasma-apply-colorscheme {scheme} failed: "
            f"{r.stderr.strip()}")
        return
    accent = str(palette.get("accent") or "")
    accent_ok = False
    if _HEX6.fullmatch(accent):
        r = _run(["plasma-apply-colorscheme", "--accent-color", accent],
                 timeout=30)
        accent_ok = r.returncode == 0
        if not accent_ok:
            log(f"  [kde] accent apply failed: {r.stderr.strip()}")
    log(f"  [kde] colorscheme {scheme}"
        + (f", accent {accent}" if accent_ok else "")
        + " (KDE uses one global scheme for apps + shell)")


# ------------------------------------------------- original-look snapshot

_BACKUP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "desktop_backup.json")

_READ_WALLS_SCRIPT = (
    "var ds = desktops();"
    "for (var i = 0; i < ds.length; i++) {"
    "  var d = ds[i];"
    "  d.currentConfigGroup = ['Wallpaper', 'org.kde.image', 'General'];"
    "  print(d.screen + '|' + d.wallpaperPlugin + '|' + d.readConfig('Image'));"
    "}"
)


def snapshot_windows_look(log=print):
    """Save the current KDE scheme/accent/wallpapers once (never overwrites),
    so restore_windows_look() can undo the app later."""
    if os.path.exists(_BACKUP_PATH):
        return
    try:
        scheme = _run(["kreadconfig6", "--group", "General",
                       "--key", "ColorScheme"]).stdout.strip()
        accent = _run(["kreadconfig6", "--group", "General",
                       "--key", "AccentColor"]).stdout.strip()
        r = _run(["qdbus", "org.kde.plasmashell", "/PlasmaShell",
                  "org.kde.PlasmaShell.evaluateScript", _READ_WALLS_SCRIPT])
        screens = {}
        for line in r.stdout.splitlines():
            parts = line.strip().split("|", 2)
            if len(parts) == 3 and parts[0].lstrip("-").isdigit():
                screens[parts[0]] = {"plugin": parts[1], "image": parts[2]}
        with open(_BACKUP_PATH, "w", encoding="utf-8") as f:
            json.dump({"color_scheme": scheme or "BreezeLight",
                       "accent_color": accent, "screens": screens}, f, indent=2)
        log("  [backup] snapshot of the previous desktop look saved "
            "(desktop_backup.json)")
    except Exception as e:
        log(f"  [backup] snapshot failed (restore unavailable): {e}")


def restore_windows_look(log=print):
    """Write the snapshot back: color scheme/accent + per-screen wallpapers."""
    with open(_BACKUP_PATH, encoding="utf-8") as f:
        snap = json.load(f)
    scheme = snap.get("color_scheme") or "BreezeLight"
    _run(["plasma-apply-colorscheme", scheme], timeout=30)
    accent = str(snap.get("accent_color") or "")
    if _HEX6.fullmatch(accent):  # separate call: combined scheme+accent skips the scheme
        _run(["plasma-apply-colorscheme", "--accent-color", accent], timeout=30)
    screen_wall = {int(k): v["image"].replace("file://", "", 1)
                   for k, v in (snap.get("screens") or {}).items()
                   if v.get("plugin") == "org.kde.image" and v.get("image")}
    if screen_wall:
        _run(["qdbus", "org.kde.plasmashell", "/PlasmaShell",
              "org.kde.PlasmaShell.evaluateScript",
              _wallpaper_script(screen_wall)], timeout=30)
    log("  [restore] previous desktop look restored")
