"""Stage 2: fetch wallpaper art for a game.

Fallback chain per role:
  hero: SteamGridDB heroes -> Steam CDN library_hero.jpg -> wallhaven search
  logo: SteamGridDB logo -> Steam CDN logo.png -> header.jpg
  icon: SteamGridDB icon -> steamcommunity icon (hash from GetOwnedGames) -> header.jpg

Heroes come in MULTIPLES (cfg hero.count, default 6): SteamGridDB usually
has several, and the user picks one in the config UI (hero_choice.json in the
cache folder) or rotates them as a slideshow (hero.mode = "rotate").

Very small art (e.g. 64px icons) is rejected for full-screen use; the caller
falls back to the hero in that case.
"""

import glob
import json
import os
import re
import urllib.parse

import requests
from PIL import Image

SGDB = "https://www.steamgriddb.com/api/v2"
CDN = "https://cdn.cloudflare.steamstatic.com/steam/apps"

UA = {"User-Agent": "steam-theme/1.0"}
MIN_DIMENSION = 512  # px; anything smaller is too blurry to fill a monitor
MAX_HERO_CANDIDATES = 8


def _normalize_image(path, log=print):
    """Ensure a cached image is a real JPEG or PNG with a matching extension.

    Sources lie about formats: Steam community icons are ICO, some SGDB art
    is WebP, PNGs arrive at .jpg names. Browsers and PIL don't care, but the
    Topaz API only accepts JPEG/PNG/TIFF (a mislabeled file gets a 415).
    - right format, wrong extension -> plain rename (lossless, no re-encode)
    - anything else (ICO, WebP, GIF, TIFF, ...) -> transcode: PNG when the
      image has transparency (logos/icons need it), JPEG otherwise
    Returns the final path.
    """
    try:
        with Image.open(path) as im:
            fmt = (im.format or "").upper()
            alpha = (im.mode in ("RGBA", "LA")
                     or (im.mode == "P" and "transparency" in im.info))
            want_ext = ".jpg" if fmt == "JPEG" else ".png" if fmt == "PNG" else None
            if want_ext and path.lower().endswith(want_ext):
                return path  # already canonical
            # Windows locks the file while PIL has it open, so all renames
            # and saves happen below, AFTER the with-block closes it
            img = None if want_ext else im.convert("RGBA" if alpha else "RGB")
        stem = os.path.splitext(path)[0]
        if want_ext:
            dest = stem + want_ext
            if os.path.exists(dest):
                os.remove(dest)  # stale duplicate of the same stem
            os.replace(path, dest)
            log(f"  [art] renamed {os.path.basename(path)} -> "
                f"{os.path.basename(dest)} (content is {fmt})")
            return dest
        dest = stem + (".png" if alpha else ".jpg")
        img.save(dest, "PNG" if alpha else "JPEG", quality=92)
        if os.path.abspath(dest) != os.path.abspath(path):
            os.remove(path)
        log(f"  [art] transcoded {fmt or 'unknown'} -> "
            f"{'PNG' if alpha else 'JPEG'}: {os.path.basename(dest)}")
        return dest
    except Exception as e:
        log(f"  [art] normalize skipped for {os.path.basename(path)}: {e}")
        return path


def _download(url, dest, log=print):
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
    return _normalize_image(dest, log)


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


def _sgdb_art(appid, key, name=None, hero_count=1):
    """Return {"heroes": [urls], "logo": url, "icon": url} from SteamGridDB.
    Non-Steam games have no appid: resolve via the autocomplete search instead."""
    out = {"heroes": [], "logo": None, "icon": None}
    gid = None
    if appid:
        game = _sgdb_get(f"/games/steam/{appid}", key)
        gid = game and (game.get("data") or {}).get("id")
    elif name:
        res = _sgdb_get(f"/search/autocomplete/{urllib.parse.quote(name)}", key)
        hits = (res or {}).get("data") or []
        gid = hits and hits[0].get("id")
    if not gid:
        return out
    heroes = _sgdb_get(f"/heroes/game/{gid}", key,
                       {"dimensions": "3840x1240,1920x620,1600x650"})
    if heroes and heroes.get("data"):
        out["heroes"] = [h["url"] for h in heroes["data"][:hero_count]
                         if h.get("url")]
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


