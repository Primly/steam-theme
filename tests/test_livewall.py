"""Live-wallpaper (looping video -> Wallpaper Engine) tests.

Covers: graph-format workflow flattening (subgraphs, boundary constants,
named widgets), workflow validation + auto-append, the defer queue, the
ComfyUI submit flow against a mocked server, and Wallpaper Engine
integration (monitor mapping, -control invocation, config.json patching).

Run from the repo root:  python -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import livewall  # noqa: E402


def _mp4_bytes():
    head = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048
    return head


class LiveWallFixture(unittest.TestCase):
    def setUp(self):
        from PIL import Image
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.dir, ignore_errors=True))
        self.hero = os.path.join(self.dir, "hero.jpg")
        Image.new("RGB", (64, 64), (30, 40, 60)).save(self.hero, "JPEG")
        self.cfg = {"cache_dir": self.dir,
                    "livewall": {"enabled": True, "provider": "comfy",
                                 "host": "127.0.0.1", "port": 8188,
                                 "workflow": "", "defer_while_gaming": True,
                                 "timeout_seconds": 600}}


class TestGraphConversion(LiveWallFixture):
    """The new ComfyUI save format (nodes/links/definitions + subgraphs)
    flattens into classic API JSON — validated against the real built-in
    default workflow (LTX-2.5 first/last-frame loop)."""

    def test_default_workflow_converts(self):
        api, err = livewall.parse_workflow(livewall.default_workflow())
        self.assertIsNone(err, err)
        self.assertGreater(len(api), 40)   # 5 outer - subgraph + 39 inner
        types = {n["class_type"] for n in api.values()}
        self.assertIn("LoadImage", types)
        self.assertIn("TopazVideoAI", types)
        self.assertIn("GetVideoComponents", types)
        # local Topaz finishing defaults: x2 upscale (2560x736 ->
        # 5120x1472), apf-2 interpolation (24 fps x 2.5 -> 60 fps), CPU
        # side (use_gpu=true trips the node's FFmpeg encoder fallback)
        tz = [n for n in api.values() if n["class_type"] == "TopazVideoAI"][0]
        self.assertTrue(tz["inputs"]["enable_upscale"])
        self.assertEqual(tz["inputs"]["upscale_factor"], 2)
        self.assertTrue(tz["inputs"]["enable_interpolation"])
        self.assertFalse(tz["inputs"]["use_gpu"])
        self.assertEqual(tz["inputs"]["interpolation_model"], "apf-2")
        self.assertTrue(tz["inputs"]["save_video"])
        # the subgraph was flattened, not passed through by UUID
        self.assertNotIn("cf70afc4-5a03-47ce-8210-734b1de6c6bc", types)
        # every link input points at a real node
        for nid, n in api.items():
            for k, v in n["inputs"].items():
                if isinstance(v, list) and len(v) == 2 and isinstance(v[1], int):
                    self.assertIn(v[0], api, f"{nid}.{k} -> {v}")

    def test_boundary_constants_reach_inner_nodes(self):
        api, _ = livewall.parse_workflow(livewall.default_workflow())
        prompt = [n for n in api.values()
                  if n["class_type"] == "PrimitiveStringMultiline"]
        self.assertTrue(any("{loop_prompt}" in n["inputs"].get("value", "")
                            for n in prompt))
        # the width/height/duration/fps primitives carry the boundary
        # constants baked into the default LTX-2.5 workflow
        vals = {}
        for nid, n in api.items():
            if nid.endswith((":215", ":216", ":198", ":205")):
                vals[nid] = n["inputs"].get("value")
        self.assertEqual(vals.get("251:215"), 2560)   # width
        self.assertEqual(vals.get("251:216"), 736)    # height
        self.assertEqual(vals.get("251:198"), 12)     # duration
        self.assertEqual(vals.get("251:205"), 24)     # fps

    def test_loadimage_loses_frontend_widgets(self):
        api, _ = livewall.parse_workflow(livewall.default_workflow())
        li = [n for n in api.values() if n["class_type"] == "LoadImage"]
        self.assertEqual(len(li), 2)   # first frame + last frame
        for n in li:
            self.assertIn("image", n["inputs"])
            self.assertNotIn("upload", n["inputs"])

    def test_placeholder_fill_reaches_the_video_prompt(self):
        import upscale
        api, _ = livewall.parse_workflow(livewall.default_workflow())
        api, dropped = upscale.fill_workflow(
            api, {"game": "Quasimorph", "mood": "dark survival",
                  "theme_name": "Bloodsteel Vigil", "appearance": "dark",
                  "role": "hero",
                  "loop_prompt": "wisps of smoke drift upward"})
        self.assertEqual(dropped, [])
        blob = json.dumps(api)
        self.assertNotIn("{loop_prompt}", blob)
        self.assertIn("wisps of smoke drift upward", blob)

    def test_loop_prompt_placeholder_drops_gracefully(self):
        import upscale
        api, _ = livewall.parse_workflow(livewall.default_workflow())
        api, dropped = upscale.fill_workflow(
            api, {"game": "Quasimorph", "mood": "dark",
                  "theme_name": "T", "appearance": "dark", "role": "hero"})
        self.assertEqual(dropped, ["loop_prompt"])
        self.assertNotIn("{loop_prompt}", json.dumps(api))


class TestGraphSynthesis(unittest.TestCase):
    """ComfyUI's history action ("Open as workflow in a new tab") only
    accepts a *graph* in extra_data.extra_pnginfo.workflow — an Export-API
    blob there fails validation and the action silently opens nothing. Jobs
    submitted from an API workflow must therefore carry a synthesized graph."""

    API = {
        "1": {"class_type": "LoadImage", "inputs": {"image": "hero.png"}},
        "2": {"class_type": "UpscaleModelLoader",
              "inputs": {"model_name": "4x.pth"}},
        "3": {"class_type": "UltimateSDUpscale",
              "inputs": {"image": ["1", 0], "upscale_model": ["2", 0],
                         "seed": 7, "steps": 20}},
        "4": {"class_type": "SaveImage",
              "inputs": {"images": ["3", 0],
                         "filename_prefix": "steamtheme_ab12"}},
    }

    def test_shape_matches_comfyui_graph_schema(self):
        g = livewall.api_to_graph(self.API)
        self.assertEqual(g["version"], 0.4)
        self.assertIsInstance(g["nodes"], list)
        self.assertEqual(len(g["nodes"]), 4)
        self.assertIsInstance(g["last_node_id"], int)
        self.assertIsInstance(g["last_link_id"], int)
        for n in g["nodes"]:
            for key in ("id", "type", "pos", "size", "flags", "order",
                        "mode", "properties"):
                self.assertIn(key, n)
            self.assertIsInstance(n["flags"], dict)
            self.assertEqual(len(n["pos"]), 2)
            self.assertEqual(len(n["size"]), 2)
        for link in g["links"]:
            self.assertEqual(len(link), 6)

    def test_links_preserve_topology(self):
        g = livewall.api_to_graph(self.API)
        by_type = {n["type"]: n for n in g["nodes"]}
        pairs = {(l[1], l[2], l[3], l[4]) for l in g["links"]}
        self.assertIn((by_type["LoadImage"]["id"], 0,
                       by_type["UltimateSDUpscale"]["id"], 0), pairs)
        self.assertIn((by_type["UpscaleModelLoader"]["id"], 0,
                       by_type["UltimateSDUpscale"]["id"], 1), pairs)
        self.assertIn((by_type["UltimateSDUpscale"]["id"], 0,
                       by_type["SaveImage"]["id"], 0), pairs)

    def test_round_trips_through_graph_to_api(self):
        g = livewall.api_to_graph(self.API)
        back, errs = livewall.graph_to_api(g)
        self.assertEqual(errs, [], errs)
        self.assertEqual(len(back), len(self.API))
        save = [n for n in back.values()
                if n["class_type"] == "SaveImage"][0]
        self.assertEqual(save["inputs"]["images"], ["3", 0])
        self.assertEqual(save["inputs"]["filename_prefix"],
                         "steamtheme_ab12")

    def test_widget_values_are_never_null(self):
        api = {"1": {"class_type": "LoadImage", "inputs": {}},
               "2": {"class_type": "SaveImage",
                     "inputs": {"images": ["1", 0]}}}
        g = livewall.api_to_graph(api)
        for n in g["nodes"]:
            for w in n["widgets_values"]:
                self.assertIsNotNone(w)

    def test_object_info_orders_slots_and_fills_defaults(self):
        defs = {
            "LoadImage": {"input": {"required": {
                "image": [["a.png", "b.png"], {}],
                "upload": [["image"], {}]}},
                "output": ["IMAGE", "MASK"],
                "output_name": ["IMAGE", "MASK"]},
            "SaveImage": {"input": {"required": {
                "images": ["IMAGE", {}],
                "filename_prefix": ["STRING", {"default": "ComfyUI"}]}},
                "output": [], "output_name": []},
        }
        api = {"1": {"class_type": "LoadImage", "inputs": {"image": "a.png"}},
               "2": {"class_type": "SaveImage",
                     "inputs": {"images": ["1", 0]}}}
        g = livewall.api_to_graph(api, defs)
        save = [n for n in g["nodes"] if n["type"] == "SaveImage"][0]
        self.assertEqual(save["inputs"][0]["name"], "images")
        self.assertEqual(save["inputs"][0]["link"], 1)
        self.assertEqual(save["widgets_values"], ["ComfyUI"])
        load = [n for n in g["nodes"] if n["type"] == "LoadImage"][0]
        self.assertEqual(load["widgets_values"], ["a.png", "image"])

    def test_meta_embeds_a_configured_graph_verbatim(self):
        graph = {"nodes": [{"id": 1, "type": "LoadImage"}], "links": [],
                 "version": 0.4}
        meta = livewall.workflow_meta(json.dumps(graph), {}, "http://x:1")
        self.assertEqual(meta, graph)

    def test_meta_synthesizes_for_an_api_workflow(self):
        # unreachable server -> definition-less synthesis, still a graph
        meta = livewall.workflow_meta("", self.API, "http://127.0.0.1:1")
        self.assertEqual(meta["version"], 0.4)
        self.assertEqual(len(meta["nodes"]), 4)


class TestWorkflowValidation(LiveWallFixture):
    def _api_wf(self, **extra):
        wf = {"1": {"class_type": "LoadImage", "inputs": {"image": "h.jpg"}},
              "2": {"class_type": "FramesNode", "inputs": {"image": ["1", 0]}},
              "3": {"class_type": "CreateVideo",
                    "inputs": {"images": ["2", 0], "fps": 24}},
              "4": {"class_type": "SaveVideo",
                    "inputs": {"video": ["3", 0]}}}
        wf.update(extra)
        return json.dumps(wf)

    def test_api_format_accepted_untouched(self):
        api, err = livewall.parse_workflow(self._api_wf())
        self.assertIsNone(err, err)
        self.assertEqual(len(api), 4)
        self.assertNotIn("steamtheme_create", api)

    def test_empty_text_is_the_default(self):
        a1, e1 = livewall.parse_workflow("")
        a2, e2 = livewall.parse_workflow(livewall.default_workflow())
        self.assertIsNone(e1)
        self.assertEqual(set(a1), set(a2))

    def test_missing_loadimage_rejected(self):
        _, err = livewall.parse_workflow(json.dumps(
            {"1": {"class_type": "SaveVideo", "inputs": {}}}))
        self.assertIn("LoadImage", err)

    def test_bad_json_rejected(self):
        _, err = livewall.parse_workflow("{nope")
        self.assertIn("invalid JSON", err)

    def test_unquoted_placeholder_tokens_auto_quoted(self):
        # a hand-edited workflow with a bare {loop_prompt} in widgets_values
        # (people type placeholders straight into the JSON) must still parse
        wf = ('{"nodes": [{"id": 1, "type": "LoadImage", "mode": 0, '
              '"inputs": [], "widgets_values": ["h.jpg"]},'
              ' {"id": 2, "type": "FramesNode", "mode": 0, "inputs": [],'
              ' "widgets_values": [{loop_prompt}, false, 5],'
              ' "widgets_values_named": {"value": {loop_prompt}}}], '
              '"links": [[1, 1, 0, 2, 0, "IMAGE"]]}')
        logs = []
        api, err = livewall.parse_workflow(wf, log=logs.append)
        self.assertIsNone(err, err)
        self.assertEqual(api["2"]["inputs"]["value"], "{loop_prompt}")
        self.assertTrue(any("auto-quoted" in m for m in logs))

    def test_quoted_placeholders_are_left_alone(self):
        wf = self._api_wf()
        wf = wf.replace('"image": "h.jpg"',
                        '"image": "h.jpg", "note": '
                        '"{game} key art, {mood} atmosphere"')
        api, err = livewall.parse_workflow(wf)
        self.assertIsNone(err, err)
        self.assertEqual(api["1"]["inputs"]["note"],
                         "{game} key art, {mood} atmosphere")

    def test_repair_does_not_touch_strings(self):
        # a QUOTED prompt full of placeholders must survive the repair
        # scan byte-for-byte (the corruption case that motivated it)
        text = '{"a": "{loop_prompt} {game} {mood}", "b": {loop_prompt}}'
        fixed, n = livewall.repair_json(text)
        self.assertEqual(n, 1)
        self.assertEqual(json.loads(fixed)["a"],
                         "{loop_prompt} {game} {mood}")

    def test_unrepairable_json_still_rejected(self):
        _, err = livewall.parse_workflow('{"a": {"b": 1,}}')
        self.assertIn("invalid JSON", err)

    def test_auto_append_when_no_saver(self):
        # ends in a bare IMAGE producer -> CreateVideo + SaveVideo appended
        api, err = livewall.parse_workflow(json.dumps(
            {"1": {"class_type": "LoadImage", "inputs": {"image": "h.jpg"}},
             "2": {"class_type": "FramesNode", "inputs": {"image": ["1", 0]}}}))
        self.assertIsNone(err, err)
        self.assertIn("steamtheme_create", api)
        self.assertIn("steamtheme_save", api)
        self.assertEqual(api["steamtheme_create"]["inputs"]["images"],
                         ["2", 0])
        self.assertEqual(api["steamtheme_save"]["inputs"]["video"],
                         ["steamtheme_create", 0])

    def test_no_terminal_image_rejected(self):
        # every IMAGE output is consumed by a non-saver node -> nothing to
        # wire CreateVideo from
        _, err = livewall.parse_workflow(json.dumps(
            {"1": {"class_type": "LoadImage", "inputs": {"image": "h.jpg"}},
             "2": {"class_type": "FramesNode", "inputs": {"image": ["1", 0]}},
             "3": {"class_type": "SaveImage", "inputs": {"images": ["2", 0]}}}))
        self.assertIn("SaveVideo", err)

    def test_muted_graph_nodes_excluded(self):
        graph = {"nodes": [
            {"id": 1, "type": "LoadImage", "mode": 0, "inputs": [],
             "widgets_values_named": {"image": "h.jpg"}},
            {"id": 2, "type": "FramesNode", "mode": 2, "inputs": [],
             "widgets_values_named": {}}],
            "links": []}
        api, err = livewall.parse_workflow(json.dumps(graph))
        self.assertIsNone(err, err)
        self.assertEqual(list(api), ["1", "steamtheme_create",
                                     "steamtheme_save"])


class TestOriginalHeroSelection(LiveWallFixture):
    """The video stage must use the ORIGINAL art — upscaled variants are
    big uploads and the workflow downsamples to its output size anyway."""

    def test_upscaled_maps_back_to_source(self):
        src = os.path.join(self.dir, "hero_1.png")
        from PIL import Image
        Image.new("RGB", (16, 16)).save(src)
        up = os.path.join(self.dir,
                          "hero_1_upscaled_comfy-2d882e83.png")
        Image.new("RGB", (256, 256)).save(up)
        self.assertEqual(livewall._original_hero(up), src)

    def test_plain_hero_passes_through(self):
        src = os.path.join(self.dir, "hero_2.jpg")
        self.assertEqual(livewall._original_hero(src), src)
        self.assertEqual(livewall._original_hero(None), None)

    def test_missing_source_keeps_upscaled(self):
        up = os.path.join(self.dir, "hero_9_upscaled_x.png")
        self.assertEqual(livewall._original_hero(up), up)

    def test_maybe_generate_queues_the_original(self):
        # given an upscaled path, the queued job's source is the original
        from PIL import Image
        src = os.path.join(self.dir, "hero_1.png")
        Image.new("RGB", (16, 16), (1, 2, 3)).save(src)
        up = os.path.join(self.dir, "hero_1_upscaled_comfy-x.png")
        Image.new("RGB", (64, 64), (9, 9, 9)).save(up)
        livewall.maybe_generate(self.cfg, up, {}, "440", "T", "",
                                lambda m: None)
        with open(os.path.join(self.dir, "video_queue.json"),
                  encoding="utf-8") as f:
            q = json.load(f)
        self.assertEqual(q[0]["src"], src)


class TestLiveWallQueue(LiveWallFixture):
    def setUp(self):
        super().setUp()
        self.qfile = os.path.join(self.dir, "video_queue.json")

    def _q(self):
        with open(self.qfile, encoding="utf-8") as f:
            return json.load(f)

    def test_defers_and_dedupes_while_gaming(self):
        with mock.patch.object(livewall, "_game_running", return_value=True):
            d1 = os.path.join(self.dir, "v1.mp4")
            d2 = os.path.join(self.dir, "v1.mp4")
            o1 = livewall.generate(self.cfg, self.hero, d1, {}, lambda m: None)
            o2 = livewall.generate(self.cfg, self.hero, d2, {}, lambda m: None)
        self.assertIsNone(o1)
        self.assertIsNone(o2)
        q = self._q()
        self.assertEqual(len(q), 1)          # deduped by dst
        self.assertEqual(q[0]["hero"], self.hero)

    def test_drain_drops_poison_jobs(self):
        dst = os.path.join(self.dir, "v.mp4")
        with mock.patch.object(livewall, "_game_running", return_value=True):
            livewall.generate(self.cfg, self.hero, dst, {}, lambda m: None)
        with mock.patch.object(livewall, "_game_running",
                               return_value=False), \
                mock.patch.object(livewall, "generate",
                                  lambda *a, **k: None), \
                mock.patch.object(livewall, "requests") as rq:
            rq.get.side_effect = _idle_get
            for _ in range(3):
                livewall.drain_queue(self.cfg, lambda m: None)
        self.assertEqual(self._q(), [])

    def test_drain_noop_when_disabled(self):
        self.cfg["livewall"]["enabled"] = False
        self.assertEqual(livewall.drain_queue(self.cfg, lambda m: None), [])

    def test_excluded_running_app_is_not_a_game(self):
        # Wallpaper Engine (431960, excluded) running via Steam's now-playing
        # registry must NOT count as a running game — counting it would
        # defer every queued upscale/video job forever
        import steamdetect
        import upscale
        with mock.patch.object(steamdetect, "get_running_appid",
                               lambda: 431960), \
                mock.patch.object(steamdetect, "find_running_custom_game",
                                  lambda c: None):
            self.assertTrue(upscale._game_running({"exclude_appids": []}))
            self.assertFalse(upscale._game_running(
                {"exclude_appids": [431960]}))
            # a real, non-excluded game still counts
            with mock.patch.object(steamdetect, "get_running_appid",
                                   lambda: 570):
                self.assertTrue(upscale._game_running(
                    {"exclude_appids": [431960]}))


def _idle_get(url, **kw):
    if url.endswith("/queue"):
        return mock.Mock(status_code=200,
                         json=lambda: {"queue_running": [],
                                       "queue_pending": []})
    return mock.Mock(status_code=200, json=lambda: {})


def _write(dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "wb") as f:
        f.write(_mp4_bytes())
    return dst


class TestLiveWallSubmit(LiveWallFixture):
    """generate() against a mocked ComfyUI: object_info filtering, hero
    upload, LoadImage patching, placeholder fill, submit -> history -> view."""

    WF = json.dumps({
        "1": {"class_type": "LoadImage", "inputs": {"image": "h.jpg"}},
        "2": {"class_type": "FramesNode", "inputs": {"image": ["1", 0]}},
        "3": {"class_type": "CreateVideo", "inputs": {"images": ["2", 0],
                                                      "fps": 24}},
        "4": {"class_type": "SaveVideo", "inputs": {"video": ["3", 0]}}})

    def setUp(self):
        super().setUp()
        self.cfg["livewall"]["workflow"] = self.WF
        self.cfg["livewall"]["defer_while_gaming"] = False
        self.dst = os.path.join(self.dir, "v.mp4")
        self.submitted = None

    def _oi(self, url, **kw):
        ct = url.rsplit("/", 1)[-1]
        return mock.Mock(status_code=200, text="",
                         json=lambda: {ct: {"input": {
                             "required": {"image": ["IMAGE", {}]} if ct
                             == "LoadImage" else {}}}})

    def _post(self, url, **kw):
        if url.endswith("/upload/image"):
            return mock.Mock(status_code=200, text="",
                             json=lambda: {"name": "u.jpg"})
        if url.endswith("/prompt"):
            self.submitted = kw["json"]["prompt"]
            return mock.Mock(status_code=200, text="",
                             json=lambda: {"prompt_id": "p1"})
        raise AssertionError(url)

    def _get(self, url, **kw):
        if "/history/" in url:
            return mock.Mock(status_code=200, text="", json=lambda: {
                "p1": {"status": {"completed": True, "status_str": "success"},
                       "outputs": {"4": {"videos": [
                           {"filename": "steamtheme_loop_00001_.mp4",
                            "subfolder": "", "type": "output"}]}}}})
        if url.endswith("/view"):
            return mock.Mock(status_code=200, content=_mp4_bytes())
        if "/object_info/" in url:
            return self._oi(url, **kw)
        raise AssertionError(url)

    def test_happy_path(self):
        import requests as real_requests
        with mock.patch.object(livewall, "requests") as rq, \
                mock.patch.object(livewall.time, "sleep"):
            rq.RequestException = real_requests.RequestException
            rq.get.side_effect = self._get
            rq.post.side_effect = self._post
            out = livewall.generate(self.cfg, self.hero, self.dst,
                                    {"game": "Hades II",
                                     "mood": "dark"}, lambda m: None)
        self.assertEqual(out, self.dst)
        with open(self.dst, "rb") as f:
            self.assertIn(b"ftyp", f.read()[:16])
        # hero uploaded and patched into every LoadImage
        self.assertTrue(self.submitted["1"]["inputs"]["image"]
                        .startswith("steamtheme_"))
        # SaveVideo got our filename prefix
        self.assertTrue(self.submitted["4"]["inputs"]["filename_prefix"]
                        .startswith("steamtheme/"))


class TestWallpaperEngine(LiveWallFixture):
    # apply_we() is a Windows-only path (it no-ops on other platforms),
    # so its subprocess/config tests skip cleanly there — same convention
    # as the other platform-specific tests

    def test_monitor_mapping(self):
        self.assertEqual(
            livewall.we_monitor({"livewall": {"we_monitor": 2}}, None), 2)
        self.assertEqual(
            livewall.we_monitor({"livewall": {"we_monitor": -1}},
                                "\\\\.\\DISPLAY5"), 4)
        self.assertEqual(
            livewall.we_monitor({"livewall": {}}, "\\\\.\\DISPLAY1"), 0)
        self.assertEqual(livewall.we_monitor({"livewall": {}}, ""), 0)

    @unittest.skipUnless(sys.platform == "win32",
                         "Wallpaper Engine apply is Windows-only")
    def test_apply_via_control_when_running(self):
        proj = os.path.join(self.dir, "project.json")
        with open(proj, "w") as f:
            f.write("{}")
        vid = os.path.join(self.dir, "wallpaper.mp4")
        with open(vid, "wb") as f:
            f.write(_mp4_bytes())
        exe = r"C:\fake\wallpaper64.exe"
        with mock.patch.object(livewall, "we_binary", lambda: exe), \
                mock.patch.object(livewall, "publish_to_we",
                                  lambda e, p, log: vid), \
                mock.patch.object(livewall, "we_running",
                                  lambda: True), \
                mock.patch.object(livewall.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0)
            ok = livewall.apply_we(self.cfg, proj, "\\\\.\\DISPLAY1",
                                   lambda m: None)
        self.assertTrue(ok)
        args = run.call_args[0][0]
        self.assertEqual(args[0], exe)
        self.assertEqual(args[1:4], ["-control", "openWallpaper", "-file"])
        self.assertEqual(args[4], vid)      # the mp4, not project.json
        self.assertEqual(args[6], "0")     # DISPLAY1 -> monitor 0
        self.assertEqual(args[5], "-monitor")

    @unittest.skipUnless(sys.platform == "win32",
                         "Wallpaper Engine config patch is Windows-only")
    def test_apply_via_config_when_not_running(self):
        exe = os.path.join(self.dir, "wallpaper64.exe")
        with open(exe, "w") as f:
            f.write("x")
        proj = os.path.join(self.dir, "project.json")
        with open(proj, "w") as f:
            f.write("{}")
        cfgp = os.path.join(self.dir, "config.json")
        with open(cfgp, "w") as f:
            json.dump({"?installdirectory": "x", "tester": {"general": {
                "wallpaperconfig": {"selectedwallpapers": {
                    "Monitor0": {"file": None}}}}}}, f)
        vid = os.path.join(self.dir, "wallpaper.mp4")
        with open(vid, "wb") as f:
            f.write(_mp4_bytes())
        with mock.patch.dict("os.environ", {"USERNAME": "tester"}), \
                mock.patch.object(livewall, "we_binary", lambda: exe), \
                mock.patch.object(livewall, "publish_to_we",
                                  lambda e, p, log: vid), \
                mock.patch.object(livewall, "we_running", lambda: False):
            ok = livewall.apply_we(self.cfg, proj, "\\\\.\\DISPLAY1",
                                   lambda m: None)
        self.assertTrue(ok)
        with open(cfgp, encoding="utf-8") as f:
            c = json.load(f)
        got = c["tester"]["general"]["wallpaperconfig"][
            "selectedwallpapers"]["Monitor0"]["file"]
        self.assertEqual(got, vid.replace("\\", "/"))

    def test_publish_copies_into_we_myprojects(self):
        # the video lands in WE's own projects tree (like its built-in
        # video wallpapers) so cache wipes can't orphan the wallpaper
        we = os.path.join(self.dir, "wallpaper_engine")
        os.makedirs(we)
        exe = os.path.join(we, "wallpaper64.exe")
        with open(exe, "w") as f:
            f.write("x")
        projdir = os.path.join(self.dir, "proj")
        os.makedirs(projdir)
        with open(os.path.join(projdir, "wallpaper.mp4"), "wb") as f:
            f.write(_mp4_bytes())
        with open(os.path.join(projdir, "project.json"), "w") as f:
            json.dump({"file": "wallpaper.mp4", "type": "video",
                       "title": "Frostbound Vigil - The Witcher 3"}, f)
        out = livewall.publish_to_we(exe, os.path.join(projdir,
                                                       "project.json"),
                                     lambda m: None)
        self.assertTrue(os.path.isfile(out))
        self.assertIn(os.path.join("projects", "myprojects", "steam-theme"),
                      out)
        self.assertTrue(out.endswith("wallpaper.mp4"))
        self.assertTrue(os.path.isfile(os.path.join(
            os.path.dirname(out), "project.json")))

    def test_install_writes_project_and_preview(self):
        proj = livewall.install(self.cfg, os.path.join(self.dir, "v.mp4"),
                                "440", "Ashen Vigil", self.hero,
                                lambda m: None)
        with open(proj, encoding="utf-8") as f:
            p = json.load(f)
        self.assertEqual(p["file"], "v.mp4")
        self.assertEqual(p["type"], "video")
        self.assertIn("Ashen Vigil", p["title"])
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "preview.jpg")))


if __name__ == "__main__":
    unittest.main()