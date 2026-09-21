# Steam Theme

Turns your last-played Steam game into a full Windows theme: per-monitor
wallpaper, accent color, dark/light mode, Windows Terminal scheme, and
(optionally) SignalRGB lighting.

## Pipeline

1. **Detect** — `steamdetect.py` picks what to theme, in priority order:
   a watched **non-Steam process** that's running (custom games), a **Steam
   game running right now** (`HKCU\Software\Valve\Steam\RunningAppID`,
   toggleable as *now playing*), otherwise the **last-played** game from
   `IPlayerService/GetOwnedGames` sorted by `rtime_last_played` (more
   reliable than `GetRecentlyPlayedGames`, which has no timestamps and has
   shown stale-cache bugs). SteamID64 auto-detects from the registry.
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

## Getting started (from zero)

No git or programming experience needed — follow the steps in order. All
commands go in **PowerShell** (Start menu → type `PowerShell` → Enter).

### 1. Install Python

1. Download Python 3.10 or newer from <https://www.python.org/downloads/>.
2. Run the installer. **Tick the “Add python.exe to PATH” checkbox on the
   first screen** — this is required, then click Install.
3. Close and reopen PowerShell, then verify:

   ```powershell
   python --version
   ```

   You should see something like `Python 3.13.5`. If you get an error, the
   PATH checkbox was missed — re-run the installer and tick it.

### 2. Get the code

**Option A — with git (recommended; makes updates one command):**

1. Install Git from <https://git-scm.com/download/win> (defaults are fine —
   keep clicking Next).
2. In PowerShell, pick a home for the project and clone it:

   ```powershell
   cd $env:USERPROFILE\Documents
   git clone https://github.com/Primly/steam-theme.git
   cd steam-theme
   ```

   The repository is public, so cloning just works.

**Option B — without git:**

1. Open <https://github.com/Primly/steam-theme> in your browser,
   click the green **Code** button → **Download ZIP**.
2. Extract the ZIP, then in PowerShell `cd` into the extracted folder, e.g.:

   ```powershell
   cd $env:USERPROFILE\Downloads\steam-theme-main
   ```

**Updating later:** with Option A, run `git pull` inside the project folder.
With Option B, download a fresh ZIP.

### 3. Install the dependencies

From the project folder:

```powershell
pip install -r requirements.txt
```

### 4. Configure it in your browser

```powershell
python main.py --ui
```

This creates `config.json` on first save and opens the settings page at
<http://127.0.0.1:8765> — everything is point-and-click:

1. **Steam** — paste a free Steam Web API key from
   <https://steamcommunity.com/dev/apikey> (there's a link in the UI), click
   **Auto-detect** for the SteamID64, then **Test** to verify.
2. **Artwork sources** — optional but *much* better art: a free SteamGridDB
   key from <https://www.steamgriddb.com/profile/preferences/api>.
3. Everything else (AI/VLM, Topaz upscaling, SignalRGB, Windows Terminal) is
   optional and documented inline in the UI, with test buttons.

`config.json` is gitignored — your API keys never leave your machine via git.

### 5. Try it

```powershell
python main.py --once --dry-run   # full pipeline, changes nothing
python main.py --once             # real run: wallpaper + theme apply
```

### 6. Make it automatic

```powershell
powershell -ExecutionPolicy Bypass -File install_task.ps1
```

