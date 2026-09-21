"""Web UI guard tests — spins up the real server on an ephemeral port and
throws hostile requests at it.

Run from the repo root:  python -m unittest discover -s tests -v
"""

import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import webui  # noqa: E402


class ServerFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), webui.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def req(self, path, method="GET", host=None, origin=None, fetchsite=None,
            body=None, raw=None):
        host = host or f"127.0.0.1:{self.port}"
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                   method=method)
        r.add_header("Host", host)
        if origin:
            r.add_header("Origin", origin)
        if fetchsite:
            r.add_header("Sec-Fetch-Site", fetchsite)
        data = raw if raw is not None else (
            json.dumps(body).encode() if body is not None else None)
        if data:
            r.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(r, data=data, timeout=5) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            return e.code


class TestLocalOnlyGuard(ServerFixture):
    def test_legit_localhost_ok(self):
        self.assertEqual(self.req("/api/config"), 200)
        self.assertEqual(self.req("/api/config",
                                  host=f"localhost:{self.port}"), 200)

    def test_dns_rebinding_rejected(self):
        # an attacker domain resolving to 127.0.0.1 sends its own Host header
        self.assertEqual(self.req("/api/config", host="evil.com"), 403)
        self.assertEqual(self.req("/api/config", host="127.0.0.1.evil.com"), 403)
        self.assertEqual(self.req("/api/config", host="169.254.1.1"), 403)

    def test_cross_origin_post_rejected(self):
        self.assertEqual(self.req("/api/config", "POST",
                                  origin="https://evil.com", body={"a": 1}), 403)
        self.assertEqual(self.req("/api/config", "POST",
                                  origin="null", body={"a": 1}), 403)

    def test_sec_fetch_site_cross_site_rejected(self):
        self.assertEqual(self.req("/api/config", "POST",
                                  fetchsite="cross-site", body={"a": 1}), 403)

    def test_cross_port_localhost_rejected(self):
        # a compromised page served by ANOTHER local app (LM Studio :1234,
        # SignalRGB :16038, any dev server) must not be able to POST to us
        self.assertEqual(self.req("/api/config", "POST",
                                  origin="http://127.0.0.1:1234",
                                  body={"a": 1}), 403)
        self.assertEqual(self.req("/api/config", "POST",
                                  origin="http://localhost:16038",
                                  body={"a": 1}), 403)
        self.assertEqual(self.req("/api/config", "POST",
                                  origin="https://localhost",
                                  body={"a": 1}), 403)

    def test_local_origin_post_accepted(self):
        # must reach validation (400 for junk), not the guard (403)
        self.assertEqual(self.req("/api/config", "POST",
                                  origin=f"http://127.0.0.1:{self.port}",
                                  body={"cache_dir": "../../Windows"}), 400)


class TestBodyValidation(ServerFixture):
    def test_empty_body_rejected(self):
        self.assertEqual(self.req("/api/config", "POST", raw=b""), 400)

    def test_invalid_json_rejected(self):
        self.assertEqual(self.req("/api/config", "POST", raw=b"{nope"), 400)

    def test_non_object_rejected(self):
        self.assertEqual(self.req("/api/config", "POST", raw=b'"string"'), 400)

    def test_oversized_body_rejected(self):
        big = b'{"pad":"' + b"A" * (webui.MAX_BODY_BYTES + 1000) + b'"}'
        self.assertEqual(self.req("/api/config", "POST", raw=big), 400)

    def test_redo_mode_allowlist(self):
        self.assertEqual(self.req("/api/redo", "POST", body={"mode": "nope"}), 400)

    def test_apply_theme_key_validated(self):
        for bad in ("../../evil", "a b", "", "a|b", "..\\.."):
            self.assertEqual(
                self.req("/api/apply-theme", "POST", body={"key": bad}), 400)


class TestArtEndpoint(ServerFixture):
    def test_traversal_rejected(self):
        self.assertEqual(self.req("/api/art?key=../../../..&role=hero"), 404)

    def test_bad_role_rejected(self):
        self.assertEqual(self.req("/api/art?key=x&role=../../evil"), 400)

    def test_missing_art_404(self):
        self.assertEqual(self.req("/api/art?key=no_such_game&role=hero"), 404)


if __name__ == "__main__":
    unittest.main()
