# Steam Theme — Architecture & Handoff

This document explains how the app works, maps every module, and records the
decisions and recent changes so another engineer (or another agent in a
different harness) can pick up and keep improving it with minimal ramp-up.

Current version: **1.11.0**

---

## 1. What the app does

**Steam Theme** turns your last-played Steam game into a full desktop theme,
automatically. When you launch (or finish) a game it:

1. Detects the current game (Steam "now playing" / "last played", or a custom
   watched non-Steam process).
2. Fetches key art — a set of **hero** images, a **logo**, and an **icon** —
   from SteamGridDB / the Steam CDN / wallhaven.
3. Extracts a 16-color **palette** (optionally named + mood-classified by a
   local VLM), picks a WCAG-AA-compliant accent, and decides dark vs light.
4. Composes a per-monitor wallpaper (hero on the biggest display, logo on the
   second, icon on the third), writes a Windows `.theme` / KDE manifest, and
   applies it together with the accent + dark/light mode.
5. Optionally: **upscales** undersized art (Topaz / ComfyUI / images API),
   skins the **Windows Terminal** / **Konsole** color scheme, pushes a
   **SignalRGB** / **OpenRGB** lighting effect, and generates a **looping
   video** animated wallpaper via ComfyUI + Wallpaper Engine.

It runs as a background **polling service** with a **browser config UI** at
`http://127.0.0.1:8765/`.

---

## 2. Repository layout

```
main.py            entry point + service loop + the 4-stage pipeline
steamdetect.py     Stage 1 — detect current game (Steam API / running process)
artwork.py         Stage 2 — fetch + cache hero/logo/icon art
palette.py         Stage 3 — palette extraction + VLM naming
wallpaper.py       shared, platform-agnostic compositing (PIL only)
theme.py           Stage 4 (WINDOWS) — registry colors, .theme file, apply
linux_theme.py     Stage 4 (LINUX) — KDE Plasma 6 colors, manifest, apply
upscale.py         optional upscaling (Topaz / ComfyUI / images API + queue)
livewall.py        optional looping-video wallpaper (ComfyUI -> Wallpaper Engine)
extras.py          terminal + RGB dispatch, SignalRGB effect writer, Konsole
srgb_effects.py    SignalRGB Lightscript HTML effect templates (canvas JS)
openrgb_sync.py    Linux OpenRGB SDK lighting
webui.py           local HTTP config server + API
webui.html         single-page config UI (left sidebar + tabs)

config.example.json  committed example config (real config.json is gitignored)
install_task.ps1     Windows scheduled-task installer (3 tasks)
install_service.sh   Linux (systemd user service) installer
requirements.txt     runtime deps

tests/             stdlib unittest suite (no extra deps)
.github/workflows/ci.yml   CI matrix (Windows + Linux)
```

Secrets & local state are **not** committed (`.gitignore` covers
`config.json`, `state.json`, `service.log`, `*_backup.json`, `cache/`).

---

## 3. The pipeline (the mental model)

The whole app revolves around one function: **`main.run_pipeline(cfg, game, log)`**,
which runs the four stages in order. Every feature (`--once`, `--appid`,
Ultimate Fetch, Regenerate All, gallery re-apply) reuses this pipeline or a
thin slice of it.

```
steamdetect.get_running/owned_games  ->  {appid, name, ...}
        │
artwork.fetch_artwork  ->  {hero, hero_candidates[], logo, icon}   (cached per game)
        │
palette.build_palette  ->  {colors[16], accent, appearance, background, ...}
   (+ palette.ai_theme_naming ->  {theme_name, mood, ...extra VLM keys})
        │
upscale_toward_monitors  (optional, mutates art paths to upscaled copies)
        │
wallpaper.compose_wallpaper  ->  span/tiles image
theme.write_theme_file + apply_theme + write_registry_colors
extras.apply_terminal / apply_rgb
livewall.maybe_generate  (queues a video, never blocks)
```

`check_once` (the poll body) wraps `run_pipeline` and **de-duplicates**: it
only re-themes when the detected game's *identity* (cache key) changed, unless
forced.

### Key design decisions

- **One theme per game.** Detected games map to a stable `cache_key`
  (appid, or `custom_<slug>` for non-Steam). Replaying a game never re-themes.
