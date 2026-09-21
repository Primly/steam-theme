"""Browser-based configuration UI.

Zero-dependency (stdlib http.server), bound to localhost only.
  python main.py --ui        -> http://127.0.0.1:8765

API:
  GET  /api/config              current config.json
  POST /api/config              validate + atomically save config.json
  GET  /api/monitors            detected displays
  GET  /api/detect-steamid      SteamID64 from registry
  GET  /api/log?lines=200       tail of service.log
  POST /api/test/steam          {steam_api_key, steam_id64}
  POST /api/test/sgdb           {steamgriddb_api_key}
  POST /api/test/ai             {base_url, api_key, model}   (OpenAI-compatible)
  POST /api/test/topaz          {api_key}                    (auth check, no credits)
  POST /api/run-now             run the pipeline once in a background thread
"""

import json
import os
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
HTML_PATH = os.path.join(BASE_DIR, "webui.html")

TOPAZ_BASE = "https://api.topazlabs.com/image/v1"

MAX_BODY_BYTES = 256 * 1024          # config JSON is a few KB; cap abuse
MAX_LOG_LINES = 1000
REDO_MODES = {"reapply", "regenerate", "refetch"}
LOCAL_HOSTNAMES = {"127.0.0.1", "localhost", "::1", "[::1]"}


def load_config():
    # fresh checkout: serve example-config defaults until the user saves
    if not os.path.exists(CONFIG_PATH):
        import main as app
        return app.load_config()
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg):
    if not isinstance(cfg, dict):
        raise ValueError("config must be a JSON object")
    # path-like settings must stay inside the app directory — a hostile value
    # here would otherwise turn /api/log into an arbitrary file read and the
    # refetch handler into an arbitrary directory delete
    import main as app
    for key, default in (("cache_dir", "cache"), ("state_file", "state.json"),
                         ("log_file", "service.log")):
        val = cfg.get(key)
        if val:
            try:
                app.safe_join(BASE_DIR, val)
            except ValueError:
                raise ValueError(f"{key!r} must be a path inside the app folder")
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)  # atomic on Windows


# ------------------------------------------------------------------- testers

def test_steam(body):
    import steamdetect
    key, sid = body.get("steam_api_key", ""), body.get("steam_id64", "")
    if not sid:
        sid = steamdetect.get_steam_id64() or ""
    t = time.time()
    r = requests.get(
        "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/",
        params={"key": key, "steamids": sid}, timeout=15)
    r.raise_for_status()
    players = r.json().get("response", {}).get("players") or []
    if not players:
        return {"ok": False, "error": "no player returned — check key and SteamID64"}
    p = players[0]
    return {"ok": True, "latency_ms": round((time.time() - t) * 1000),
            "detail": f"{p.get('personaname')} ({p.get('steamid')})"}


def test_sgdb(body):
    t = time.time()
    r = requests.get("https://www.steamgriddb.com/api/v2/search/autocomplete/quasimorph",
                     headers={"Authorization": f"Bearer {body.get('steamgriddb_api_key', '')}"},
                     timeout=15)
    if r.status_code in (401, 403):
        return {"ok": False, "error": f"HTTP {r.status_code} — invalid API key"}
    r.raise_for_status()
    n = len(r.json().get("data") or [])
    return {"ok": True, "latency_ms": round((time.time() - t) * 1000),
            "detail": f"key valid, search returned {n} results"}