# ------------------------------------------------------------ hero helpers

def _hero_num(path):
    m = re.fullmatch(r"hero_(\d+)\.(?:jpg|png)", os.path.basename(path))
    return int(m.group(1)) if m else 0


def hero_candidates(cache_dir):
    """All cached hero candidates, display order: legacy hero.jpg/hero.png
    first, then hero_1, hero_2, ... (numeric sort — numbers can pass 9 as
    files are pruned and re-fetched over time).

    Filenames must fullmatch hero_<digits>.(jpg|png) exactly — the glob
    alone would also catch hero_N_upscaled_<model>.png (the upscale cache),
    corrupting both the list and every picker index."""
    out = []
    for name in ("hero.jpg", "hero.png"):  # legacy single-hero cache files
        p = os.path.join(cache_dir, name)
        if os.path.exists(p):
            out.append(p)
    numbered = (glob.glob(os.path.join(cache_dir, "hero_*.jpg"))
                + glob.glob(os.path.join(cache_dir, "hero_*.png")))
    out += sorted((p for p in numbered if _hero_num(p)), key=_hero_num)
    return out


def _next_hero_dest(cache_dir, taken):
    """First free hero_N slot after the taken ones (new candidates always
    sort after existing ones in display order). The extension is provisional
    — _normalize_image fixes it to match the actual content."""
    n = max([_hero_num(p) for p in taken] + [0]) + 1
    return os.path.join(cache_dir, f"hero_{n}.jpg")


def hero_choice(cache_dir):
    """The user's chosen hero index (0-based), clamped to the candidate list."""
    try:
        with open(os.path.join(cache_dir, "hero_choice.json"),
                  encoding="utf-8") as f:
            idx = int(json.load(f).get("index", 0))
    except (OSError, ValueError):
        idx = 0
    n = len(hero_candidates(cache_dir))
    return min(max(idx, 0), n - 1) if n else 0


def active_hero(cache_dir):
    """Path of the currently-chosen hero candidate, or None."""
    cands = hero_candidates(cache_dir)
    return cands[hero_choice(cache_dir)] if cands else None


def hero_count_for(cfg):
    """Sanitized cfg['hero']['count'] (1..MAX_HERO_CANDIDATES, default 6)."""
    hero_cfg = cfg.get("hero") if isinstance(cfg.get("hero"), dict) else {}
    try:
        n = int(hero_cfg.get("count", 6) or 6)
    except (TypeError, ValueError):
        n = 6
    return max(1, min(n, MAX_HERO_CANDIDATES))


def art_is_cached(cache_dir, hero_count=1):
    """True when the cache folder already has the full art set: at least
    hero_count hero candidates plus a logo and an icon. Used by Ultimate
    Fetch to skip complete games without touching the network."""
    if len(hero_candidates(cache_dir)) < max(1, hero_count):
        return False
    return all(
        any(os.path.exists(os.path.join(cache_dir, f"{role}{ext}"))
            for ext in (".jpg", ".png"))
        for role in ("logo", "icon"))


def set_hero_choice(cache_dir, index):
    """Persist the user's hero pick; regenerate applies it."""
    n = len(hero_candidates(cache_dir))
    if not n:
        raise ValueError("no hero candidates cached")
    index = int(index)
    if not 0 <= index < n:
        raise ValueError(f"hero index {index} out of range (0..{n - 1})")
    with open(os.path.join(cache_dir, "hero_choice.json"), "w",
              encoding="utf-8") as f:
        json.dump({"index": index}, f)
    return index


