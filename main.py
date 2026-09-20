"""Steam Wallpaper service.

Polls Steam for the last-played game; when it changes, runs the pipeline:
  detect -> fetch art -> palette -> compose per-monitor wallpaper -> .theme
  -> Windows Terminal / SignalRGB extras.

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
import sys
import time
from datetime import datetime, timezone

import artwork
import extras
import palette as palette_mod
import steamdetect
import theme as theme_mod
import upscale

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_config():
    with open(os.path.join(BASE_DIR, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def _log_factory(cfg):
    log_path = os.path.join(BASE_DIR, cfg.get("log_file", "service.log"))

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
    return os.path.join(BASE_DIR, cfg.get("state_file", "state.json"))


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
    appid, name = game["appid"], game["name"]
    cache = os.path.join(BASE_DIR, cfg["cache_dir"], str(appid))
    log(f"pipeline: {name} ({appid})")

    # Stage 2 — art (cached per appid)
    art = artwork.fetch_artwork(appid, name, game.get("icon_url"),
                                {**cfg, "cache_dir": os.path.join(BASE_DIR, cfg["cache_dir"])},
                                log)
    if not art:
        log("  !! no artwork found at all; aborting")
        return None

    # monitors first, so art can be upscaled toward its target display
    monitors = theme_mod.assign_roles(theme_mod.enumerate_monitors(),
                                      cfg.get("monitors", []), log)
    for m in monitors:
        role = m["role"]
        if role in art:
            w, h = m["rect"][2] - m["rect"][0], m["rect"][3] - m["rect"][1]
            if role in ("logo", "icon"):  # centered roles only need the fit box
                w, h = round(w * 0.8), round(h * 0.7)
            art[role] = upscale.maybe_upscale(art[role], w, h, cfg, log)

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
    theme_name = (ai or {}).get("theme_name") or f"{name} — Steam Wallpaper"
    mood = (ai or {}).get("mood", "")
    pal["theme_name"] = theme_name
    pal["mood"] = mood
    log(f"  [pal] {pal['appearance']} mode, accent {pal['accent']} "
        f"({pal['accent_grade']}, {pal['accent_contrast']}:1), theme '{theme_name}'")

    # Stage 4 — composite + theme
    wallpaper = theme_mod.compose_wallpaper(monitors, art,
                                            os.path.join(cache, "wallpaper_span.jpg"), log)
    theme_path = theme_mod.write_theme_file(
        os.path.join(cache, "theme.theme"), theme_name, wallpaper, pal)

    with open(os.path.join(cache, "palette.json"), "w", encoding="utf-8") as f:
        json.dump({**pal, "wallpaper": os.path.abspath(wallpaper)}, f, indent=2)

    if dry_run:
        log("  [dry-run] skipping registry writes, theme apply, and extras")
        return {"theme_path": theme_path, "palette": pal, "wallpaper": wallpaper}

    theme_mod.write_registry_colors(pal, log)
    theme_mod.apply_theme(theme_path, log)
    extras.apply_windows_terminal(cfg, pal, theme_name, art.get("hero"), log)
    extras.apply_signalrgb(cfg, pal, mood, log)
    return {"theme_path": theme_path, "palette": pal, "wallpaper": wallpaper}


def reapply(cfg, log):
    state = load_state(cfg)
    theme_path = state.get("theme_path")
    if theme_path and os.path.exists(theme_path):
        log("re-applying cached theme after unlock")
        theme_mod.apply_theme(theme_path, log)
    else:
        log("nothing to re-apply")


def check_once(cfg, log, force=False, appid_override=None, dry_run=False):
    steam_id = cfg.get("steam_id64") or steamdetect.get_steam_id64()
    if appid_override:
        game = {"appid": appid_override, "name": f"app {appid_override}",
                "rtime_last_played": int(time.time()), "icon_url": None}
        # resolve the real name if we can
        try:
            g = steamdetect.get_last_played_game(cfg["steam_api_key"], steam_id)
            if g and g["appid"] == appid_override:
                game = g
        except Exception:
            pass
    else:
        game = steamdetect.get_last_played_game(cfg["steam_api_key"], steam_id)
    if not game:
        log("no played games returned by the Steam API")
        return

    state = load_state(cfg)
    key = f"{game['appid']}:{game['rtime_last_played']}"
    ts = datetime.fromtimestamp(game["rtime_last_played"], timezone.utc)
    log(f"last played: {game['name']} ({game['appid']}) at {ts:%Y-%m-%d %H:%M} UTC")

    if game["appid"] in cfg.get("exclude_appids", []):
        log(f"  appid {game['appid']} is in the exclude list; skipping")
        return
    if not force and state.get("last_key") == key:
        log("  unchanged since last run")
        return

    result = run_pipeline(cfg, game, log, dry_run=dry_run)
    if result and not dry_run:
        state.update({"last_key": key, "last_appid": game["appid"],
                      "last_name": game["name"], "theme_path": result["theme_path"]})
        save_state(cfg, state)


def main():
    ap = argparse.ArgumentParser(description="Steam last-played-game wallpaper service")
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