This registers the background service and config page to start at logon —
see [Scheduled task](#scheduled-task-service--unlock-fix) for details.

## Run

```powershell
python main.py --once            # check once, apply if the game changed
python main.py --once --dry-run  # full pipeline, changes nothing
python main.py --appid 440       # force-theme a specific game
python main.py --reapply         # re-apply cached theme (unlock trigger)
python main.py --ui              # browser config page at http://127.0.0.1:8765
python main.py                   # polling service (every 30s)
```

## Browser config UI

`python main.py --ui` serves a localhost-only settings page
(http://127.0.0.1:8765):

- **General** — appearance preference (always dark / always light / auto from
  wallpaper brightness), palette style, poll interval, excluded app IDs, and
  the *now playing* toggle (theme a Steam game the moment it launches rather
  than after it becomes your last-played).
- **Steam** — API key + SteamID64 with auto-detect, live connection test.
- **Artwork sources** — SteamGridDB / wallhaven keys, live key test.
- **AI / VLM** — OpenAI-compatible endpoint setup with presets (LM Studio,
  Ollama, OpenRouter), model dropdown populated from the server's `/models`,
  and a live chat-completion test with latency.
- **Upscaling** — undersized artwork can be upscaled via the Topaz Gigapixel
  API (cloud, per-image credits; the test button verifies the key *without*
  spending any) or the AI section's endpoint (OpenAI Images API, best-effort).
- **Non-Steam games** — a process watch list so Epic/GOG/emulator titles
  get themed too (see below).
- **Extras** — Windows Terminal + SignalRGB toggles.
- **Monitors** — auto-detected display list with per-display role mapping.
- **Theme gallery** — every cached game theme with thumbnails; re-apply any
  of them instantly (no API calls, no Topaz credits).
- **Activity log** — live tail of `service.log`; "Save & apply now" re-runs
  the pipeline immediately.

### Undo: Restore Windows look

The first time the pipeline applies a theme it snapshots your existing
wallpaper, accent color, and light/dark modes to `windows_backup.json`
(gitignored, never overwritten — so it always holds *your* pre-app look,
not one of our themes). The **Restore Windows look** button in the UI writes
that snapshot back. To fully uninstall: restore, then remove the scheduled
tasks (commands at the top of `install_task.ps1`) and delete the folder.

### Non-Steam games

Steam's API only sees Steam-launched games, so `custom_games` watches
processes instead. One per line in the UI (or JSON in `config.json`):

```
eldenring.exe | 1245620 | ELDEN RING
retroarch.exe |         | RetroArch
```

`process | appid (optional) | display name (optional)`. While the process is
running it wins over Steam detection; when it exits, the theme returns to
your Steam game. An appid (from any Steam store page URL) gets proper Steam
CDN + SteamGridDB art; without one, art comes from a SteamGridDB name search
with a wallhaven fallback.

The polling service reloads `config.json` every cycle, so UI saves take
effect without a restart.

### Privacy & security

- **Your keys stay local.** `config.json` holds your Steam / SteamGridDB /
  Topaz / AI keys. It is gitignored and is never transmitted anywhere except
  to the service each key belongs to. Only the placeholder
  `config.example.json` is committed.
- **The config UI is localhost-only.** It binds to `127.0.0.1` and
  additionally verifies the `Host`/`Origin` headers on every request, which
  blocks DNS-rebinding and cross-site (CSRF) requests from web pages — a
  malicious site open in your browser cannot read your keys or change
  settings through the local server. `Origin` must match the server's own
  origin *exactly*, so a compromised page served by another localhost app
  (a dev server, LM Studio, SignalRGB, ...) can't POST to it either.
- **Config-supplied paths are sandboxed.** `cache_dir`, `state_file`, and
  `log_file` must resolve inside the app folder; anything else is rejected,
  so a hostile config can't turn the log viewer into a file reader or the
  refetch button into a directory deleter.
- **Machine-generated content is treated as untrusted.** Theme names from a
  VLM are control-character-stripped before going into a `.theme` INI file,
  the UI renders all server-supplied strings as text (never as HTML), and
  palette colors are validated as `#RRGGBB` before being baked into the
  SignalRGB effect (an HTML/JS file SignalRGB executes).
- **Supply chain:** Dependabot watches the pip requirements and the GitHub
  Actions workflows for known-vulnerable versions; CI runs the test suite
  (including the guard tests above) on every push and pull request.

### Scheduled task (service + unlock fix)

```powershell
powershell -ExecutionPolicy Bypass -File install_task.ps1
```

Creates `SteamTheme` (logon → polling service),
`SteamThemeUI` (logon → config page on http://127.0.0.1:8765), and —
when run from an **elevated** shell — `SteamTheme-Unlock`
(workstation unlock → `--reapply`), which fixes the half-applied "hybrid
Custom" theme Windows can leave when a theme is applied while the session is
locked. Without elevation the unlock task is skipped with a note. Re-running
the installer automatically migrates tasks registered under the app's old
name (`SteamWallpaperTheme` / `SteamWallpaperUI`).

## SignalRGB integration — how it works

### What you get

Every time the pipeline themes a game, your RGB lighting switches to an
effect **generated from that game's palette** — not just a static effect you
picked beforehand. The generated effect appears in SignalRGB's effect list
as **“Steam Theme”** and is applied automatically.

### Why it works this way

SignalRGB's local REST API can **apply** existing effects and **read** their
settings, but it cannot **change** an effect's colors — parameter writes are
silently ignored (verified against the live API). Effects, however, are just
HTML/JS canvas files ("Lightscripts"). So instead of configuring an existing
effect, the pipeline **authors one**: it writes `Steam Theme.html`,
skinned with the current theme palette, into your effects folder, then
applies it over the API.

### Requirements & one-time setup

1. **SignalRGB installed and running** with its local API reachable
   (default `http://localhost:16038`). SignalRGB documents the local API as
   requiring **Pro** for most endpoints — if the API tests fail in the config
   UI, that's the first thing to check.
2. **Custom effects folder** — by default `Documents\WhirlwindFX\Effects`.
   If your Documents folder is redirected (e.g. OneDrive), the app finds the
   real path automatically; `signalrgb.effects_dir` overrides it.
3. **One restart of SignalRGB** after the first run, so it discovers the new
   `Steam Theme.html` file. (SignalRGB only scans the effects folder at
   launch.) After that, every game update is applied live — no restarts.
4. Enable **SignalRGB lighting sync** in the Extras section of the config UI.

You can verify discovery any time in SignalRGB: the effect appears under
Lighting Effects as “Steam Theme” (publisher: SteamTheme).

### Effect styles

Six built-in styles (`signalrgb.effect_style`, selectable in the UI), all
re-skinned per game from the extracted palette (accent + two dominant
colors):

| Style | Look |
|---|---|
| `gradient` | Palette gradient scrolling across devices; direction selectable (L→R, R→L, T→B, B→T) in the effect's own controls |
| `pulse` | Accent glow breathing outward from the center |
| `ripple` | Ambient expanding rings in palette colors on a dark base |
| `rain` | Falling palette streaks with fading trails |
| `comet` | Bright accent bar sweeping with a palette trail |
| `solid` | Flat accent color, optional breathing |

Each generated effect exposes **Accent / Palette 2 / Palette 3** color
pickers (plus style controls like speed) in SignalRGB's Customize page —
you can tweak them live, and they'll be re-skinned when the next game's
theme applies.

### Fallback effect

If the custom effect can't be applied (e.g. first run before the discovery
restart), the pipeline falls back to the stock effect named in
`signalrgb.fallback_effect` (default `"Solid Color"`). Set
`custom_effect: false` to skip generation entirely and always apply that
named effect instead — useful if you'd rather keep a stock Pro effect (say,
an audio visualizer) than the generated one.

### Keypress-reactive (keytap) layer

Every generated style includes a **keypress layer**: SignalRGB calls a
lightscript's global `onCanvasTapped(x, y)` function — coordinates in the
320×200 canvas space, mapped from the physical key position — whenever a key
is pressed on a key-mapped device. (This is the same hook SignalRGB's own
first-party effects like *Neon Sunset* and *Ripples* use.) Our effects answer
with palette-colored rings radiating from the pressed key — a radial flash
for the `solid` style — layered on top of the ambient animation. Toggle it
per effect on SignalRGB's Customize page with the **Keypress Effects**
switch; the `ripple` style also has an **Ambient ripples** switch if you
want keypress rings only.

Requirements:

- **SignalRGB Pro** — keytap input is a Pro feature. Without Pro the ambient
  animation still plays; the keypress layer just never fires.
- A **keyboard or keypad** device with an LED layout. Strips, fans, etc. have
  no key positions, so they simply keep playing the ambient part.

If you'd rather use a stock Pro keytap effect instead, set
`custom_effect: false` and `fallback_effect: "Ripple"` — you keep
reactivity, at the cost of palette skinning.

## Gotchas

- `.theme` files reference wallpapers by **absolute path** — `cache/` must not
  be cleaned blindly or applied themes break.
- Non-Steam games are detected by **process polling** every
  `poll_interval_seconds` — expect up to one interval of delay, and rename-
  proofing is on you (the exe name must match).
- `exclude_appids` defaults to `[431960]` (Wallpaper Engine — not a game).
- **Development:** `python -m unittest discover -s tests -v` runs the test
  suite (path sandboxing, web UI guards, effect rendering, INI injection).
  CI runs the same on every push via GitHub Actions.
- SignalRGB's REST API can only apply existing effects — parameters are
  read-only over HTTP. So the pipeline generates a custom palette-gradient
  effect (`Steam Theme.html`) into `Documents\WhirlwindFX\Effects` and
  applies it by name. One SignalRGB restart is needed the first time so it
  discovers the file; afterwards every game updates it live. Set
  `signalrgb.custom_effect: false` to always use the named
  `signalrgb.fallback_effect` instead. `signalrgb.effects_dir` overrides the
  target folder if needed (OneDrive-redirected Documents folders are handled
  automatically).