def test_ai(body):
    """OpenAI-compatible: list models, then a 1-token chat completion."""
    base = (body.get("base_url") or "").rstrip("/")
    if urlparse(base).scheme not in ("http", "https"):
        return {"ok": False, "error": "base_url must be an http(s) URL"}
    key = body.get("api_key") or ""
    model = body.get("model") or ""
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    out = {"ok": False}
    t = time.time()
    try:
        r = requests.get(base + "/models", headers=headers, timeout=10)
        if r.status_code == 200:
            ids = [m.get("id") for m in r.json().get("data", []) if m.get("id")]
            out["models"] = ids
    except requests.RequestException:
        pass  # some servers don't implement /models; chat test is authoritative
    try:
        r = requests.post(base + "/chat/completions", headers=headers, timeout=45,
                          json={"model": model,
                                "messages": [{"role": "user",
                                              "content": "Reply with exactly: ok"}],
                                "max_tokens": 8, "temperature": 0})
        if r.status_code != 200:
            out["error"] = f"chat HTTP {r.status_code}: {r.text[:300]}"
            return out
        reply = r.json()["choices"][0]["message"]["content"]
        out.update(ok=True, latency_ms=round((time.time() - t) * 1000),
                   detail=f"model '{model}' replied: {reply.strip()!r}")
        return out
    except requests.RequestException as e:
        out["error"] = f"unreachable: {e}"
        return out


def test_topaz(body):
    """Auth check that uses NO credits: POST /enhance/async with no image.
    A valid key gets a 4xx validation error; an invalid key gets 401/403."""
    key = body.get("api_key") or ""
    t = time.time()
    try:
        r = requests.post(TOPAZ_BASE + "/enhance/async",
                          headers={"X-API-Key": key}, timeout=20)
    except requests.RequestException as e:
        return {"ok": False, "error": f"unreachable: {e}"}
    ms = round((time.time() - t) * 1000)
    if r.status_code in (401, 403):
        return {"ok": False, "latency_ms": ms,
                "error": f"HTTP {r.status_code} — API key rejected"}
    return {"ok": True, "latency_ms": ms,
            "detail": f"key accepted (HTTP {r.status_code} on purposefully-empty job; "
                      "no image was processed, no credits used)"}


TESTS = {"steam": test_steam, "sgdb": test_sgdb, "ai": test_ai, "topaz": test_topaz}


def _gallery():
    """Every cached game theme, for the UI gallery."""
    import main as app
    cfg = app.load_config()
    root = app.cfg_path(cfg, "cache_dir", "cache")
    items = []
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return items
    state = app.load_state(cfg)
    current = str(state.get("last_cache_key") or state.get("last_appid") or "")
    for entry in entries:
        folder = app.safe_join(root, entry)
        if not os.path.isdir(folder):
            continue
        try:
            with open(os.path.join(folder, "palette.json"), encoding="utf-8") as f:
                p = json.load(f)
        except (OSError, ValueError):
            continue
        items.append({
            "key": entry,
            "theme_name": p.get("theme_name") or entry,
            "accent": p.get("accent"),
            "appearance": p.get("appearance"),
            "has_art": os.path.exists(os.path.join(folder, "hero.jpg")),
            "current": entry == current,
        })
    return items


