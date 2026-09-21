"""Core regression tests — stdlib unittest, no extra deps.

Run from the repo root:  python -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
import theme  # noqa: E402
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