- **Everything is cached & idempotent.** Art lives in `cache/<key>/`; upscales
  are `<role>_upscaled_<model>.png` (provider/model in the filename); videos are
  the hero-art hash. Re-running any bulk pass only does *missing* work.
- **Bulk passes never block on slow work.** Regenerate All / Ultimate Fetch
  *queue* upscales & videos for background workers rather than rendering inline.

---

## 4. Module-by-module

### `main.py`
- `__version__`, `safe_join` (path-traversal guard), `load_config` /
  `save_config` / `cfg_path` (all path-like settings forced inside `BASE_DIR`).
- `run_pipeline` (the pipeline), `check_once` (detection + hold + de-dupe),
  `reapply`, `reapply_from_cache` (gallery), `fetch_library` (Ultimate Fetch),
  `regenerate_cache` (Regenerate All), `upscale_pending_estimate`.
- The `main()` service loop: live-reloads config each cycle, drains the Comfy
  upscale queue, kicks the live-wallpaper drain worker, runs `check_once`,
  sleeps `poll_interval_seconds`.

### `steamdetect.py` (Stage 1)
- `get_owned_games` (Steam Web API `IPlayerService/GetOwnedGames`),
  `get_last_played_game`, `get_running_appid` (now-playing via registry /
  `/proc`), `get_steam_id64` (registry / `loginusers.vdf`).
- `find_running_custom_game` matches configured process names (tasklist on
  Windows, `/proc/*/comm|cmdline` on Linux — the argv[0] basename covers
  Wine/Proton wrappers).

### `artwork.py` (Stage 2)
- Fetch chain per role with graceful fallbacks. `SteamGridDB` (keyed) ->
  Steam CDN -> wallhaven (keyless-ish).
- `_normalize_image` fixes lying formats/extensions (ICO/WebP/… -> JPEG/PNG)
  so the Topaz API never 415s. `_big_enough` rejects art < 512px.
- `hero_candidates` / `active_hero` / `hero_choice` / `set_hero_choice`
  manage the multi-hero set + the user's per-game pick (`hero_choice.json`).

### `palette.py` (Stage 3)
- Median-cut 16-color palette; `decide_mode` (dark/light from luminance);
  `pick_accent` nudges lightness to WCAG AA; `_rel_luminance` / `contrast`.
- `ai_theme_naming` calls an OpenAI-compatible VLM (LM Studio / Ollama /
  OpenRouter) with an image + prompt, parses a JSON object back. Self-gates on
  `ai.enabled`/`base_url`/`model`/`use_for.theme_naming`; returns `None` cleanly.

### `wallpaper.py` (compositing, shared)
- `assign_roles` (largest=hero, then logo, then icon; config overrides).
- `compose_wallpaper` (Windows: one span image) + `compose_monitor_tile`
  (hero "covers", logo/icon get a blurred-hero-backdrop "centered" treatment).
