"""Stage 1: detect the last-played Steam game.

Uses IPlayerService/GetOwnedGames with rtime_last_played (UTC unix ts),
which is more reliable than GetRecentlyPlayedGames (no timestamps, and it
has shown stale-cache bugs). SteamID64 is auto-detected from the registry
when not set in config.
"""

import json
import urllib.request

STEAM_ID_BASE = 76561197960265728


def get_steam_id64():
    """Derive SteamID64 from the currently logged-in Steam user's registry entry."""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Valve\Steam\ActiveProcess")
        account_id, _ = winreg.QueryValueEx(key, "ActiveUser")
        if account_id:
            return str(STEAM_ID_BASE + account_id)
    except OSError:
        pass
    return None


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "steam-theme/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def _public_game(g):
    icon_hash = g.get("img_icon_url")
    return {
        "appid": g["appid"],
        "name": g.get("name", f"app {g['appid']}"),
        "rtime_last_played": g.get("rtime_last_played", 0),
        "icon_url": (
            "https://media.steampowered.com/steamcommunity/public/images/"
            f"apps/{g['appid']}/{icon_hash}.jpg" if icon_hash else None
        ),
    }


def get_owned_games(api_key, steam_id64):
    """Full owned-games list (public shape); [] on empty response."""
    url = (
        "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
        f"?key={api_key}&steamid={steam_id64}"
        "&include_appinfo=1&include_played_free_games=1&format=json"
    )
    return [_public_game(g)
            for g in (_get_json(url).get("response", {}).get("games") or [])]


def get_last_played_game(api_key, steam_id64):
    """Return {appid, name, rtime_last_played, icon_url} or None."""
    played = [g for g in get_owned_games(api_key, steam_id64)
              if g.get("rtime_last_played")]
    return max(played, key=lambda x: x["rtime_last_played"]) if played else None


def get_running_appid():
    """AppID of the game Steam is currently running (0/None when nothing is).

    Steam maintains HKCU\\Software\\Valve\\Steam\\RunningAppID while a
    Steam-launched game is up. Reading it lets us theme a game the moment it
    launches instead of waiting for it to become 'last played'.
    """
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam")
        appid, _ = winreg.QueryValueEx(key, "RunningAppID")
        return int(appid) or None
    except (OSError, ValueError):
        return None


def find_running_custom_game(custom_games):
    """Return the first custom_games entry whose process is running, else None.

    Each entry: {"process": "game.exe", "appid": 123 (optional),
                 "name": "Display Name" (optional)}. This covers non-Steam
    games (Epic/GOG/emulators): poll for the process instead of the Steam API.
    """
    import subprocess
    entries = [e for e in (custom_games or []) if e.get("process")]
    if not entries:
        return None
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    running = set()
    for line in out.splitlines():
        # CSV row: "name.exe","pid",...
        name = line.split('","', 1)[0].strip('"').lower()
        if name:
            running.add(name)
    for e in entries:
        if e["process"].strip().lower() in running:
            return e
    return None
