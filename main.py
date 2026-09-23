"""Steam Theme service.

Polls Steam for the last-played game; when it changes, runs the pipeline:
  detect -> fetch art -> palette -> compose per-monitor wallpaper -> theme
  -> terminal / RGB extras. Runs on Windows (registry + .theme) and Linux
  (KDE Plasma 6 — Bazzite).

Usage:
  python main.py                run the polling service (forever)
  python main.py --once         check once, apply if the game changed
  python main.py --force        apply even if the game hasn't changed
  python main.py --appid 440    theme a specific game
  python main.py --reapply      re-apply the current cached theme (unlock trigger)
  python main.py --dry-run      full pipeline but no theme/registry/extras writes
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import artwork
import extras
import palette as palette_mod
import steamdetect
import upscale

if sys.platform == "win32":
    import theme as theme_mod
else:
    import linux_theme as theme_mod

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
__version__ = "1.2.0"


def safe_join(base, *parts):
    """Join parts under base; refuse anything that escapes it (path traversal)."""
    base = os.path.abspath(base)
    p = os.path.abspath(os.path.join(base, *parts))
    try:
        inside = os.path.commonpath([base, p]) == base
    except ValueError:  # different drive
        inside = False
    if not inside:
        raise ValueError(f"refusing path outside the app directory: {p}")
    return p


def cfg_path(cfg, key, default):
    """Resolve a config path setting (cache_dir/state_file/log_file) inside
    BASE_DIR, falling back to the default if a (hostile) value would escape."""
    try:
        return safe_join(BASE_DIR, cfg.get(key) or default)
    except ValueError:
        return os.path.join(BASE_DIR, default)


def load_config():
    """Load config.json, falling back to config.example.json on a fresh
    checkout so the config UI works before the first save. Placeholder values
    ('YOUR_...') are blanked so no API gets called with a junk key."""
    path = os.path.join(BASE_DIR, "config.json")
    if not os.path.exists(path):
        path = os.path.join(BASE_DIR, "config.example.json")
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    for k, v in list(cfg.items()):
        if isinstance(v, str) and v.startswith("YOUR_"):
            cfg[k] = ""
    return cfg


def _log_factory(cfg):
    log_path = cfg_path(cfg, "log_file", "service.log")

    def log(msg):
        line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
        print(line, flush=True)
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
    return log


def _state_path(cfg):
    return cfg_path(cfg, "state_file", "state.json")


def load_state(cfg):
    try:
        with open(_state_path(cfg), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(cfg, state):
    with open(_state_path(cfg), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def run_pipeline(cfg, game, log, dry_run=False):
    appid, name = game.get("appid"), game["name"]
    # custom (non-Steam) games may have no appid; cache under a stable key
    cache_key = game.get("cache_key") or str(appid)
    cache_root = cfg_path(cfg, "cache_dir", "cache")
    cache = safe_join(cache_root, cache_key)
    log(f"pipeline: {name} ({appid or cache_key})")

    # Stage 2 — art (cached per game)
    art = artwork.fetch_artwork(appid, name, game.get("icon_url"),
                                {**cfg, "cache_dir": cache_root},
                                log, cache_key=cache_key)
    if not art:
        log("  !! no artwork found at all; aborting")
        return None

    # monitors first, so art can be upscaled toward its target display
    monitors = theme_mod.assign_roles(theme_mod.enumerate_monitors(),
                                      cfg.get("monitors", []), log)

    # Stage 3 — palette (+ optional VLM naming)
    hero = art.get("hero") or next(iter(art.values()))
    pref = cfg.get("appearance_preference")
    if pref not in ("dark", "light", "auto"):  # back-compat with force_dark_mode
        pref = "dark" if cfg.get("force_dark_mode") else "auto"
    force = None if pref == "auto" else pref
    pal = palette_mod.build_palette(hero, cfg.get("palette_mode", "colorful"),
                                    force=force)
    ai = palette_mod.ai_theme_naming(cfg, hero, pal, name, log)
    if ai:
        log(f"  [vlm] {ai}")
        if ai.get("appearance") in ("dark", "light") and pref == "auto":
            pal["appearance"] = ai["appearance"]
        if ai.get("palette_mode") in ("material", "muted", "colorful"):
            pal = palette_mod.build_palette(hero, ai["palette_mode"], force=force)
            if pref == "auto":
                pal["appearance"] = ai.get("appearance", pal["appearance"])
    theme_name = (ai or {}).get("theme_name") or f"{name} — Steam Theme"
    mood = (ai or {}).get("mood", "")

    # upscale after VLM naming so the mood can steer generative Topaz models
    up_ctx = {"game": name, "mood": mood}
    for m in monitors:
        role = m["role"]
        if role in art:
            w, h = m["rect"][2] - m["rect"][0], m["rect"][3] - m["rect"][1]
            if role in ("logo", "icon"):  # centered roles only need the fit box
                w, h = round(w * 0.8), round(h * 0.7)
            art[role] = upscale.maybe_upscale(art[role], w, h, cfg, log,
                                              context=up_ctx, role=role)
    pal["theme_name"] = theme_name
    pal["mood"] = mood
    pal["game_name"] = name  # per-game SignalRGB effect titles, gallery, etc.
    log(f"  [pal] {pal['appearance']} mode, accent {pal['accent']} "
        f"({pal['accent_grade']}, {pal['accent_contrast']}:1), theme '{theme_name}'")

    # Stage 4 — composite + theme
    # Unique filename per run: Windows keys its TranscodedWallpaper cache by
    # path, so reusing the same filename would silently keep the OLD image.
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    wallpaper = theme_mod.compose_wallpaper(monitors, art,
                                            os.path.join(cache, f"wallpaper_{stamp}.jpg"), log)
    theme_path = theme_mod.write_theme_file(
        os.path.join(cache, theme_mod.THEME_FILENAME), theme_name, wallpaper, pal)

    with open(os.path.join(cache, "palette.json"), "w", encoding="utf-8") as f:
        json.dump({**pal, "wallpaper": os.path.abspath(wallpaper)}, f, indent=2)

    if dry_run:
        log("  [dry-run] skipping registry writes, theme apply, and extras")
        return {"theme_path": theme_path, "palette": pal, "wallpaper": wallpaper}

    # snapshot the pre-app Windows look once so 'Restore' can undo us later
    theme_mod.snapshot_windows_look(log)
    sys_mode = cfg.get("system_mode", "match")
    app_mode = cfg.get("app_mode", "match")
    theme_mod.write_registry_colors(pal, log, sys_mode, app_mode)
    theme_mod.apply_theme(theme_path, log, cfg=cfg)
    # the theme engine applies its own mode defaults on launch — re-assert
    # ours after it finishes so app/system modes land as configured
    time.sleep(3)
    theme_mod.write_registry_colors(pal, log, sys_mode, app_mode)
    extras.apply_terminal(cfg, pal, theme_name, art.get("hero"), log)
    extras.apply_rgb(cfg, pal, log)
    return {"theme_path": theme_path, "palette": pal, "wallpaper": wallpaper}


def reapply(cfg, log):
    state = load_state(cfg)
    theme_path = state.get("theme_path")
    if not (theme_path and os.path.exists(theme_path)):
        log("nothing to re-apply")
        return
    log("re-applying cached theme after unlock")
    # palette.json sits next to the cached .theme; needed to re-assert the
    # registry colors/modes that the theme engine clobbers on launch
    pal = None
    try:
        with open(os.path.join(os.path.dirname(theme_path), "palette.json"),
                  encoding="utf-8") as f:
            pal = json.load(f)
    except (OSError, json.JSONDecodeError):
        pass
    sys_mode = cfg.get("system_mode", "match")
    app_mode = cfg.get("app_mode", "match")
    if pal:
        theme_mod.write_registry_colors(pal, log, sys_mode, app_mode)
    theme_mod.apply_theme(theme_path, log, cfg=cfg)
    if pal:
        time.sleep(3)
        theme_mod.write_registry_colors(pal, log, sys_mode, app_mode)


def reapply_from_cache(cfg, log, cache_key):
    """Re-apply a theme straight from its cache folder (theme gallery)."""
    cache = safe_join(cfg_path(cfg, "cache_dir", "cache"), cache_key)
    theme_path = os.path.join(cache, theme_mod.THEME_FILENAME)
    if not os.path.exists(theme_path):
        raise FileNotFoundError(f"no cached theme for {cache_key!r}")
    with open(os.path.join(cache, "palette.json"), encoding="utf-8") as f:
        pal = json.load(f)
    log(f"gallery: re-applying '{pal.get('theme_name') or cache_key}'")
    sys_mode = cfg.get("system_mode", "match")
    app_mode = cfg.get("app_mode", "match")
    theme_mod.write_registry_colors(pal, log, sys_mode, app_mode)
    theme_mod.apply_theme(theme_path, log, cfg=cfg)
    time.sleep(3)
    theme_mod.write_registry_colors(pal, log, sys_mode, app_mode)
    extras.apply_terminal(cfg, pal, pal.get("theme_name") or cache_key,
                          pal.get("wallpaper"), log)
    extras.apply_rgb(cfg, pal, log)
    # point state at this theme and set a manual hold: the poller keeps
    # this theme until the user actually plays a (different) game, instead of
    # reverting to the last-played game on the next poll
    state = load_state(cfg)
    state.update({"theme_path": theme_path, "last_cache_key": cache_key,
                  "last_identity": cache_key,
                  "last_name": pal.get("theme_name") or cache_key,
                  "manual_hold": {
                      "identity": cache_key,
                      "name": (pal.get("game_name") or pal.get("theme_name")
                               or cache_key),
                      "held_at": time.time()}})
    try:
        state["last_appid"] = int(cache_key)
    except ValueError:
        pass
    save_state(cfg, state)


def check_once(cfg, log, force=False, appid_override=None, dry_run=False):
    """Detection priority: custom (non-Steam) process > Steam now-playing >
    Steam last-played. Theming is keyed per game ('identity'), so replaying
    the same game never triggers a redundant re-theme."""
    steam_id = cfg.get("steam_id64") or steamdetect.get_steam_id64()
    excludes = set(cfg.get("exclude_appids", []))
    state = load_state(cfg)

    if appid_override:
        game = {"appid": appid_override, "name": f"app {appid_override}",
                "rtime_last_played": int(time.time()), "icon_url": None}
        # resolve the real name if we can
        try:
            for g in steamdetect.get_owned_games(cfg["steam_api_key"], steam_id):
                if g["appid"] == appid_override:
                    game = g
                    break
        except Exception:
            pass
        source = "cli"
    else:
        try:
            games = steamdetect.get_owned_games(cfg["steam_api_key"], steam_id)
        except Exception as e:
            log(f"steam api error: {e}")
            return
        played = [g for g in games if g.get("rtime_last_played")]
        game = max(played, key=lambda x: x["rtime_last_played"]) if played else None
        source = "last-played"

        # priority 1: a running custom (non-Steam) game
        custom = steamdetect.find_running_custom_game(cfg.get("custom_games"))
        if custom:
            # appid must be a positive int; a hand-edited config value that
            # isn't is ignored rather than ever reaching a path
            try:
                c_appid = int(custom.get("appid") or 0) or None
            except (TypeError, ValueError):
                c_appid = None
            c_name = (custom.get("name") or "").strip() or custom["process"]
            if c_appid in excludes:
                log(f"  custom game {c_name} is in the exclude list; skipping")
            else:
                by_id = {g["appid"]: g for g in games}
                game = dict(by_id.get(c_appid) or {
                    "appid": c_appid, "name": c_name,
                    "rtime_last_played": 0, "icon_url": None})
                game["name"] = (custom.get("name") or "").strip() or game["name"]
                game["cache_key"] = (str(c_appid) if c_appid else
                                     "custom_" + re.sub(r"[^a-z0-9]+", "-",
                                                         c_name.lower()).strip("-"))
                source = "custom-process"
        # priority 2: a Steam game running right now
        elif cfg.get("now_playing", True):
            running = steamdetect.get_running_appid()
            if running and running not in excludes:
                by_id = {g["appid"]: g for g in games}
                game = by_id.get(running) or {
                    "appid": running, "name": f"app {running}",
                    "rtime_last_played": 0, "icon_url": None}
                source = "now-playing"

    if not game:
        log("no played games returned by the Steam API")
        return

    appid = game.get("appid")
    cache_key = game.get("cache_key") or str(appid)
    identity = cache_key  # one theme per game regardless of play timestamps

    # A theme manually applied from the web UI gallery sets a "hold" so the
    # poller doesn't immediately revert it to the last-played game. The hold
    # releases as soon as the user actually plays something: a running game
    # is detected, or Steam reports a last-played time newer than the hold.
    hold = state.get("manual_hold")
    held = str(hold.get("identity") or "") if isinstance(hold, dict) else ""
    if held and not appid_override:
        if identity == held:
            # the held game is what's detected now — the hold did its job
            state.pop("manual_hold", None)
            save_state(cfg, state)
        elif force:
            # a forced re-run (UI redo buttons) targets the theme the user is
            # looking at, not whatever Steam last-played happens to report
            try:
                held_appid = int(held)
            except ValueError:  # custom_<slug> key
                held_appid = None
            held_game = (next((g for g in games if g["appid"] == held_appid), None)
                         if held_appid is not None else None)
            game = dict(held_game or {"appid": held_appid,
                                      "name": hold.get("name") or held,
                                      "rtime_last_played": 0,
                                      "icon_url": None})
            if held_appid is None:
                game["cache_key"] = held
            appid = game.get("appid")
            cache_key = identity = held
            source = "manual-hold"
            log(f"manual theme hold: re-running held theme "
                f"'{hold.get('name') or held}' (detected game ignored)")
        else:
            running = source in ("now-playing", "custom-process")
            played_since = (game.get("rtime_last_played") or 0) > \
                           (hold.get("held_at") or 0)
            if running or played_since:
                log(f"manual theme hold released — {source}: {game['name']}")
                state.pop("manual_hold", None)
                save_state(cfg, state)
            else:
                log(f"manual theme hold: keeping '{hold.get('name') or held}' "
                    f"(detected {game['name']} was not played since the hold)")
                return

    key = f"{source}:{identity}"
    if game.get("rtime_last_played"):
        ts = datetime.fromtimestamp(game["rtime_last_played"], timezone.utc)
        log(f"{source}: {game['name']} ({appid or cache_key}) "
            f"at {ts:%Y-%m-%d %H:%M} UTC")
    else:
        log(f"{source}: {game['name']} ({appid or cache_key})")

    if appid in excludes:
        log(f"  appid {appid} is in the exclude list; skipping")
        return
    if not force and state.get("last_identity") == identity:
        # same game as the applied theme — just refresh bookkeeping
        if state.get("last_key") != key:
            state["last_key"] = key
            save_state(cfg, state)
        log("  unchanged since last run")
        return

    result = run_pipeline(cfg, game, log, dry_run=dry_run)
    if result and not dry_run:
        state.pop("manual_hold", None)  # theming caught up with detection
        state.update({"last_key": key, "last_identity": identity,
                      "last_appid": appid, "last_cache_key": cache_key,
                      "last_name": game["name"],
                      "theme_path": result["theme_path"]})
        save_state(cfg, state)


def main():
    ap = argparse.ArgumentParser(description="Steam Theme — last-played-game wallpaper service")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--reapply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--appid", type=int, default=None)
    ap.add_argument("--ui", action="store_true", help="open the browser config page")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    if args.ui:
        import webui
        webui.serve(port=args.port)
        return

    cfg = load_config()
    log = _log_factory(cfg)
    if not os.path.exists(os.path.join(BASE_DIR, "config.json")):
        log("config.json not found — using config.example.json defaults; "
            "run 'python main.py --ui' to set your Steam API key")

    if args.reapply:
        reapply(cfg, log)
        return
    if args.once or args.appid or args.dry_run:
        check_once(cfg, log, force=args.force, appid_override=args.appid,
                   dry_run=args.dry_run)
        return

    log("service started (Ctrl+C to stop); config reloads every cycle")
    while True:
        cfg = load_config()  # live-reload so the web UI applies without restart
        interval = cfg.get("poll_interval_seconds", 30)
        try:
            check_once(cfg, log)
        except Exception as e:
            log(f"poll error: {e}")
        time.sleep(interval)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
