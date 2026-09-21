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


def get_last_played_game(api_key, steam_id64):
    """Return {appid, name, rtime_last_played, icon_url} or None."""
    url = (
        "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
        f"?key={api_key}&steamid={steam_id64}"
        "&include_appinfo=1&include_played_free_games=1&format=json"
    )
    games = _get_json(url).get("response", {}).get("games") or []
    played = [g for g in games if g.get("rtime_last_played")]
    if not played:
        return None
    g = max(played, key=lambda x: x["rtime_last_played"])
    icon_hash = g.get("img_icon_url")
    return {
        "appid": g["appid"],
        "name": g.get("name", f"app {g['appid']}"),
        "rtime_last_played": g["rtime_last_played"],
        "icon_url": (
            "https://media.steampowered.com/steamcommunity/public/images/"
            f"apps/{g['appid']}/{icon_hash}.jpg" if icon_hash else None
        ),
    }