- `_ini_safe` sanitizes anything heading into an INI file (theme names can come
  from a VLM, so they're treated as untrusted).

### `theme.py` (Windows) / `linux_theme.py` (KDE Plasma 6)
- Both export the same interface: `enumerate_monitors / assign_roles /
  compose_wallpaper / write_theme_file / apply_theme /
  write_registry_colors / snapshot_windows_look / restore_windows_look`.
- Windows: registry accent + dark/light, a `.theme` INI (with a `[Slideshow]`
  section for hero rotation), `os.startfile` to apply, and a `windows_backup.json`
  snapshot for "Restore Windows look".
- Linux: `kscreen-doctor`/`kscreen-doctor` monitor JSON, per-screen tiles pushed
  via `plasmashell … evaluateScript` (D-Bus), `plasma-apply-colorscheme`
  (scheme and accent applied by **separate** invocations — a known Plasma quirk),
  and a JSON manifest (`theme.json`) + `slideshow.json`.

### `upscale.py`
- Providers: `topaz` (cloud, credits), `comfy` (local Export-API workflow),
  `ai` (OpenAI-compatible `/images/edits`, best-effort).
- `cache_path` encodes provider/model so switching models re-generates;
  `find_existing_upscale` (glob `<base>_upscaled_*.png`) makes the "already
  upscaled" check **provider-agnostic**.
- A **background queue** (`comfy_queue.json`) drains deferred Comfy jobs while
  no game runs; `_unload_comfy_models` calls ComfyUI's `/free` after each job.
- Prompts/workflows interpolate `{placeholders}` from the VLM context
  (`fill_prompt` / `fill_workflow`) — unknown placeholders are dropped + logged.

### `livewall.py`
- Generates a looping video from the **original** hero art (not the upscale)
  via ComfyUI; installs it as a Wallpaper Engine project (`publish_to_we`),
  applies it live via WE's `-control` or queues it in WE's `config.json`.
- Accepts both the classic Export-API JSON **and** the new ComfyUI graph/save
  format (`graph_to_api`, incl. subgraph flattening), and auto-repairs unquoted
  `{placeholder}` tokens (`repair_json`).
- Videos are expensive, so `maybe_generate` only *queues* (`video_queue.json`);
  a single-flight `start_drain` worker renders one at a time, gated on gaming +
  ComfyUI availability.

### `extras.py` / `srgb_effects.py` / `openrgb_sync.py`
- Terminal: Windows Terminal (contrast-guaranteed `readable_terminal_colors`)
  or Konsole `.colorscheme`.
- RGB: SignalRGB (Windows — writes a palette-skinned Lightscript HTML effect
  into the WhirlwindFX Effects folder, then applies it by name over the REST
  API) or OpenRGB SDK (Linux — per-LED Direct/Static mode).

### `webui.py` + `webui.html`
- A `ThreadingHTTPServer` bound to `127.0.0.1`. The config page is a single
  `webui.html` with a **left sidebar + one tab per section** (Overview, General,
  Non-Steam games, Steam, Artwork sources, AI / VLM, Upscaling, Live wallpaper,
  Extras, Monitors, Library, Gallery, Activity log).
- All endpoints are under `/api/…`. Long jobs run in daemon threads; progress
  polls `/api/…-status`.

---

## 5. Configuration surface

`config.json` (created from `config.example.json` on first save). Notable areas:

- `steam_api_key`, `steam_id64`, `steamgriddb_api_key`, `wallhaven_api_key`
- `poll_interval_seconds`, `now_playing`, `custom_games[]`, `exclude_appids[]`
- `palette_mode`, `appearance_preference` (dark/light/auto), `system_mode`,
  `app_mode`, `hero.{count,mode,interval_minutes,shuffle}`
- `ai.{enabled,base_url,api_key,model,use_for,prompt}` (the VLM)
- `upscaling.{enabled,provider}`, `topaz.{api_key,model,creativity,prompt,models}`
- `comfy.{host,port,workflow,workflows{hero,logo,icon},defer_while_gaming,unload_models_after,timeout_seconds}`
- `livewall.{enabled,provider,host,port,workflow,all_heroes,topaz_finish,defer_while_gaming,timeout_seconds,we_monitor}`
- `signalrgb.*`, `windows_terminal.*` (Windows) / `konsole.*`, `openrgb.*` (Linux)

Path-like settings (`cache_dir`, `state_file`, `log_file`) are forced inside the
app directory by `save_config` + `cfg_path` / `safe_join`.

---

## 6. External integrations (what must be configured/live)

| System | Purpose | Notes |
| --- | --- | --- |
| Steam Web API | list owned games + last-played | free key; `steam_id64` auto-detected |
| SteamGridDB | proper heroes/logos/icons | free key; keyless CDN fallback exists |
| wallhaven | hero fallback | optional API key |
| Topaz Gigapixel | cloud upscale | `account.topazlabs.com` API key; per-image credits |
| ComfyUI (local) | upscale + video | Export-API (Dev Mode); host/port; auto-unload |
| OpenAI-compatible VLM | naming + mood | LM Studio / Ollama / OpenRouter |
| SignalRGB (Win) | lighting | local REST API; generated effect file |
| OpenRGB (Linux) | lighting | `openrgb --server` SDK |
| Windows Terminal / Konsole | terminal colors | writes settings / `.colorscheme` |
| Wallpaper Engine (Win) | animated wallpaper | `-control` / `config.json` |

---

## 7. Security model

- The web server binds **127.0.0.1 only** and enforces a **DNS-rebinding +
  CSRF guard** (`_local_only`: hostname allow-list, exact-Origin match,
  `Sec-Fetch-Site` rejection). It holds every API key, so this is load-bearing.
- **Path traversal**: `safe_join`/`cfg_path` confine every path-derived read.
- **INI/JS injection**: `wallpaper._ini_safe` (theme names, from a VLM, are
  untrusted), `srgb_effects._safe_color` (palette colors are `#RRGGBB`-regex
  checked before being interpolated into a JS file), Linux D-Bus scripts build
  paths through `json.dumps` (no break-out of the string literal).
- **No shell**: every `subprocess` call uses an arg list (no `shell=True`), so
  there is no command-injection surface.
- **Secrets stay out of git**: `config.json`/`state.json`/`service.log`/
  `*_backup.json` are gitignored; only `config.example.json` is committed.

---

## 8. Testing

- `python -m unittest discover -s tests` (stdlib only; ~242 tests).
- Covers path sandboxing, palette math, Steam parsing, art caching, the
  regenerate/upscale passes, live-wallpaper graph conversion, and the web UI
  (incl. the local-only guard).
- CI runs the matrix **Windows + Linux** (`python-version: 3.13`), does a
  `py_compile` of every module, then the unit tests.

> **Known flaky test:** `tests/test_webui.py::TestLocalOnlyGuard.test_cross_port_localhost_rejected`
> intermittently raises `ConnectionAbortedError` (`WinError 10053`) on Windows
> when run in the full suite (passes in isolation). It's a `ThreadingHTTPServer`
> socket-teardown race, not a product bug. Re-run in isolation if CI flakes.

---

## 9. How to run locally

```bash
# install deps
pip install -r requirements.txt

# open the config UI in a browser (also starts the server)
python main.py --ui

# one-shot checks
python main.py --once          # check once, apply if the game changed
python main.py --force         # apply regardless
python main.py --appid 440     # theme a specific game
python main.py --dry-run       # full pipeline, no writes
python main.py --reapply       # re-apply the cached theme

# bulk
python main.py --fetch-all --upscale   # pre-cache the whole library
```

On Windows the installer (`install_task.ps1`, run as Administrator for the
unlock trigger) registers three scheduled tasks: `SteamTheme` (polling service),
`SteamTheme-Unlock` (re-apply on session unlock), `SteamThemeUI` (config UI on
logon). Start/stop with `schtasks /Run|/End /TN <TaskName>`.

---

## 10. Known limitations / future work

- **Windows Terminal settings.json is JSONC** (comments/trailing commas);
  `apply_windows_terminal` uses strict `json.load` and skips gracefully rather
  than risk corrupting the file. A JSONC-tolerant parse would widen real-world
  compatibility.
- **Video generation is inherently slow** (minutes-to-an-hour per clip); the
  queue/drain worker keeps the poller responsive but renders are still serial.
- **VLM naming** needs an OpenAI-compatible endpoint with vision; it's optional
  and the app degrades gracefully to a generic name when unconfigured.
- **KDE apply** depends on `qdbus`/`kscreen-doctor`/`plasma-apply-colorscheme`
  being reachable from the session environment (`_session_env` fills in the
  standard paths for SSH/systemd).
- Direct pushes to `main` bypass the branch-protection ruleset (intentional for
  this project so far — flagged, not changed).

---

## 11. What the last pass changed

A systematic bug-hunt / hardening pass (committed on top of the tabbed-UI work):

- **Fixed unclosed file handles** (ResourceWarnings + Windows file-lock risk)
  in `upscale._topaz`, `upscale._ai`, and the `wallpaper` compositor.
- **Hardened `openrgb_sync`** port parsing (unguarded `int()` could crash best-
  effort lighting on a malformed config).
- **Clamped the poll interval** in the `main()` loop so a non-numeric or absurd
  `poll_interval_seconds` can never crash/starve the service.
- **Guarded `restore_windows_look`** (both platforms) against a missing backup
  file, logging instead of raising.
- **Closed a CI gap**: added `livewall.py` to the `py_compile` step.
- **Bumped version** to `1.11.0`.

No behavior changed; all fixes are defensive.
