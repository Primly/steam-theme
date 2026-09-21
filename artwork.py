"""Stage 2: fetch wallpaper art for a game.

Fallback chain per role:
  hero: SteamGridDB hero -> Steam CDN library_hero.jpg -> header.jpg -> wallhaven search
  logo: SteamGridDB logo -> Steam CDN logo.png -> header.jpg
  icon: SteamGridDB icon -> steamcommunity icon (hash from GetOwnedGames) -> header.jpg

Very small art (e.g. 64px icons) is rejected for full-screen use; the caller
falls back to the hero in that case.
"""

import os
import requests
from PIL import Image

SGDB = "https://www.steamgriddb.com/api/v2"
CDN = "https://cdn.cloudflare.steamstatic.com/steam/apps"

UA = {"User-Agent": "steam-theme/1.0"}
MIN_DIMENSION = 512  # px; anything smaller is too blurry to fill a monitor


def _download(url, dest):
    r = requests.get(url, headers=UA, timeout=30)
    if r.status_code != 200 or len(r.content) < 5000:
        return None
    with open(dest, "wb") as f:
        f.write(r.content)
    try:
        with Image.open(dest) as im:
            im.verify()
    except Exception:
        os.remove(dest)
        return None
    return dest


def _big_enough(path):
    try:
        with Image.open(path) as im:
            return min(im.size) >= MIN_DIMENSION
    except Exception:
        return False


def _sgdb_get(path, key, params=None):
    r = requests.get(SGDB + path, headers={**UA, "Authorization": f"Bearer {key}"},
                     params=params, timeout=20)
    if r.status_code != 200:
        return None
    return r.json()


def _sgdb_art(appid, key):
    """Return {hero, logo, icon} of candidate URLs from SteamGridDB (may contain None)."""
    out = {"hero": None, "logo": None, "icon": None}
    game = _sgdb_get(f"/games/steam/{appid}", key)
    if not game or not game.get("data"):
        return out
    gid = game["data"]["id"]
    hero = _sgdb_get(f"/heroes/game/{gid}", key,
                     {"dimensions": "3840x1240,1920x620,1600x650"})
    if hero and hero.get("data"):
        out["hero"] = hero["data"][0]["url"]
    logo = _sgdb_get(f"/logos/game/{gid}", key, {"types": "static"})
    if logo and logo.get("data"):
        out["logo"] = logo["data"][0]["url"]
    icon = _sgdb_get(f"/icons/game/{gid}", key)
    if icon and icon.get("data"):
        out["icon"] = icon["data"][0]["url"]
    return out


def _wallhaven_hero(name, key=None):
    params = {"q": name, "categories": "100", "atleast": "2560x1440",
              "sorting": "relevance", "order": "desc"}
    if key:
        params["apikey"] = key
    try:
        r = requests.get("https://wallhaven.cc/api/v1/search", params=params,
                         headers=UA, timeout=20)
        data = r.json().get("data") or []
        if data:
            return data[0]["path"]
    except Exception:
        pass
    return None


def fetch_artwork(appid, name, icon_url, cfg, log=print):
    """Fetch art into cache/<appid>/ and return {hero, logo, icon} local paths.

    Any role that fails is simply absent from the dict; callers fall back to hero.
    """
    cache = os.path.join(cfg["cache_dir"], str(appid))
    os.makedirs(cache, exist_ok=True)
    sgdb_key = cfg.get("steamgriddb_api_key") or ""

    candidates = {"hero": [], "logo": [], "icon": []}

    if sgdb_key:
        try:
            sgdb = _sgdb_art(appid, sgdb_key)
            for role in candidates:
                if sgdb.get(role):
                    candidates[role].append(sgdb[role])
        except Exception as e:
            log(f"  [art] SteamGridDB error: {e}")
    else:
        log("  [art] no SteamGridDB key configured, skipping (cdn.steamgriddb.com fallback)")

    candidates["hero"] += [
        f"{CDN}/{appid}/library_hero.jpg",
        f"{CDN}/{appid}/header.jpg",
    ]
    candidates["logo"] += [
        f"{CDN}/{appid}/logo.png",
        f"{CDN}/{appid}/header.jpg",
    ]
    if icon_url:
        candidates["icon"].append(icon_url)
    candidates["icon"].append(f"{CDN}/{appid}/header.jpg")

    result = {}
    for role, urls in candidates.items():
        dest = os.path.join(cache, f"{role}.jpg")
        if os.path.exists(dest) and (_big_enough(dest) or role != "hero"):
            result[role] = dest
            continue
        for url in urls:
            log(f"  [art] trying {role}: {url}")
            got = _download(url, dest)
            # heroes must be big; logos/icons may be small or transparent PNGs —
            # the compositor gives those a centered-on-backdrop treatment
            if got and (_big_enough(got) or role != "hero"):
                result[role] = got
                break
        if role not in result:
            log(f"  [art] no usable {role} art from primary sources")

    if "hero" not in result:
        wh = _wallhaven_hero(name, cfg.get("wallhaven_api_key") or None)
        if wh:
            log(f"  [art] wallhaven fallback: {wh}")
            got = _download(wh, os.path.join(cache, "hero.jpg"))
            if got:
                result["hero"] = got

    return result