# -------------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    server_version = "SteamThemeUI/1.0"

    def _send(self, code, obj=None, content_type="application/json", raw=None):
        body = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        """Parse the JSON request body. Returns None when the body is absent,
        unreadable, or over the size cap — callers must reject the request
        rather than treating it as an empty object."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if n <= 0 or n > MAX_BODY_BYTES:
            # drain a bounded amount so the client can actually read our 400
            # (closing with unread request data triggers a TCP RST on Windows
            # and the response is lost)
            self.close_connection = True
            remaining = min(max(n, 0), 4 * 1024 * 1024)
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            return None
        try:
            data = json.loads(self.rfile.read(n))
            return data if isinstance(data, (dict, list)) else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def _local_only(self):
        """DNS-rebinding / CSRF guard. The server holds every API key in
        config.json and can trigger file writes, so it must only answer
        genuinely-local requests:
          - Host must be 127.0.0.1/localhost/::1 (blocks DNS-rebinding,
            where an attacker domain resolves to 127.0.0.1 and would
            otherwise be same-origin with this server)
          - Origin, when present, must be EXACTLY this server's origin.
            A merely-local origin is not enough: other localhost apps
            (dev servers, LM Studio, SignalRGB, ...) could serve a
            compromised page that POSTs cross-port — simple requests
            don't need CORS to *send*, only to read
          - Sec-Fetch-Site: cross-site is rejected outright
        """
        port = self.server.server_address[1]
        own_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}",
                       f"http://[::1]:{port}"}

        def host_ok(value):
            # Host headers are bare 'name:port'; Origin includes a scheme
            parsed = urlparse(value if "://" in value else f"//{value}")
            return (parsed.hostname or "").lower() in LOCAL_HOSTNAMES

        if not host_ok(self.headers.get("Host", "")):
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in own_origins:
            return False
        if (self.headers.get("Sec-Fetch-Site") or "").lower() == "cross-site":
            return False
        return True

    def log_message(self, *a):
        pass  # quiet

    # ---- GET
    def do_GET(self):
        if not self._local_only():
            self._send(403, {"error": "forbidden: local requests only"})
            return
        path = self.path.split("?")[0]
        if path == "/":
            with open(HTML_PATH, "rb") as f:
                self._send(200, content_type="text/html; charset=utf-8", raw=f.read())
        elif path == "/api/config":
            self._send(200, load_config())
        elif path == "/api/monitors":
            try:
                import theme as theme_mod
                self._send(200, {"ok": True, "monitors": theme_mod.enumerate_monitors()})
            except Exception as e:
                self._send(200, {"ok": False, "error": str(e), "monitors": []})
        elif path == "/api/detect-steamid":
            import steamdetect
            self._send(200, {"steam_id64": steamdetect.get_steam_id64()})
        elif path == "/api/state":
            import main as app
            cfg = app.load_config()
            state = app.load_state(cfg)
            state["version"] = app.__version__
            state["theme_name"] = None
            cache_key = str(state.get("last_cache_key")
                            or state.get("last_appid") or "")
            try:
                pal = app.safe_join(app.cfg_path(cfg, "cache_dir", "cache"),
                                    cache_key, "palette.json")
                with open(pal, encoding="utf-8") as f:
                    state["theme_name"] = json.load(f).get("theme_name")
            except (OSError, ValueError, json.JSONDecodeError):
                pass
            self._send(200, state)
        elif path == "/api/gallery":
            self._send(200, {"ok": True, "themes": _gallery()})
        elif path == "/api/art":
            import main as app
            q = parse_qs(urlparse(self.path).query)
            key, role = q.get("key", [""])[0], q.get("role", ["hero"])[0]
            if role not in ("hero", "logo", "icon"):
                self._send(400, {"error": "bad role"})
                return
            try:
                img = app.safe_join(app.cfg_path(load_config(), "cache_dir", "cache"),
                                    key, f"{role}.jpg")
                with open(img, "rb") as f:
                    data = f.read()
                self._send(200, content_type="image/jpeg", raw=data)
            except (OSError, ValueError):
                self._send(404, {"error": "no art"})
        elif path == "/api/log":
            import main as app
            try:
                lines = max(1, min(int(parse_qs(urlparse(self.path).query)
                                       .get("lines", [200])[0]), MAX_LOG_LINES))
            except ValueError:
                lines = 200
            log_path = app.cfg_path(load_config(), "log_file", "service.log")
            try:
                with open(log_path, encoding="utf-8", errors="replace") as f:
                    tail = f.readlines()[-lines:]
                self._send(200, {"ok": True, "log": "".join(tail)})
            except OSError:
                self._send(200, {"ok": False, "log": "(no log yet)"})
        else:
            self._send(404, {"error": "not found"})

    # ---- POST
    def do_POST(self):
        if not self._local_only():
            self._send(403, {"error": "forbidden: local requests only"})
            return
        path = self.path.split("?")[0]
        body = self._body()
        if body is None:
            self._send(400, {"ok": False, "error": "missing or invalid JSON body"})
            return
        if path == "/api/config":
            try:
                save_config(body)
                self._send(200, {"ok": True})
            except Exception as e:
                self._send(400, {"ok": False, "error": str(e)})
        elif path.startswith("/api/test/"):
            kind = path.rsplit("/", 1)[-1]
            fn = TESTS.get(kind)
            if not fn:
                self._send(404, {"error": "unknown test"})
                return
            try:
                self._send(200, fn(body))
            except Exception as e:
                self._send(200, {"ok": False, "error": str(e)})
        elif path == "/api/redo":
            mode = body.get("mode", "regenerate")
            if mode not in REDO_MODES:
                self._send(400, {"ok": False, "error": f"unknown redo mode {mode!r}"})
                return

            def work():
                import shutil
                import main as app
                cfg = app.load_config()
                log = app._log_factory(cfg)
                state = app.load_state(cfg)
                cache_key = str(state.get("last_cache_key")
                                or state.get("last_appid") or "")
                try:
                    if mode == "reapply":
                        app.reapply(cfg, log)
                        return
                    if mode == "refetch" and cache_key:
                        target = app.safe_join(
                            app.cfg_path(cfg, "cache_dir", "cache"), cache_key)
                        shutil.rmtree(target, ignore_errors=True)
                        log(f"redo: cleared cache for {cache_key} "
                            "(art + upscales will be re-created)")
                    app.check_once(cfg, log, force=True)
                except Exception as e:
                    log(f"redo error: {e}")
            threading.Thread(target=work, daemon=True).start()
            self._send(200, {"ok": True, "detail": f"{mode} started; watch the log"})
        elif path == "/api/apply-theme":
            import re as _re
            key = str(body.get("key") or "")
            # cache keys are appids or custom_<slug> — reject anything fancier
            # up front (safe_join would catch it too, but fail fast + cheap)
            if not _re.fullmatch(r"[A-Za-z0-9._-]+", key):
                self._send(400, {"ok": False, "error": "bad theme key"})
                return

            def work():
                import main as app
                cfg = app.load_config()
                log = app._log_factory(cfg)
                try:
                    app.reapply_from_cache(cfg, log, key)
                except Exception as e:
                    log(f"gallery apply error: {e}")
            threading.Thread(target=work, daemon=True).start()
            self._send(200, {"ok": True, "detail": f"applying '{key}'; watch the log"})
        elif path == "/api/release-hold":
            def work():
                import main as app
                cfg = app.load_config()
                log = app._log_factory(cfg)
                state = app.load_state(cfg)
                hold = state.pop("manual_hold", None)
                if not hold:
                    log("release-hold: no manual theme hold was active")
                    return
                app.save_state(cfg, state)
                log(f"manual theme hold released "
                    f"('{hold.get('name') or hold.get('identity')}') — "
                    "auto-theming resumed")
                try:
                    app.check_once(cfg, log)  # catch up to the last-played game
                except Exception as e:
                    log(f"release-hold check error: {e}")
            threading.Thread(target=work, daemon=True).start()
            self._send(200, {"ok": True,
                             "detail": "auto-theming resumed; catching up to "
                                       "your last-played game"})
        elif path == "/api/restore":
            def work():
                import main as app
                import theme as theme_mod
                log = app._log_factory(app.load_config())
                try:
                    theme_mod.restore_windows_look(log)
                except FileNotFoundError:
                    log("restore: no snapshot found "
                        "(windows_backup.json missing — nothing was ever applied?)")
                except Exception as e:
                    log(f"restore error: {e}")
            threading.Thread(target=work, daemon=True).start()
            self._send(200, {"ok": True,
                             "detail": "restoring previous Windows look; watch the log"})
        elif path == "/api/run-now":
            def work():
                import main as app
                cfg = app.load_config()
                log = app._log_factory(cfg)
                try:
                    app.check_once(cfg, log, force=True)
                except Exception as e:
                    log(f"run-now error: {e}")
            threading.Thread(target=work, daemon=True).start()
            self._send(200, {"ok": True, "detail": "pipeline started; watch the log below"})
        else:
            self._send(404, {"error": "not found"})


def serve(port=8765, open_browser=True):
    addr = ("127.0.0.1", port)
    httpd = ThreadingHTTPServer(addr, Handler)
    url = f"http://{addr[0]}:{addr[1]}"
    print(f"config UI at {url} (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    # direct launch = background server (e.g. scheduled task): no browser pop-up.
    # use `python main.py --ui` for an interactive launch that opens the page.
    serve(open_browser=False)
