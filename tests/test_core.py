"""Core regression tests — stdlib unittest, no extra deps.

Run from the repo root:  python -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
import theme  # noqa: E402
import extras  # noqa: E402
import srgb_effects  # noqa: E402
import steamdetect  # noqa: E402


class TestPathSandbox(unittest.TestCase):
    def test_safe_join_accepts_children(self):
        base = tempfile.mkdtemp()
        self.assertTrue(main.safe_join(base, "a", "b.txt").startswith(base))

    def test_safe_join_rejects_traversal(self):
        base = tempfile.mkdtemp()
        with self.assertRaises(ValueError):
            main.safe_join(base, "..", "escape.txt")
        with self.assertRaises(ValueError):
            main.safe_join(base, "..", "..", "Windows", "win.ini")

    def test_safe_join_rejects_absolute(self):
        base = tempfile.mkdtemp()
        with self.assertRaises(ValueError):
            main.safe_join(base, "C:\\Windows\\System32\\drivers")

    def test_cfg_path_falls_back_to_default(self):
        cfg = {"cache_dir": "..\\..\\evil"}
        self.assertEqual(main.cfg_path(cfg, "cache_dir", "cache"),
                         os.path.join(main.BASE_DIR, "cache"))

    def test_cfg_path_resolves_normal_values(self):
        cfg = {"log_file": "logs\\service.log"}
        self.assertTrue(main.cfg_path(cfg, "log_file", "service.log")
                        .endswith("logs\\service.log"))


class TestIniSanitizer(unittest.TestCase):
    def test_strips_newlines(self):
        # a VLM theme name must not be able to inject INI keys
        evil = "Nice Theme\r\n[Control Panel\\Desktop]\r\nSCRNSAVE.EXE=C:\\x.scr"
        out = theme._ini_safe(evil)
        self.assertNotIn("\n", out)
        self.assertNotIn("\r", out)

    def test_strips_control_chars_and_limits_length(self):
        out = theme._ini_safe("ok\x00\x1f" + "a" * 500)
        self.assertLessEqual(len(out), 128)
        self.assertNotIn("\x00", out)

    def test_theme_file_has_no_injected_lines(self):
        pal = {"accent": "#AABBCC"}
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.theme")
            theme.write_theme_file(path, "Good\r\nSCRNSAVE.EXE=evil", d, pal)
            with open(path, encoding="utf-8") as f:
                content = f.read()
        # the injected text must never appear as an INI key (line start)
        self.assertFalse(any(line.startswith("SCRNSAVE")
                             for line in content.splitlines()))
        self.assertIn("DisplayName=Good", content)


class TestSrgbEffects(unittest.TestCase):
    PAL = {"accent": "#66C0F4", "colors": ["#112233", "#445566", "#778899"]}

    def test_all_styles_render(self):
        for style in srgb_effects.STYLES:
            html = srgb_effects.render_effect(style, "Steam Theme", self.PAL)
            self.assertIn("<canvas", html)
            self.assertIn("#66C0F4", html)          # accent baked in
            self.assertIn("onCanvasTapped", html)   # keypress layer present
            self.assertIn("ensureInit", html)       # load-order guard present

    def test_unknown_style_falls_back_to_gradient(self):
        html = srgb_effects.render_effect("nope", "Steam Theme", self.PAL)
        self.assertIn("direction", html)  # gradient's meta property

    def test_palette_color_injection_neutralized(self):
        # a tampered cache/palette.json must not inject script into the
        # SignalRGB effect (an HTML/JS file SignalRGB executes)
        evil = {"accent": '#66C0F4"><script>alert(1)</script>',
                "colors": ["#112233", 'x" onload="y']}
        html = srgb_effects.render_effect("solid", "Steam Theme", evil)
        self.assertNotIn("alert(1)", html)
        self.assertNotIn("onload", html)
        self.assertNotIn('\"><script>', html)  # no attribute breakout
        self.assertIn('#66C0F4', html)  # sanitized accent kept

    def test_short_palette_is_padded(self):
        html = srgb_effects.render_effect("solid", "Steam Theme",
                                          {"accent": "#ABCDEF", "colors": []})
        self.assertIn("#ABCDEF", html)


class TestSteamDetectShapes(unittest.TestCase):
    def test_public_game_shape(self):
        g = steamdetect._public_game({"appid": 440, "name": "TF2",
                                      "rtime_last_played": 123,
                                      "img_icon_url": "abc"})
        self.assertEqual(g["appid"], 440)
        self.assertTrue(g["icon_url"].endswith("/440/abc.jpg"))

    def test_public_game_without_icon(self):
        g = steamdetect._public_game({"appid": 1})
        self.assertIsNone(g["icon_url"])
        self.assertEqual(g["rtime_last_played"], 0)

    def test_custom_game_empty_config(self):
        self.assertIsNone(steamdetect.find_running_custom_game([]))
        self.assertIsNone(steamdetect.find_running_custom_game(None))


class TestEffectTitles(unittest.TestCase):
    def test_per_game_title(self):
        cfg = {"signalrgb": {"effect_scope": "per_game"}}
        pal = {"game_name": "The Witcher 3: Wild Hunt", "theme_name": "Ashen Vigil"}
        self.assertEqual(extras.effect_title(cfg, pal),
                         "Steam Theme - The Witcher 3_ Wild Hunt")

    def test_single_scope(self):
        cfg = {"signalrgb": {"effect_scope": "single"}}
        self.assertEqual(extras.effect_title(cfg, {"game_name": "X"}),
                         "Steam Theme")

    def test_default_scope_per_game_with_theme_name_fallback(self):
        self.assertEqual(extras.effect_title({}, {"theme_name": "Ashen Vigil"}),
                         "Steam Theme - Ashen Vigil")
        self.assertEqual(extras.effect_title({}, {}), "Steam Theme")

    def test_safe_name_strips_forbidden_chars(self):
        # Windows-forbidden: < > : " / \ | ? *
        self.assertEqual(extras._safe_title_name("a<b>/c"), "a_b__c")
        self.assertEqual(extras._safe_title_name("Game: The \"Sequel\""),
                         "Game_ The _Sequel_")
        self.assertEqual(extras._safe_title_name("..."), "Game")


class TestManualThemeHold(unittest.TestCase):
    """A theme manually applied from the gallery sets state.manual_hold;
    check_once must not revert it to the last-played game until the user
    actually plays something new."""

    GAMES = [{"appid": 440, "name": "TF2", "rtime_last_played": 1000,
              "icon_url": None},
             {"appid": 570, "name": "Dota 2", "rtime_last_played": 2000,
              "icon_url": None}]

    def setUp(self):
        self.tmp = tempfile.mkdtemp(dir=main.BASE_DIR)
        rel = os.path.relpath(self.tmp, main.BASE_DIR)
        self.cfg = {"steam_api_key": "k", "steam_id64": "1",
                    "cache_dir": rel,
                    "state_file": os.path.join(rel, "state.json"),
                    "log_file": os.path.join(rel, "service.log"),
                    "now_playing": True, "custom_games": [],
                    "exclude_appids": []}
        self.hold_at = time.time()
        main.save_state(self.cfg, {
            "last_identity": "440", "last_cache_key": "440",
            "last_appid": 440, "theme_path": "x",
            "manual_hold": {"identity": "440", "name": "TF2",
                            "held_at": self.hold_at}})
        self.pipeline_calls = []
        patches = [
            mock.patch.object(main.steamdetect, "get_owned_games",
                              lambda k, s: list(self.GAMES)),
            mock.patch.object(main.steamdetect, "get_running_appid",
                              lambda: None),
            mock.patch.object(main.steamdetect, "find_running_custom_game",
                              lambda c: None),
            mock.patch.object(main, "run_pipeline", self._fake_pipeline),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._cleanup_tmp)

    def _cleanup_tmp(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_pipeline(self, cfg, game, log, dry_run=False):
        self.pipeline_calls.append(game)
        return {"theme_path": "x", "palette": {}, "wallpaper": "w"}

    def _check(self, **kw):
        main.check_once(self.cfg, lambda m: None, **kw)
        return main.load_state(self.cfg)

    def test_stale_detection_does_not_revert_hold(self):
        # Dota 2 was last played BEFORE the hold — poller must stay put
        state = self._check()
        self.assertEqual(self.pipeline_calls, [])
        self.assertEqual(state["manual_hold"]["identity"], "440")
        self.assertEqual(state["last_identity"], "440")

    def test_playing_another_game_releases_hold(self):
        games = [dict(self.GAMES[0]),
                 dict(self.GAMES[1], rtime_last_played=self.hold_at + 60)]
        with mock.patch.object(main.steamdetect, "get_owned_games",
                               lambda k, s: games):
            state = self._check()
        self.assertEqual([g["appid"] for g in self.pipeline_calls], [570])
        self.assertNotIn("manual_hold", state)
        self.assertEqual(state["last_identity"], "570")

    def test_running_another_game_releases_hold(self):
        with mock.patch.object(main.steamdetect, "get_running_appid",
                               lambda: 570):
            state = self._check()
        self.assertEqual([g["appid"] for g in self.pipeline_calls], [570])
        self.assertNotIn("manual_hold", state)

    def test_detecting_the_held_game_clears_hold_quietly(self):
        # last-played is the held game itself — hold is moot, no re-theme
        games = [dict(self.GAMES[0], rtime_last_played=self.hold_at + 60),
                 dict(self.GAMES[1], rtime_last_played=1000)]
        with mock.patch.object(main.steamdetect, "get_owned_games",
                               lambda k, s: games):
            state = self._check()
        self.assertEqual(self.pipeline_calls, [])
        self.assertNotIn("manual_hold", state)

    def test_forced_rerun_targets_held_theme_not_detected_game(self):
        state = self._check(force=True)
        self.assertEqual([g["appid"] for g in self.pipeline_calls], [440])
        self.assertNotIn("manual_hold", state)
        self.assertEqual(state["last_identity"], "440")


class TestReapplyFromCacheHold(unittest.TestCase):
    def test_gallery_apply_sets_manual_hold(self):
        tmp = tempfile.mkdtemp(dir=main.BASE_DIR)
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp,
                                                            ignore_errors=True))
        rel = os.path.relpath(tmp, main.BASE_DIR)
        cfg = {"cache_dir": rel, "state_file": os.path.join(rel, "state.json"),
               "log_file": os.path.join(rel, "service.log")}
        cache = os.path.join(tmp, "440")
        os.makedirs(cache)
        with open(os.path.join(cache, "theme.theme"), "w") as f:
            f.write("[Theme]")
        with open(os.path.join(cache, "palette.json"), "w") as f:
            json.dump({"theme_name": "Ashen Vigil", "game_name": "Witcher 3"},
                      f)
        with mock.patch.object(main.theme_mod, "write_registry_colors"), \
                mock.patch.object(main.theme_mod, "apply_theme"), \
                mock.patch.object(main.extras, "apply_windows_terminal"), \
                mock.patch.object(main.extras, "apply_signalrgb"), \
                mock.patch.object(main.time, "sleep"):
            main.reapply_from_cache(cfg, lambda m: None, "440")
        state = main.load_state(cfg)
        self.assertEqual(state["manual_hold"]["identity"], "440")
        self.assertEqual(state["manual_hold"]["name"], "Witcher 3")
        self.assertGreater(state["manual_hold"]["held_at"], 0)
        self.assertEqual(state["last_identity"], "440")


class TestConfigLoading(unittest.TestCase):
    def test_example_config_is_valid_and_placeholder_blanked(self):
        # simulate a fresh checkout: no config.json next to the code? there is
        # one here, so just validate the example file directly
        with open(os.path.join(main.BASE_DIR, "config.example.json"),
                  encoding="utf-8") as f:
            cfg = json.load(f)
        self.assertTrue(cfg["steam_api_key"].startswith("YOUR_"))
        self.assertIn("signalrgb", cfg)
        self.assertIn("now_playing", cfg)
        self.assertIn("custom_games", cfg)


if __name__ == "__main__":
    unittest.main()
