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
from unittest import mock

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

    def test_bad_candidate_index_400(self):
        self.assertEqual(self.req("/api/art?key=x&role=hero&i=abc"), 400)
        self.assertEqual(self.req("/api/art?key=x&role=hero&i=1%20OR%201"), 400)

    def test_out_of_range_candidate_404(self):
        self.assertEqual(
            self.req("/api/art?key=no_such_game&role=hero&i=99"), 404)


class TestHeroChoiceEndpoint(ServerFixture):
    def test_bad_key_rejected(self):
        for bad in ("../../evil", "a b", "", "a|b"):
            self.assertEqual(self.req("/api/hero-choice", "POST",
                                      body={"key": bad, "index": 0}), 400)

    def test_bad_index_rejected(self):
        for bad in ("nope", None, [1], {"x": 1}):
            self.assertEqual(self.req("/api/hero-choice", "POST",
                                      body={"key": "440", "index": bad}), 400)

    def test_non_current_theme_rejected(self):
        # a well-formed key that is not the currently-applied theme must be
        # refused before any file is touched
        code = self.req("/api/hero-choice", "POST",
                        body={"key": "definitely_not_current_987654",
                              "index": 0})
        self.assertEqual(code, 400)


class TestFetchAllEndpoints(ServerFixture):
    def test_status_shape(self):
        r = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/fetch-all-status")
        with urllib.request.urlopen(r, timeout=5) as resp:
            data = json.loads(resp.read())
        self.assertIn("running", data)
        self.assertIn("done", data)
        self.assertIn("total", data)

    def test_double_start_rejected(self):
        # simulate a running fetch without starting a worker (no network)
        webui.FETCH_ALL["running"] = True
        try:
            self.assertEqual(self.req("/api/fetch-all", "POST", body={}), 409)
        finally:
            webui.FETCH_ALL["running"] = False

    def test_cancel_when_idle_is_harmless(self):
        webui.FETCH_ALL["running"] = False
        r = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/fetch-all-cancel",
            method="POST", data=b"{}",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(r, timeout=5) as resp:
            data = json.loads(resp.read())
        self.assertFalse(data["ok"])

    def test_upscale_option_requires_upscaling_enabled(self):
        # opting into credits with upscaling disabled must fail BEFORE the
        # worker thread starts (no silent fetch-only surprise)
        import main
        webui.FETCH_ALL["running"] = False
        with mock.patch.object(main, "load_config",
                               lambda: {"upscaling": {"enabled": False}}):
            code = self.req("/api/fetch-all", "POST", body={"upscale": True})
        self.assertEqual(code, 400)
        self.assertFalse(webui.FETCH_ALL["running"])  # nothing started

    def test_upscale_estimate_endpoint_passthrough(self):
        import main
        with mock.patch.object(main, "upscale_pending_estimate",
                               lambda cfg, log: {"games": 2, "images": 5}):
            r = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/api/fetch-all-upscale-estimate")
            with urllib.request.urlopen(r, timeout=5) as resp:
                data = json.loads(resp.read())
        self.assertTrue(data["ok"])
        self.assertEqual(data["images"], 5)
        self.assertEqual(data["games"], 2)


class TestComfyEndpoint(ServerFixture):
    def _post(self, body):
        r = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/test/comfy",
            method="POST", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(r, timeout=5) as resp:
            return json.loads(resp.read())

    def test_bad_host_rejected_before_any_network(self):
        # no mocking: the host regex must reject this before requests is used
        data = self._post({"host": "evil.com/x", "port": 8188})
        self.assertFalse(data["ok"])
        self.assertIn("bad host", data["error"])

    def test_bad_port_rejected(self):
        for bad in ("nope", 0, 70000):
            data = self._post({"host": "127.0.0.1", "port": bad})
            self.assertFalse(data["ok"])
            self.assertIn("bad port", data["error"])

    def test_unreachable_server(self):
        import requests
        with mock.patch.object(
                webui.requests, "get",
                side_effect=requests.RequestException("nope")):
            data = self._post({"host": "127.0.0.1", "port": 8188})
        self.assertFalse(data["ok"])
        self.assertIn("unreachable", data["error"])

    def test_non_200_system_stats(self):
        with mock.patch.object(webui.requests, "get") as g:
            g.return_value = mock.Mock(status_code=500)
            data = self._post({"host": "127.0.0.1", "port": 8188})
        self.assertFalse(data["ok"])
        self.assertIn("500", data["error"])

    def test_success_reports_gpu_and_default_workflow_ok(self):
        with mock.patch.object(webui.requests, "get") as g:
            r = mock.Mock(status_code=200)
            r.json = lambda: {"devices": [{"name": "NVIDIA RTX 4090"}]}
            g.return_value = r
            data = self._post({"host": "127.0.0.1", "port": 8188,
                               "workflow": ""})  # empty = built-in default
        self.assertTrue(data["ok"], data)
        self.assertIn("NVIDIA RTX 4090", data["detail"])

    def test_connected_but_workflow_json_broken(self):
        with mock.patch.object(webui.requests, "get") as g:
            r = mock.Mock(status_code=200)
            r.json = lambda: {"devices": []}
            g.return_value = r
            data = self._post({"host": "127.0.0.1", "port": 8188,
                               "workflow": "{bad json"})
        self.assertFalse(data["ok"])
        self.assertIn("workflow", data["error"])

    def test_connected_but_workflow_missing_required_nodes(self):
        with mock.patch.object(webui.requests, "get") as g:
            r = mock.Mock(status_code=200)
            r.json = lambda: {"devices": []}
            g.return_value = r
            data = self._post({"host": "127.0.0.1", "port": 8188,
                               "workflow": '{"1": {"class_type": "KSampler"}}'})
        self.assertFalse(data["ok"])
        self.assertIn("workflow", data["error"])

    def test_default_workflow_served_over_http(self):
        r = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/comfy-default-workflow")
        with urllib.request.urlopen(r, timeout=5) as resp:
            data = json.loads(resp.read())
        self.assertTrue(data["ok"])
        self.assertIn("LoadImage", data["workflow"])
        self.assertIn("SaveImage", data["workflow"])


if __name__ == "__main__":
    unittest.main()
