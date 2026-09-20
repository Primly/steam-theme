# Steam Wallpaper

Turns your last-played Steam game into a full Windows theme: per-monitor
wallpaper, accent color, dark/light mode, Windows Terminal scheme, and
(optionally) SignalRGB lighting.

## Pipeline

1. **Detect** — `steamdetect.py` polls `IPlayerService/GetOwnedGames` and sorts
   by `rtime_last_played` (more reliable than `GetRecentlyPlayedGames`, which
   has no timestamps and has shown stale-cache bugs). SteamID64 auto-detects
   from the registry.
2. **Artwork** — `artwork.py` tries SteamGridDB (heroes/logos/icons with
   ultrawide dimension filters), then the Steam CDN
   (`library_hero.jpg`, `logo.png`, `header.jpg`, steamcommunity icon), then a
   wallhaven name search. Everything is cached per-appid in `cache/`.
3. **Palette** — `palette.py` does a 16-color median-cut (PIL), with
   Material/Muted/Colorful modes, picks a WCAG-AA-checked accent, and decides
   dark vs. light from palette luminance. `force_dark_mode: true` (the
   default) pins every theme to dark, including VLM suggestions. Optionally a local VLM
   (OpenAI-compatible, e.g. LM Studio) or OpenRouter model classifies the
   mood and names the theme.
4. **Apply** — `theme.py` composites one span-style image across all monitor
   rectangles (each display center-cropped from its assigned art), writes the
   accent/dark-mode registry keys, generates a `.theme` INI, and launches it
   so Windows performs the full visual transition. Then Windows Terminal gets
   a named color scheme and SignalRGB gets pinged.

## Monitor mapping

`config.json` → `monitors` matches substrings of the display's device
description (case-insensitive):

| Match | Display | Art role |
|---|---|---|
| `LS49AG95` | Odyssey G9 (5120×1440) | hero |
| `3220DGF` | Dell S3220DGF (2560×1440) | logo |
| `PDM-15T` | small portable display | icon |

Unmatched monitors fall back by size (largest → hero, then logo, then icon).
Heroes are cover-cropped to fill their display. Transparent PNG logos and
small/square icons get a **centered treatment** instead: the art is placed on
a darkened, Gaussian-blurred backdrop of the hero, so wordmarks and icons
stay crisp rather than being zoomed and cropped.

## Setup

```powershell
pip install -r requirements.txt
copy config.example.json config.json
```

`config.json` is gitignored (it holds your API keys). Fill in:

1. **Steam key** — free at https://steamcommunity.com/dev/apikey. Leave
   `steam_id64` empty to auto-detect the logged-in Steam user.
2. **SteamGridDB key** (optional but much better art): create a free key at
   https://www.steamgriddb.com/profile/preferences/api and set
   `steamgriddb_api_key`.
3. **VLM naming** (optional): set `vlm.enabled: true` and point `base_url` at
   your LM Studio server, or set `openrouter.enabled: true` + key.

## Run

```powershell
python main.py --once            # check once, apply if the game changed
python main.py --once --dry-run  # full pipeline, changes nothing
python main.py --appid 440       # force-theme a specific game
python main.py --reapply         # re-apply cached theme (unlock trigger)
python main.py                   # polling service (every 30s)
```

### Scheduled task (service + unlock fix)

```powershell
powershell -ExecutionPolicy Bypass -File install_task.ps1
```

Creates `SteamWallpaperTheme` (logon → polling service) and
`SteamWallpaperTheme-Unlock` (workstation unlock → `--reapply`), which fixes
the half-applied "hybrid Custom" theme Windows can leave when a theme is
applied while the session is locked.

## Gotchas

- `.theme` files reference wallpapers by **absolute path** — `cache/` must not
  be cleaned blindly or applied themes break.
- The Web API only sees Steam-launched games. Epic/GOG/non-Steam shortcuts
  would need process-exit detection instead (WMI `__InstanceDeletionEvent`).
- `exclude_appids` defaults to `[431960]` (Wallpaper Engine — not a game).
- SignalRGB's local API surface varies by version; if the ping fails, check
  the logged error and adjust `extras.apply_signalrgb`.
