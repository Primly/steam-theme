"""Stage 1: detect the last-played Steam game.

Uses IPlayerService/GetOwnedGames with rtime_last_played (UTC unix ts),
which is more reliable than GetRecentlyPlayedGames (no timestamps, and it
has shown stale-cache bugs). SteamID64 is auto-detected from the registry
when not set in config.
"""

import glob
import json
import os
import re
import sys
import urllib.request

STEAM_ID_BASE = 76561197960265728


def get_steam_id64():
    """Derive SteamID64 from the currently logged-in Steam user."""
    if sys.platform != "win32":
        return _steam_id64_linux()
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


def _steam_id64_linux():
    path = os.path.expanduser("~/.local/share/Steam/config/loginusers.vdf")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return parse_loginusers(f.read())
    except OSError:
        return None


def parse_loginusers(text):
    """loginusers.vdf -> SteamID64 of the most relevant account.

    Prefer the user flagged MostRecent, then AutoLogin, then the newest
    Timestamp. VDF top-level keys are SteamID64 strings."""
    users = re.findall(r'"(\d{17})"\s*\{([^}]*)\}', text, re.S)
    if not users:
        return None

    def val(block, key):
        m = re.search(r'"%s"\s*"([^"]*)"' % key, block)
        return m.group(1) if m else ""

    for sid, block in users:
        if val(block, "MostRecent") == "1":
            return sid
    for sid, block in users:
        if val(block, "AutoLogin") == "1":
            return sid
    return max(users, key=lambda u: int(val(u[1], "Timestamp") or 0))[0]


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
    if sys.platform != "win32":
        return _running_appid_linux()
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam")
        appid, _ = winreg.QueryValueEx(key, "RunningAppID")
        return int(appid) or None
    except (OSError, ValueError):
        return None


def _running_appid_linux():
    return _appid_from_proc_environ() or _running_appid_from_registry_vdf()


def appid_from_environ(data):
    """/proc/<pid>/environ bytes -> Steam appid, or None. Pure/testable."""
    m = (re.search(rb"(?:^|\0)SteamAppId=(\d+)", data)
         or re.search(rb"(?:^|\0)SteamGameId=(\d+)", data))
    if not m:
        return None
    return int(m.group(1)) or None


def _appid_from_proc_environ():
    for env_path in sorted(glob.glob("/proc/[0-9]*/environ")):
        try:
            with open(env_path, "rb") as f:
                appid = appid_from_environ(f.read())
        except OSError:
            continue
        if appid:
            return appid
    return None


def parse_running_appid_vdf(text):
    """registry.vdf (Wine-style mirror of HKCU) -> RunningAppID, or None."""
    m = re.search(r'"RunningAppID"\s*"(\d+)"', text)
    return int(m.group(1)) if m and int(m.group(1)) else None


def _running_appid_from_registry_vdf():
    try:
        with open(os.path.expanduser("~/.steam/registry.vdf"),
                  encoding="utf-8", errors="replace") as f:
            return parse_running_appid_vdf(f.read())
    except OSError:
        return None


def find_running_custom_game(custom_games):
    """Return the first custom_games entry whose process is running, else None.

    Each entry: {"process": "game.exe", "appid": 123 (optional),
                 "name": "Display Name" (optional)}. This covers non-Steam
    games (Epic/GOG/emulators): poll for the process instead of the Steam API.
    """
    entries = [e for e in (custom_games or []) if e.get("process")]
    if not entries:
        return None
    running = (_running_process_names_linux() if sys.platform != "win32"
               else _running_process_names_windows())
    if running is None:
        return None
    for e in entries:
        if e["process"].strip().lower() in running:
            return e
    return None


def _running_process_names_windows():
    """Lowercased image names from tasklist, or None on failure."""
    import subprocess
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
    return running


def _running_process_names_linux():
    """Lowercased process names from /proc: comm plus argv[0] basename
    (argv0 covers Wine/Proton games, whose comm may be a wrapper)."""
    running = set()
    for comm in glob.glob("/proc/[0-9]*/comm"):
        try:
            with open(comm, encoding="utf-8", errors="replace") as f:
                name = f.read().strip().lower()
            if name:
                running.add(name)
        except OSError:
            pass
    for cmd in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            with open(cmd, "rb") as f:
                argv0 = f.read().split(b"\0")[0]
            if argv0:
                running.add(os.path.basename(
                    argv0.decode(errors="replace")).lower())
        except OSError:
            pass
    return running