def fetch_artwork(appid, name, icon_url, cfg, log=print, cache_key=None):
    """Fetch art into cache/<key>/ and return local paths:
    {hero, logo, icon, hero_candidates}.

    'hero' is the ACTIVE candidate (user's hero_choice.json pick, default the
    first); 'hero_candidates' lists them all for the slideshow mode. Any role
    that fails is simply absent; callers fall back to hero. appid may be None
    for non-Steam custom games (SGDB name search + wallhaven only).
    """
    cache = os.path.join(cfg["cache_dir"], cache_key or str(appid))
    os.makedirs(cache, exist_ok=True)
    sgdb_key = cfg.get("steamgriddb_api_key") or ""
    hero_count = hero_count_for(cfg)

    hero_urls, logo_urls, icon_urls = [], [], []

    if sgdb_key:
        try:
            sgdb = _sgdb_art(appid, sgdb_key, name=name, hero_count=hero_count)
            hero_urls += sgdb["heroes"]
            if sgdb.get("logo"):
                logo_urls.append(sgdb["logo"])
            if sgdb.get("icon"):
                icon_urls.append(sgdb["icon"])
        except Exception as e:
            log(f"  [art] SteamGridDB error: {e}")
    else:
        log("  [art] no SteamGridDB key configured, skipping (cdn.steamgriddb.com fallback)")

    if appid:  # Steam CDN only exists for real appids
        hero_urls.append(f"{CDN}/{appid}/library_hero.jpg")
        logo_urls += [f"{CDN}/{appid}/logo.png", f"{CDN}/{appid}/header.jpg"]
        if icon_url:
            icon_urls.append(icon_url)
        icon_urls.append(f"{CDN}/{appid}/header.jpg")

    # heroes: keep cached candidates (count is authoritative — extras are
    # pruned), drop any too small to use, then top up from sources. Keeping
    # `heroes` identical to hero_candidates(cache) matters: hero_choice()
    # indexes into the candidate list.
    existing = hero_candidates(cache)
    for p in existing[hero_count:]:
        try:
            os.remove(p)
            log(f"  [art] hero count={hero_count}: dropped "
                f"{os.path.basename(p)}")
        except OSError:
            pass
    heroes = []
    for p in existing[:hero_count]:
        p = _normalize_image(p, log)  # fix legacy formats/extensions in place
        if _big_enough(p):
            heroes.append(p)
        else:
            try:
                os.remove(p)
                log(f"  [art] {os.path.basename(p)} is too small for a "
                    "wallpaper — deleted")
            except OSError:
                heroes.append(p)  # can't delete -> keep indexes aligned
    for url in hero_urls:
        if len(heroes) >= hero_count:
            break
        dest = _next_hero_dest(cache, heroes)
        log(f"  [art] trying hero #{len(heroes) + 1}: {url}")
        got = _download(url, dest, log)
        if got and _big_enough(got):
            heroes.append(got)
        elif got:
            os.remove(got)  # too small for a wallpaper; don't cache it
    if not heroes:
        wh = _wallhaven_hero(name, cfg.get("wallhaven_api_key") or None)
        if wh:
            log(f"  [art] wallhaven fallback: {wh}")
            got = _download(wh, _next_hero_dest(cache, heroes), log)
            if got:
                heroes.append(got)
    if len(heroes) > 1:
        log(f"  [art] {len(heroes)} hero candidates cached")

    result = {}
    for role, urls in (("logo", logo_urls), ("icon", icon_urls)):
        cached = next((p for p in (os.path.join(cache, f"{role}.jpg"),
                                   os.path.join(cache, f"{role}.png"))
                       if os.path.exists(p)), None)
        if cached:
            result[role] = _normalize_image(cached, log)
            continue
        for url in urls:
            log(f"  [art] trying {role}: {url}")
            got = _download(url, os.path.join(cache, f"{role}.jpg"), log)
            # logos/icons may be small or transparent PNGs — the compositor
            # gives those a centered-on-backdrop treatment
            if got:
                result[role] = got
                break
        if role not in result:
            log(f"  [art] no usable {role} art from primary sources")

    if heroes:
        idx = hero_choice(cache)
        result["hero"] = heroes[idx]
        result["hero_candidates"] = heroes
        if idx:
            log(f"  [art] hero: user-picked candidate #{idx + 1}")
    return result
