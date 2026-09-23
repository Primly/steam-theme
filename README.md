# 🎮 Steam Theme

**Turns your last-played Steam game into a full desktop theme — automatically.**

Every time you play something new, Steam Theme turns the game's key art into a
matching desktop: wallpaper on **every monitor**, your accent color, dark/light
mode, a terminal color scheme, and RGB lighting — all derived from the game's
artwork and named by AI. Play a game, and your whole PC reskins itself.

[![CI](https://github.com/Primly/steam-theme/actions/workflows/ci.yml/badge.svg)](https://github.com/Primly/steam-theme/actions/workflows/ci.yml)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%28KDE%20Plasma%206%29-blue)](https://github.com/Primly/steam-theme)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776ab)](https://www.python.org/)
[![Version](https://img.shields.io/badge/version-1.2.0-66c0f4)](https://github.com/Primly/steam-theme/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

---

## ✨ What it does

- **Detects** what you played — a Steam game running *right now*, your
  last-played Steam game, or a watched non-Steam process (Epic, GOG,
  emulators).
- **Fetches** proper key art — hero, logo and icon images (SteamGridDB →
  Steam CDN → wallhaven), cached per game so nothing is downloaded twice.
- **Extracts a palette** — 16 colors, a WCAG-AA-checked accent, and a
  dark/light decision from the artwork itself.
- **Names the theme** with an AI/VLM of your choice (local LM Studio/Ollama
  model or any cloud endpoint) — e.g. *“Autumn Tempest”* for Ghost of Tsushima.
- **Applies it everywhere:**
  - per-monitor wallpapers (hero on the big screen, logo & icon on the others)
  - system accent color + dark/light mode
  - Windows Terminal / Konsole color scheme
  - RGB lighting: SignalRGB (Windows) or OpenRGB (Linux)
- **Runs itself** in the background and re-themes whenever you play a new game.

## 🖥️ Supported platforms

| Capability | Windows 10 / 11 | Linux · KDE Plasma 6 (Wayland) |
|---|---|---|
| Wallpaper (per monitor) | ✅ one span image | ✅ one exact-sized tile per screen |
| Accent + dark/light | ✅ registry | ✅ BreezeDark/Light + accent |
| Theme detection | ✅ registry + Steam API | ✅ /proc + Steam API |
| Terminal scheme | ✅ Windows Terminal | ✅ Konsole |
| RGB lighting | ✅ SignalRGB | ✅ OpenRGB |
| Autostart | ✅ scheduled tasks | ✅ systemd user services |
| Undo (restore) | ✅ | ✅ |

Developed and tested on Windows 11 and [Bazzite](https://bazzite.gg) 44
(Plasma 6.7, Wayland). Other Plasma 6 distros should work as-is — the
Troubleshooting section near the end covers the few platform quirks.

## 📋 What you'll need

- **Python 3.10 or newer** (Bazzite ships it)
- A **Steam account** and a free **Steam Web API key** —
  <https://steamcommunity.com/dev/apikey>
- Optional, for the best experience:
  - a free **SteamGridDB** key (much better art) —
    <https://www.steamgriddb.com/profile/preferences/api>
  - an **OpenAI-compatible VLM** (LM Studio, Ollama, OpenRouter, …) for theme
    naming — or skip it and get generated names
  - a **Topaz Gigapixel** API key (paid, per-image credits) for upscaling
  - **SignalRGB Pro** (Windows) or **OpenRGB** (Linux) for lighting

No git or programming knowledge is required — the install below is
click-by-click.

---

## 🚀 Installation — Windows

Open **PowerShell** (Start menu → type `powershell` → Enter). All commands go
there, one at a time, pressing Enter after each.

### 1. Install Python

1. Download Python 3.10+ from <https://www.python.org/downloads/>.
2. Run the installer. **Tick the “Add python.exe to PATH” checkbox on the
   first screen** — this is required. Click Install.
3. Close and reopen PowerShell, then verify:

   ```powershell
   python --version
   ```

   You should see something like `Python 3.13.5`. If you get an error, the
   PATH checkbox was missed — re-run the installer and tick it.

### 2. Get the app

**Option A — with git (recommended, updates are one command):**

1. Install Git from <https://git-scm.com/download/win> — the defaults are
   fine, keep clicking Next.
2. In PowerShell:

   ```powershell
   cd $env:USERPROFILE\Documents
   git clone https://github.com/Primly/steam-theme.git
   cd steam-theme
   ```

   `git clone` downloads the app into a new folder
   `Documents\steam-theme` — you never need to touch git again except for
   updates.

**Option B — without git:**

1. Open <https://github.com/Primly/steam-theme> in your browser → green
   **Code** button → **Download ZIP**.
2. Extract the ZIP (right-click → Extract All), then:

   ```powershell
   cd $env:USERPROFILE\Downloads\steam-theme-main
   ```

### 3. Install the app's Python packages

From the project folder:

```powershell
pip install -r requirements.txt
```

### 4. First run

```powershell
python main.py --once --dry-run   # full pipeline, changes nothing
python main.py --once             # the real thing: your desktop gets themed
```

### 5. Configure it in your browser

```powershell
python main.py --ui
```

opens the settings page at <http://127.0.0.1:8765> — everything is
point-and-click with test buttons. At minimum:

1. **Steam** — paste your Web API key, click **Auto-detect** for the
   SteamID64, then **Test connection**.
2. **Artwork sources** — a free SteamGridDB key gives you much better art.

Everything else (AI, upscaling, lighting, monitors) is optional and
documented inline in the UI. Saving creates `config.json` — your keys never
leave your machine.

### 6. Make it run itself

```powershell
powershell -ExecutionPolicy Bypass -File install_task.ps1
```

Registers the background service + config page to start at logon. It also
creates a *unlock* task (when run from an elevated shell) that fixes the
half-applied theme Windows can leave when a theme is applied while the
screen is locked.

## 🚀 Installation — Linux (Bazzite & other KDE distros)

Open a terminal (Konsole, or whatever your distro ships).

1. **Bazzite** already ships Python 3 with the needed libraries. On other
   distros install Python 3.10+ and, on immutable/atomic distros, prefer a
   venv (`python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`).
2. Get the app:

   ```bash
   git clone https://github.com/Primly/steam-theme.git
   cd steam-theme
   pip install -r requirements.txt   # no-op on Bazzite except the OpenRGB helper
   ```

3. Try it, then configure in a browser (same UI as Windows):

   ```bash
   python3 main.py --once --dry-run
   python3 main.py --ui
   ```

4. Autostart as systemd **user** services (no root):

   ```bash
   bash install_service.sh
   ```

**RGB lighting on Linux:** install OpenRGB (`ujust openrgb install` on
Bazzite), then run `openrgb --server` (add it to autostart). Enable
*OpenRGB lighting sync* in the config UI.

**Monitor matching on Linux:** matches are against connector names
(`HDMI-A-1`, `eDP-1`, `DP-1`, …) shown in the UI's Monitors section — the
Linux desktop doesn't expose monitor model names to apps.

## 🔄 Updating

- **With git:** open the project folder in your terminal and run
  `git pull`, then restart the service if it's running
  (Windows: `schtasks /End /TN SteamTheme & schtasks /Run /TN SteamTheme` ·
  Linux: `systemctl --user restart steam-theme.service`).
- **Without git:** download a fresh ZIP and replace the folder — keep your
  `config.json` (and `cache/` if you want to keep downloaded art).

## 🗑️ Uninstalling

1. In the config UI press **Restore** to put back your pre-app desktop look.
2. Remove the autostart tasks:
   - Windows: `Unregister-ScheduledTask -TaskName SteamTheme, SteamTheme-Unlock, SteamThemeUI -Confirm:$false`
   - Linux: `bash install_service.sh remove`
3. Delete the project folder. Nothing else was installed on your system.

---

## 🧠 How it works

The pipeline runs every poll (default 30 s) and only acts when the detected
game changes:

```
detect ──▶ fetch art ──▶ palette ──▶ (AI naming) ──▶ (upscale) ──▶ compose ──▶ apply ──▶ extras
```

1. **Detect** — priority order:
   1. a watched **non-Steam process** that is running (details in the
      non-Steam games section below),
   2. a **Steam game running right now** (optional *now playing* toggle —
      themes the moment a game launches),
   3. otherwise the **last-played** game from the Steam Web API
      (`GetOwnedGames`, sorted by last-play timestamp — more reliable than
      the recently-played endpoint).
   Your SteamID64 is auto-detected (registry on Windows, Steam's
   `loginusers.vdf` on Linux).
2. **Artwork** — per role (hero / logo / icon), with fallbacks:
   SteamGridDB → Steam CDN (`library_hero.jpg`, `logo.png`, `header.jpg`)
   → wallhaven search. Downloaded once per game, cached in `cache/`.
3. **Palette** — 16-color median cut (Colorful / Material / Muted styles),
   an accent checked for WCAG contrast, and a dark/light decision from the
   art's luminance. You can pin everything to dark (default), light, or auto.
4. **AI naming (optional)** — any OpenAI-compatible chat endpoint names the
   theme and describes its mood; the mood also steers generative upscaling.
   Failed/skipped naming falls back to `"<Game> — Steam Theme"`.
5. **Upscaling (optional)** — art smaller than its target monitor is
   upscaled via Topaz Gigapixel (details in the upscaling section below).
6. **Compose + apply** — Windows: one span-style image across all monitors,
   accent/dark-mode registry keys, and a generated `.theme` applied exactly
   as if you double-clicked it. Linux: per-screen exact-sized tiles pushed
   to Plasma over D-Bus, plus the KDE color scheme + accent.

### Monitor roles

Each display gets a role — **hero** (big screen), **logo**, **icon** — set in
the config UI's Monitors section by matching a substring of the display name
(Windows: model names like `LS49AG95`; Linux: connector names like
`HDMI-A-1`). Unmatched monitors fall back by size: largest → hero, then
logo, then icon.

Heroes are cover-cropped to fill their screen. Transparent logos and icons
get a **centered treatment**: the art sits on a darkened, blurred backdrop of
the hero, so wordmarks stay crisp instead of being zoomed and cropped.

## ⚙️ Configuration

`python main.py --ui` serves <http://127.0.0.1:8765> (localhost only):

| Section | What it controls |
|---|---|
| **General** | dark/light preference, palette style, poll interval, excluded app IDs, *now playing* toggle |
| **Non-Steam games** | process watch list (Epic/GOG/emulators) |
| **Steam** | API key, auto-detected SteamID64, live test |
| **Artwork sources** | SteamGridDB / wallhaven keys, live test |
| **AI / VLM** | OpenAI-compatible endpoint, presets, model dropdown, live test |
| **Upscaling** | Topaz key, model picker (Precision/Generative), per-role models, creativity, prompt |
| **Extras** | Windows Terminal (Windows) / Konsole (Linux), SignalRGB (Windows) / OpenRGB (Linux) |
| **Monitors** | detected displays → role mapping |
| **Theme gallery** | thumbnails of every cached theme; re-apply instantly |
| **Activity log** | live tail of what the service is doing |

The background service **re-reads config.json every cycle** — saving in the
UI takes effect without restarts.

<details>
<summary><strong>config.json reference</strong> (all keys, with defaults)</summary>

| Key | Default | Meaning |
|---|---|---|
| `steam_api_key` | — | Steam Web API key (required) |
| `steam_id64` | auto | Your SteamID64 (auto-detected when empty) |
| `steamgriddb_api_key` | `""` | Better artwork from SteamGridDB |
| `wallhaven_api_key` | `""` | Optional wallhaven fallback key |
| `poll_interval_seconds` | `30` | How often to check what's playing |
| `now_playing` | `true` | Theme a game the moment it launches |
| `custom_games` | `[]` | Non-Steam process watch list |
| `exclude_appids` | `[431960]` | Never theme these (Wallpaper Engine etc.) |
| `appearance_preference` | `"dark"` | `dark` / `light` / `auto` (from art luminance) |
| `system_mode` / `app_mode` | `"match"` | `match` the theme, or force `dark`/`light` |
| `palette_mode` | `"colorful"` | `colorful` / `material` / `muted` |
| `ai.enabled` / `base_url` / `api_key` / `model` | `false` | OpenAI-compatible VLM for theme naming |
| `upscaling.enabled` / `provider` | `false` | `topaz` or `ai` (OpenAI images endpoint) |
| `topaz.api_key` / `model` / `creativity` / `prompt` / `models` | — | Topaz settings; `models.hero/logo/icon` override per role |
| `signalrgb.*` (Windows) | — | `enabled`, `base_url`, `custom_effect`, `effect_style`, `effect_scope` (`per_game`/`single`), `effects_dir`, `fallback_effect` |
| `windows_terminal.*` (Windows) | — | `enabled`, `set_background_image`, `background_opacity` |
| `konsole.*` (Linux) | — | `enabled`, `set_default_profile` |
| `openrgb.*` (Linux) | — | `enabled`, `host`, `port` |
| `monitors` | by size | `[{ "match": "…", "role": "hero" }]` |
| `cache_dir` / `state_file` / `log_file` | `cache` / `state.json` / `service.log` | Paths (must stay inside the app folder) |

</details>

## 🖼️ Theme gallery & manual holds

Every game you've themed is in the gallery with a thumbnail. **Applying one
re-applies it instantly** — no API calls, no upscaling, no credits.

Applying a gallery theme sets a **manual hold**: the background service keeps
your choice instead of reverting to the last-played game on its next poll.
The hold releases automatically the moment you actually play something, and
the UI shows a banner with a **Resume auto-theming now** button. The
Re-apply / Regenerate / Full refetch buttons also respect the hold — they
re-run the theme you're looking at, not whatever Steam happens to report.

## 🎨 AI / VLM theme naming

Point the AI section at any OpenAI-compatible chat endpoint — local (LM
Studio, Ollama, vLLM) or cloud (OpenRouter, etc.). The model sees the hero
art and returns a theme name, a mood description, and (in auto mode) a
dark/light + palette-style opinion. The mood also feeds generative Topaz
models. Everything is optional — no AI configured just means
`"<Game> — Steam Theme"` names.

## 🔎 Upscaling with Topaz Gigapixel

When artwork is smaller than the monitor it must fill, it can be upscaled
through the [Topaz Labs](https://www.topazlabs.com/) image API (paid,
per-image credits — the **Test** button verifies your key *without*
spending any):

- **Precision models** (`Standard V2`, `High Fidelity V2`, `Low Resolution
  V2`, `Text Refine`, `CGI`) — faithful enlargement, ~1 credit per 24 MP.
- **Generative models** (`Bloom 2`, `Wonder 3`, `Redefine`, `Bloom
  Realism`, `Recovery V2`, `Standard MAX`) — creative re-interpretation,
  ~1 credit per 2 MP. Steerable with a **creativity** slider and a
  `{game}` / `{mood}` prompt template.
- **Per-role models**: e.g. Bloom 2 for heroes, Text Refine for logos/icons.

Results are cached per model — a cached upscale is never re-billed. The
*Full refetch* button in the UI deletes the current game's cached art and
upscales (re-generates) them, which **does** spend credits. Alternatively,
`provider: "ai"` uses an OpenAI-compatible images endpoint, best-effort.

## 🌈 RGB lighting

### SignalRGB (Windows)

**What you get:** every themed game switches your lighting to an effect
**generated from that game's palette**. In the default *per-game* scope each
game gets its own effect — *“Steam Theme - The Witcher 3”*, *“Steam Theme -
ELDEN RING”*, … — which you can tweak individually in SignalRGB's Customize
page; each game's file is only rewritten when that game re-themes, so your
tweaks survive. Prefer one shared effect? Set **Effect scope → single**.

**How it works (and why it's set up this way):**

- SignalRGB's local REST API can *apply* effects and *read* their settings,
  but **cannot change an effect's colors** — parameter writes are silently
  ignored. Effects, however, are just HTML/JS canvas files ("Lightscripts").
  So the pipeline **authors** one per game: `Steam Theme - <Game>.html`,
  skinned with that game's palette, written into your Effects folder, then
  applied by name over the API.
- **SignalRGB only discovers new files at launch.** So the pipeline also
  refreshes a shared `Steam Theme.html` mirror every run — that file was
  discovered long ago, so lighting still updates live the moment a new game
  is themed. After your next SignalRGB restart, the per-game files are
  discovered and applied directly. Net: no restarts needed for lighting to
  track games; a restart only unlocks the individually-named effects.

**Setup checklist:**

1. SignalRGB installed and running, local API reachable (default
   `http://localhost:16038`). Most API endpoints need **SignalRGB Pro** — if
   tests fail in the config UI, check that first.
2. Effects folder: `Documents\WhirlwindFX\Effects` by default —
   OneDrive-redirected Documents folders are detected automatically;
   `signalrgb.effects_dir` overrides.
3. **Restart SignalRGB once** after the first themed game, so it discovers
   the `Steam Theme.html` mirror. That's the only restart you ever need.
4. Enable **SignalRGB lighting sync** in Extras.

**Effect styles** (selectable per config, re-skinned per game from the
palette — accent + two dominant colors):

| Style | Look |
|---|---|
| `gradient` | Palette gradient sweeping across devices; direction selectable in the effect's own controls |
| `pulse` | Accent glow breathing outward from the center |
| `ripple` | Ambient expanding rings in palette colors |
| `rain` | Falling palette streaks with fading trails |
| `comet` | Bright accent bar sweeping with a palette trail |
| `solid` | Flat accent color, optional breathing |

Every generated effect includes a **keypress layer** — rings radiate from
the pressed key (the same `onCanvasTapped` hook SignalRGB's own keytap
effects use), toggleable per effect on the Customize page. Keypress input
needs **SignalRGB Pro** and a keyboard/keypad with an LED layout; strips and
fans just keep the ambient animation. Apply order is: per-game effect →
shared mirror → the stock effect named in `fallback_effect` (default
`"Solid Color"`). Set `custom_effect: false` to skip generation entirely and
always apply a named stock effect (e.g. `fallback_effect: "Ripple"` for a
stock Pro keytap effect). Accumulated per-game files live in your Effects
folder — delete any you don't want; they're only recreated if you replay
that game.

### OpenRGB (Linux)

SignalRGB doesn't exist on Linux; [OpenRGB](https://openrgb.org) is the
standard there. Its SDK server lets clients push per-LED colors:

1. `ujust openrgb install` on Bazzite (or your distro's OpenRGB package).
2. Run `openrgb --server` (add it to autostart).
3. Enable **OpenRGB lighting sync** in the config UI.

The palette is distributed across each device's LEDs (accent first) in its
Direct/Static mode. Best-effort: if the SDK server isn't running, the
pipeline logs it and moves on.

## ↩️ Restore & snapshots

The first time a theme is applied, your current wallpaper, accent color and
light/dark modes are snapshotted (`windows_backup.json` on Windows,
`desktop_backup.json` on Linux — gitignored, never overwritten). The
**Restore** button in the UI puts that snapshot back.

> [!NOTE]
> The snapshot is taken on *first apply*. If your desktop was already themed
> by Steam Theme before you looked at this, restore may look like "one of
> our themes". Fix the baseline: set Windows/KDE exactly how you want it,
> delete the backup file, and the next apply re-snapshots.

## 🎮 Non-Steam games (Epic, GOG, emulators)

Steam's API only sees Steam-launched games, so the app can watch processes
instead. Add one per line in the UI:

```
eldenring.exe | 1245620 | ELDEN RING
retroarch.exe |         | RetroArch
```

`process | appid (optional) | display name (optional)`. While the process
runs it wins over Steam detection; when it exits, theming returns to your
Steam game. An appid (from any Steam store page URL) gets proper Steam CDN +
SteamGridDB art; without one, art comes from a SteamGridDB name search with
a wallhaven fallback. Linux process names are matched against the executable
name (Wine/Proton `.exe` names work too).

## 🧰 CLI reference

```powershell
python main.py                # polling service (foreground, Ctrl+C to stop)
python main.py --once         # check once, theme if the game changed
python main.py --once --force # re-theme even if unchanged (uses cache)
python main.py --once --dry-run  # full pipeline, changes nothing
python main.py --appid 440    # theme a specific game by appid
python main.py --reapply      # re-apply the current cached theme
python main.py --ui           # browser config page
python main.py --ui --port 8800
```

## 📁 Files & data

Everything the app writes stays inside its own folder:

| Path | Contents |
|---|---|
| `config.json` | your settings + API keys (gitignored, never committed) |
| `cache/<appid>/` | per-game art, upscales, palette, theme file, wallpapers |
| `state.json` | last themed game, manual hold (gitignored) |
| `service.log` | what the service has been doing (gitignored) |
| `windows_backup.json` / `desktop_backup.json` | pre-app desktop snapshot |

## 🔒 Privacy & security

- **Your keys stay local.** `config.json` holds your Steam / SteamGridDB /
  Topaz / AI keys. It is gitignored and is never transmitted anywhere except
  to the service each key belongs to. Only the placeholder
  `config.example.json` is committed.
- **The config UI is localhost-only.** It binds to `127.0.0.1` and verifies
  the `Host`/`Origin` headers on every request — this blocks DNS-rebinding
  and cross-site (CSRF) requests from web pages, including pages served by
  *other localhost apps* (dev servers, LM Studio, SignalRGB, …), since
  `Origin` must match the server's own origin exactly.
- **Config-supplied paths are sandboxed.** `cache_dir`, `state_file` and
  `log_file` must resolve inside the app folder, so a hostile config can't
  turn the log viewer into a file reader or the refetch button into a
  directory deleter.
- **Machine-generated content is untrusted by default.** VLM theme names are
  control-character-stripped before entering INI-like files, palette colors
  are validated as `#RRGGBB` before being baked into anything SignalRGB
  executes, and the UI renders all server strings as text (never HTML).
- **Supply chain:** Dependabot watches the pip requirements and GitHub
  Actions workflows; CI runs the full test suite (including the guard tests
  above) on every push and pull request.

## 🧪 Development

```bash
python -m unittest discover -s tests -v   # 65 tests
```

- `wallpaper.py` — shared, platform-agnostic compositing
- `theme.py` / `linux_theme.py` — per-platform backends (same interface)
- `main.py` — orchestration; picks the backend via `sys.platform`
- `steamdetect.py` — detection (Steam API + per-platform now-playing)
- `artwork.py`, `palette.py`, `upscale.py` — fetch → palette → upscale
- `extras.py` (+ `srgb_effects.py`, `openrgb_sync.py`) — terminal + RGB
- `webui.py` / `webui.html` — config server + UI

The suite runs on **both Windows and Linux** — CI runs it on
`windows-latest` and `ubuntu-latest` for every push and PR. The `main`
branch is protected; outside changes arrive via pull requests and must pass
CI. Platform-specific tests skip cleanly on the other OS.

## 🩺 Troubleshooting

| Symptom | Fix |
|---|---|
| `config.json not found` on first run | Normal on a fresh checkout — run `python main.py --ui` and save once |
| Steam test says "no player returned" | Wrong API key or SteamID64; re-check both in the Steam section |
| Nothing themes even though the key works | The game must be *your* last-played (play it for a minute) or running; check the Activity log in the UI |
| UI won't open at 127.0.0.1:8765 | Something else uses the port: `python main.py --ui --port 8800` |
| Wallpaper didn't change | `cache/` was deleted — use **Full refetch** in the UI (note: re-runs upscaling, may use Topaz credits) |
| Windows theme looks "half applied" after unlock | Expected occasionally; the unlock task re-applies it. Re-run `python main.py --reapply` |
| SignalRGB lighting never changes | SignalRGB must be running with local API (Pro). Restart it once after the first theme so it discovers the effect files |
| SignalRGB test fails | Pro subscription, or check the API URL in Extras |
| OpenRGB "SDK server not reachable" | Run `openrgb --server` (and autostart it); check host/port in Extras |
| Topaz test/upscale fails | Key invalid or out of credits — the Test button only verifies the key |
| Linux: monitor names don't match | Use connector names (`HDMI-A-1`) — the Monitors section shows exactly what to match |
| Linux: `xrdb: Can't open display` in logs | Harmless Wayland-session noise from KDE tools; can be ignored |
| Linux: nothing happens in Game Mode | Theming targets the Plasma desktop session — the theme applies when you return to the desktop; detection still runs |
| Restore looks like a Steam Theme | The snapshot was taken after theming began — see the note in the Restore & snapshots section |

## 📜 Changelog

- **1.2.0** — Linux support (KDE Plasma 6 / Bazzite): per-screen wallpapers,
  Breeze dark/light + accent, Konsole, OpenRGB, systemd user services,
  cross-platform CI. Platform dispatch in the UI.
- **1.1.0** — theme gallery **manual holds**: applying an old theme no longer
  gets reverted by the poller; Re-apply/Regenerate/Refetch respect the hold.
- **1.0.0** — initial release: full pipeline, per-game SignalRGB effects
  (with the shared live-apply mirror), Windows Terminal, Topaz upscaling,
  VLM naming, config UI, scheduled tasks, security hardening.

## 📄 License

[MIT](LICENSE) — use it, fork it, change it; just keep the copyright notice.

---

**Credits:** built on top of [SteamGridDB](https://www.steamgriddb.com) (art),
the [Steam Web API](https://steamcommunity.com/dev) (library data),
[SignalRGB](https://www.signalrgb.com) and [OpenRGB](https://openrgb.org)
(lighting), [Topaz Gigapixel](https://www.topazlabs.com/gigapixel) (upscaling),
and KDE Plasma's theming stack. Not affiliated with Valve, WhirlwindFX, or
Topaz Labs.
