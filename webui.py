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

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
HTML_PATH = os.path.join(BASE_DIR, "webui.html")

TOPAZ_BASE = "https://api.topazlabs.com/image/v1"


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg):
    if not isinstance(cfg, dict):
        raise ValueError("config must be a JSON object")
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


# -------------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    server_version = "SteamWallpaperUI/1.0"

    def _send(self, code, obj=None, content_type="application/json", raw=None):
        body = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return {}

    def log_message(self, *a):
        pass  # quiet

    # ---- GET
    def do_GET(self):
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
        elif path == "/api/log":
            import urllib.parse
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            lines = int(q.get("lines", [200])[0])
            log_path = ""
            try:
                log_path = os.path.join(BASE_DIR, load_config().get("log_file", "service.log"))
                with open(log_path, encoding="utf-8", errors="replace") as f:
                    tail = f.readlines()[-lines:]
                self._send(200, {"ok": True, "log": "".join(tail)})
            except OSError:
                self._send(200, {"ok": False, "log": f"(no log yet at {log_path})"})
        else:
            self._send(404, {"error": "not found"})

    # ---- POST
    def do_POST(self):
        path = self.path.split("?")[0]
        body = self._body()
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
