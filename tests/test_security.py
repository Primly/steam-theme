"""Security regression tests — each pins a fix from the 2026-11-18 review
(consolidated report: steam-theme-security-review-final.md).

Run from the repo root:  python -m unittest discover -s tests -v
"""

import io
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import artwork  # noqa: E402
import extras  # noqa: E402
import main  # noqa: E402
import upscale  # noqa: E402
import wallpaper  # noqa: E402
import webui  # noqa: E402


def _quiet(m):
    pass


def _jpeg_bytes():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (600, 600), (120, 30, 30)).save(buf, "JPEG")
    return buf.getvalue()


class TestKonsoleProfileInjection(unittest.TestCase):
    """F1: a newline in a game name must not inject [General] Command= into
    the Konsole profile — Konsole EXECUTES that key when the profile opens."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.dir, ignore_errors=True))
        self._old = extras._KONSOLE_DIR
        extras._KONSOLE_DIR = self.dir
        self.addCleanup(setattr, extras, "_KONSOLE_DIR", self._old)
        self.cfg = {"konsole": {"enabled": True, "set_default_profile": False}}
        self.pal = {"background": "#101014", "colors": ["#66C0F4"] * 16,
                    "accent": "#66C0F4", "appearance": "dark"}

    def test_newline_name_cannot_inject_command(self):
        import configparser
        extras.apply_konsole(self.cfg, self.pal,
                             "Evil\n[General]\nCommand=touch /tmp/pwned",
                             log=_quiet)
        with open(os.path.join(self.dir, "Steam Theme.profile"),
                  encoding="utf-8") as f:
            profile = f.read()
        # the payload may survive as a mangled single-line VALUE, but it must
        # never become an injected key or section (Konsole executes Command=)
        self.assertFalse(any(l.startswith("Command=")
                             for l in profile.splitlines()))
        self.assertEqual(
            sum(1 for l in profile.splitlines() if l.strip() == "[General]"),
            1)
        cp = configparser.ConfigParser()
        cp.read_string(profile)  # base code: DuplicateSectionError here
        self.assertFalse(cp.has_option("General", "Command"))
        scheme_line = next(l for l in profile.splitlines()
                           if l.startswith("ColorScheme="))
        # no control chars (printable UTF-8 is allowed and expected)
        self.assertFalse(any(ord(c) < 32 or ord(c) == 127
                             for c in scheme_line), scheme_line)

    def test_colorscheme_filename_has_no_control_chars(self):
        extras.apply_konsole(self.cfg, self.pal, "Bad\nName\rX", log=_quiet)
        for name in os.listdir(self.dir):
            self.assertFalse(any(ord(c) < 32 or ord(c) == 127 for c in name),
                             name)

    def test_non_ascii_name_preserved(self):
        # the security fix must NOT empty non-Latin names (a pure CJK name
        # would otherwise write a hidden '.colorscheme' + empty ColorScheme=)
        extras.apply_konsole(self.cfg, self.pal, "対馬魂 Ghost of Tsushima",
                             log=_quiet)
        names = os.listdir(self.dir)
        self.assertNotIn(".colorscheme", names)
        cs = [n for n in names if n.endswith(".colorscheme")]
        self.assertEqual(len(cs), 1)
        with open(os.path.join(self.dir, "Steam Theme.profile"),
                  encoding="utf-8") as f:
            profile = f.read()
        value = next(l for l in profile.splitlines()
                     if l.startswith("ColorScheme=")).split("=", 1)[1]
        self.assertTrue(value)
        self.assertIn("対馬魂", value)
        self.assertIn(value + ".colorscheme", names)

    def test_all_control_name_falls_back(self):
        extras.apply_konsole(self.cfg, self.pal, "\n\r\t", log=_quiet)
        names = os.listdir(self.dir)
        self.assertNotIn(".colorscheme", names)
        self.assertIn("Game.colorscheme", names)


class TestAtomicStateWrite(unittest.TestCase):
    """N1: a failed mid-write must leave the previous state.json intact
    (in-place writes truncate first, silently losing holds/state)."""

    def test_crash_mid_write_preserves_old_state(self):
        # tmp MUST live under BASE_DIR: cfg_path anchors state_file there, and
        # an escaping absolute path falls back to the checkout's LIVE
        # state.json — polluting it with a manual_hold (review round 2).
        with tempfile.TemporaryDirectory(dir=main.BASE_DIR) as tmp:
            rel = os.path.relpath(tmp, main.BASE_DIR)
            cfg = {"state_file": os.path.join(rel, "state.json")}
            main.save_state(cfg, {"manual_hold": {"identity": "440"}})

            def exploding_dump(obj, f, **kw):
                f.write('{"manual_hold": ')  # partial garbage, then "crash"
                raise OSError("disk full")

            with mock.patch.object(main.json, "dump", exploding_dump):
                with self.assertRaises(OSError):
                    main.save_state(cfg, {"manual_hold": {"identity": "570"}})
            self.assertEqual(main.load_state(cfg),
                             {"manual_hold": {"identity": "440"}})


class TestRoleWhitelist(unittest.TestCase):
    """N4: monitors[].role becomes a path component (slides/<role>/) — only
    hero/logo/icon may ever be assigned, and junk entries must not crash."""

    def _mons(self):
        return [{"device": "\\\\.\\DISPLAY1", "desc": "MON",
                 "rect": (0, 0, 100, 100)}]

    def test_hostile_role_not_assigned(self):
        mons = wallpaper.assign_roles(
            self._mons(), [{"match": "MON", "role": "x/../../../escaped"}],
            log=_quiet)
        self.assertIn(mons[0]["role"], ("hero", "logo", "icon"))

    def test_malformed_entries_do_not_crash(self):
        mons = wallpaper.assign_roles(
            self._mons(),
            [{"role": "hero"}, {"match": "MON"}, "junk", None,
             {"match": 5, "role": "hero"}, {"match": "MON", "role": 7}],
            log=_quiet)
        self.assertIn(mons[0]["role"], ("hero", "logo", "icon"))

    def test_non_list_mapping_tolerated(self):
        mons = wallpaper.assign_roles(self._mons(), None, log=_quiet)
        self.assertEqual(mons[0]["role"], "hero")

    def test_legit_mapping_still_works(self):
        mons = wallpaper.assign_roles(
            self._mons(), [{"match": "MON", "role": "logo"}], log=_quiet)
        self.assertEqual(mons[0]["role"], "logo")


class TestSafeJoinSymlink(unittest.TestCase):
    """F3: a symlink inside base pointing out must be refused (abspath-only
    containment let read/write escape through a planted link)."""

    def test_symlink_escape_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "cache")
            os.makedirs(base)
            outside = os.path.join(tmp, "outside")
            os.makedirs(outside)
            link = os.path.join(base, "evil")
            try:
                os.symlink(outside, link)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable on this host")
            with self.assertRaises(ValueError):
                main.safe_join(base, "evil")
            with open(os.path.join(outside, "secret.txt"), "w") as f:
                f.write("x")
            with self.assertRaises(ValueError):
                main.safe_join(base, "evil", "secret.txt")


class TestDownloadCap(unittest.TestCase):
    """F2: art downloads stream under a hard byte cap, contain network
    errors per-URL, and refuse a redirect downgrade to http."""

    def _fake_response(self, chunks, content, status=200,
                       url="https://cdn.example/art.jpg"):
        r = mock.Mock()
        r.status_code = status
        r.url = url
        r.content = content
        r.iter_content = lambda n: iter(chunks)
        r.__enter__ = lambda s: s
        r.__exit__ = lambda s, *a: None
        return r

    def test_over_cap_download_rejected_and_cleaned(self):
        data = _jpeg_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "hero_1.jpg")
            big = [b"x" * 65536] * (artwork.MAX_DOWNLOAD_BYTES // 65536 + 2)
            r = self._fake_response(big, data)
            with mock.patch.object(artwork.requests, "get", return_value=r):
                got = artwork._download("https://cdn.example/a.jpg", dest,
                                        log=_quiet)
            self.assertIsNone(got)
            self.assertFalse(os.path.exists(dest))

    def test_network_error_contained(self):
        import requests
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "hero_1.jpg")
            with mock.patch.object(artwork.requests, "get",
                                   side_effect=requests.ConnectionError("boom")):
                got = artwork._download("https://cdn.example/a.jpg", dest,
                                        log=_quiet)
            self.assertIsNone(got)

    def test_http_downgrade_refused(self):
        data = _jpeg_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "hero_1.jpg")
            r = self._fake_response([data], data,
                                    url="http://169.254.169.254/latest/meta-data")
            with mock.patch.object(artwork.requests, "get", return_value=r):
                got = artwork._download("https://cdn.example/a.jpg", dest,
                                        log=_quiet)
            self.assertIsNone(got)

    def test_valid_https_download_succeeds(self):
        data = _jpeg_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "hero_1.jpg")
            r = self._fake_response([data], data)
            with mock.patch.object(artwork.requests, "get", return_value=r):
                got = artwork._download("https://cdn.example/a.jpg", dest,
                                        log=_quiet)
            self.assertIsNotNone(got)
            self.assertTrue(got and os.path.exists(got))


class TestCappedContent(unittest.TestCase):
    """F2: the shared upscale/video download cap helper."""

    def test_over_cap_returns_none(self):
        r = mock.Mock()
        r.iter_content = lambda n: iter([b"x" * 1024, b"y" * 1024])
        self.assertIsNone(upscale._capped_content(r, 1500, _quiet, "test"))

    def test_under_cap_returns_bytes(self):
        r = mock.Mock()
        r.iter_content = lambda n: iter([b"x" * 1024, b"y" * 512])
        self.assertEqual(upscale._capped_content(r, 4096, _quiet, "test"),
                         b"x" * 1024 + b"y" * 512)


class TestWindowsTerminalMalformedSettings(unittest.TestCase):
    """A1: malformed settings.json must not crash the extras pass — a crash
    there skips save_state, so the poller re-themes every 30s forever."""

    def _run(self, settings):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "settings.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(settings, f)
            pal = {"background": "#101014", "colors": ["#66C0F4"] * 16,
                   "accent": "#66C0F4", "appearance": "dark"}
            cfg = {"windows_terminal": {"enabled": True,
                                        "set_background_image": False}}
            with mock.patch.object(extras, "_wt_settings_paths",
                                   lambda: [path]):
                extras.apply_windows_terminal(cfg, pal, "Game", None,
                                              log=_quiet)
            with open(path, encoding="utf-8") as f:
                return json.load(f)

    def test_schemes_not_a_list(self):
        out = self._run({"schemes": "junk"})
        self.assertIsInstance(out["schemes"], list)
        self.assertEqual(out["schemes"][-1]["name"], "Game")

    def test_schemes_with_non_dict_entries(self):
        out = self._run({"schemes": ["junk", 42, {"name": "Old"}]})
        self.assertEqual(out["schemes"][-1]["name"], "Game")

    def test_profiles_not_a_dict(self):
        out = self._run({"profiles": "junk"})
        self.assertEqual(out["profiles"]["defaults"]["colorScheme"], "Game")

    def test_settings_not_an_object(self):
        # must skip cleanly, not raise (file left untouched)
        out = self._run(["not", "an", "object"])
        self.assertEqual(out, ["not", "an", "object"])


class TestExcludeSet(unittest.TestCase):
    """N6: exclude_appids null/string-list must not crash the poll or
    silently fail open (str entries never match an int appid)."""

    def test_null_and_junk(self):
        self.assertEqual(main._exclude_set({"exclude_appids": None}), set())
        self.assertEqual(main._exclude_set({}), set())
        self.assertEqual(main._exclude_set({"exclude_appids": "431960"}), set())
        self.assertEqual(
            main._exclude_set({"exclude_appids": [None, "x", {}]}), set())

    def test_string_list_coerced(self):
        self.assertEqual(main._exclude_set({"exclude_appids": ["431960"]}),
                         {431960})
        self.assertEqual(
            main._exclude_set({"exclude_appids": [431960, "570"]}),
            {431960, 570})


class UIServerFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), webui.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def req(self, path, body=None, raw=None):
        method = "POST" if (body is not None or raw is not None) else "GET"
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                   method=method)
        r.add_header("Host", f"127.0.0.1:{self.port}")
        data = raw if raw is not None else (
            json.dumps(body).encode() if body is not None else None)
        if data:
            r.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(r, data=data, timeout=10) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()


class TestDotKeysRejected(UIServerFixture):
    """F4: dot-only keys pass the charset regex but must still be refused."""

    def test_apply_theme_dot_keys(self):
        for bad in (".", "..", "..."):
            code, _ = self.req("/api/apply-theme", body={"key": bad})
            self.assertEqual(code, 400, bad)

    def test_hero_choice_dot_keys(self):
        for bad in (".", ".."):
            code, _ = self.req("/api/hero-choice",
                               body={"key": bad, "index": 0})
            self.assertEqual(code, 400, bad)


class TestDeepJsonBody(UIServerFixture):
    """N3: pathologically nested JSON (under the size cap) must yield a 400,
    not an unhandled RecursionError that drops the connection."""

    def test_deeply_nested_json_returns_400(self):
        raw = b"[" * 100000 + b"]" * 100000  # 200KB < MAX_BODY_BYTES
        code, _ = self.req("/api/config", raw=raw)
        self.assertEqual(code, 400)


class TestBodyReadTimeout(UIServerFixture):
    """N2: a client dribbling a declared body must hit the socket timeout
    and get a 400 — not pin a worker thread forever."""

    def test_dribbling_body_times_out_to_400(self):
        with mock.patch.object(webui.Handler, "timeout", 0.5):
            s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            try:
                s.sendall(b"POST /api/config HTTP/1.1\r\n"
                          b"Host: 127.0.0.1:%d\r\n"
                          b"Content-Type: application/json\r\n"
                          b"Content-Length: 100\r\n\r\n{" % self.port)
                s.settimeout(5)
                resp = s.recv(4096)
                self.assertIn(b"400", resp.split(b"\r\n")[0])
            finally:
                s.close()


class TestPipelineSingleFlight(UIServerFixture):
    """F7: while a pipeline run holds the lock, trigger endpoints must 409
    instead of spawning a concurrent check_once/reapply thread."""

    def test_run_now_rejected_while_pipeline_busy(self):
        self.assertTrue(webui._PIPELINE_LOCK.acquire(blocking=False))
        try:
            code, _ = self.req("/api/run-now", body={})
            self.assertEqual(code, 409)
        finally:
            webui._PIPELINE_LOCK.release()

    def test_redo_rejected_while_pipeline_busy(self):
        self.assertTrue(webui._PIPELINE_LOCK.acquire(blocking=False))
        try:
            code, _ = self.req("/api/redo", body={"mode": "regenerate"})
            self.assertEqual(code, 409)
        finally:
            webui._PIPELINE_LOCK.release()

    def test_apply_theme_rejected_while_pipeline_busy(self):
        self.assertTrue(webui._PIPELINE_LOCK.acquire(blocking=False))
        try:
            code, _ = self.req("/api/apply-theme", body={"key": "440"})
            self.assertEqual(code, 409)
        finally:
            webui._PIPELINE_LOCK.release()

    def test_fetch_all_rejected_while_pipeline_busy(self):
        # Ultimate Fetch must share the gate, not race a quick trigger
        webui.FETCH_ALL["running"] = False
        webui.REGEN["running"] = False
        self.assertTrue(webui._PIPELINE_LOCK.acquire(blocking=False))
        try:
            code, _ = self.req("/api/fetch-all", body={})
            self.assertEqual(code, 409)
            self.assertFalse(webui.FETCH_ALL["running"])
        finally:
            webui._PIPELINE_LOCK.release()

    def test_regenerate_all_rejected_while_pipeline_busy(self):
        webui.FETCH_ALL["running"] = False
        webui.REGEN["running"] = False
        self.assertTrue(webui._PIPELINE_LOCK.acquire(blocking=False))
        try:
            code, _ = self.req("/api/regenerate-all", body={"videos": False})
            self.assertEqual(code, 409)
            self.assertFalse(webui.REGEN["running"])
        finally:
            webui._PIPELINE_LOCK.release()

    def _await_lock_free(self):
        import time as _t
        for _ in range(100):
            if webui._PIPELINE_LOCK.acquire(blocking=False):
                webui._PIPELINE_LOCK.release()
                return
            _t.sleep(0.05)
        self.fail("pipeline lock still held after the worker finished")

    def test_lock_released_after_normal_run(self):
        import main
        with mock.patch.object(main, "load_config", lambda: {}), \
                mock.patch.object(main, "_log_factory",
                                  lambda cfg: (lambda m: None)), \
                mock.patch.object(main, "check_once", lambda *a, **k: None):
            code, _ = self.req("/api/run-now", body={})
            self.assertEqual(code, 200)
            self._await_lock_free()

    def test_lock_released_after_work_exception(self):
        import main

        def boom(*a, **k):
            raise RuntimeError("boom")
        with mock.patch.object(main, "load_config", lambda: {}), \
                mock.patch.object(main, "_log_factory",
                                  lambda cfg: (lambda m: None)), \
                mock.patch.object(main, "check_once", boom):
            code, _ = self.req("/api/run-now", body={})
            self.assertEqual(code, 200)
            self._await_lock_free()


class TestConfigFileMode(unittest.TestCase):
    """F6: config.json holds every API key — it must be owner-only."""

    def test_saved_config_is_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with mock.patch.object(webui, "CONFIG_PATH", path):
                webui.save_config({"steam_api_key": "k"})
            if os.name == "posix":
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["steam_api_key"], "k")


if __name__ == "__main__":
    unittest.main()
