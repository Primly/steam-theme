"""Linux-side regression tests — stdlib unittest, no extra deps.

The Linux modules are import-safe on Windows (no Linux-only imports at module
level), so these run in the Windows CI too. OS interactions are either pure
parsers or are exercised through injected fakes.

Run from the repo root:  python -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import extras  # noqa: E402
import linux_theme  # noqa: E402
import openrgb_sync  # noqa: E402
import steamdetect  # noqa: E402


LOGINUSERS = """\
"users"
{
\t"76561198000000001"
\t{
\t\t"AccountName"\t\t"oldtimer"
\t\t"MostRecent"\t\t"0"
\t\t"Timestamp"\t\t"100"
\t}
\t"76561198000000002"
\t{
\t\t"AccountName"\t\t"taheen19"
\t\t"MostRecent"\t\t"1"
\t\t"Timestamp"\t\t"200"
\t}
}
"""


class TestLoginusersParsing(unittest.TestCase):
    def test_picks_most_recent(self):
        self.assertEqual(steamdetect.parse_loginusers(LOGINUSERS),
                         "76561198000000002")

    def test_falls_back_to_autologin_then_timestamp(self):
        text = LOGINUSERS.replace('"MostRecent"\t\t"1"', '"MostRecent"\t\t"0"')
        text = text.replace('"MostRecent"\t\t"0"', '"AutoLogin"\t\t"1"', 1)
        self.assertEqual(steamdetect.parse_loginusers(text),
                         "76561198000000001")
        # no MostRecent/AutoLogin flags: newest Timestamp wins
        plain = LOGINUSERS.replace('"MostRecent"\t\t"1"',
                                   '"Timestamp"\t\t"50"')
        plain = plain.replace('"MostRecent"\t\t"0"', '"AutoLogin"\t\t"0"')
        self.assertEqual(steamdetect.parse_loginusers(plain),
                         "76561198000000001")

    def test_empty_or_missing(self):
        self.assertIsNone(steamdetect.parse_loginusers(""))
        self.assertIsNone(steamdetect.parse_loginusers('"users"\n{\n}'))


class TestRunningAppidLinux(unittest.TestCase):
    def test_environ_scan(self):
        env = b"PATH=/usr/bin\0SteamAppId=292030\0SteamGameId=292030\0"
        self.assertEqual(steamdetect.appid_from_environ(env), 292030)

    def test_environ_no_match(self):
        self.assertIsNone(steamdetect.appid_from_environ(b"PATH=/usr/bin\0"))
        self.assertIsNone(steamdetect.appid_from_environ(b"SteamAppId=0\0"))

    def test_registry_vdf(self):
        text = '"HKCU"\n{\n\t"RunningAppID"\t\t"1245620"\n}\n'
        self.assertEqual(steamdetect.parse_running_appid_vdf(text), 1245620)
        self.assertIsNone(steamdetect.parse_running_appid_vdf(
            '"RunningAppID"\t\t"0"'))
        self.assertIsNone(steamdetect.parse_running_appid_vdf("nothing"))


KSCREEN = {
    "outputs": [
        {"name": "HDMI-A-1", "connected": True, "enabled": True,
         "pos": {"x": 1920, "y": 0}, "size": {"width": 2560, "height": 1440},
         "priority": 1, "scale": 1.65},
        {"name": "eDP-1", "connected": True, "enabled": True,
         "pos": {"x": 0, "y": 0}, "size": {"width": 1920, "height": 1080},
         "priority": 2, "scale": 1.25},
        {"name": "DP-1", "connected": True, "enabled": False,
         "pos": {"x": 0, "y": 0}, "size": {"width": 800, "height": 600},
         "priority": 3, "scale": 1.0},
        {"name": "DP-2", "connected": False, "enabled": False,
         "pos": {"x": 0, "y": 0}, "size": {"width": 800, "height": 600},
         "priority": 4, "scale": 1.0},
    ]
}


class TestKscreenParsing(unittest.TestCase):
    def test_enabled_connected_only(self):
        mons = linux_theme.parse_kscreen_outputs(KSCREEN)
        self.assertEqual([m["device"] for m in mons], ["HDMI-A-1", "eDP-1"])

    def test_rect_and_scale(self):
        mons = linux_theme.parse_kscreen_outputs(KSCREEN)
        hdmi = mons[0]
        self.assertEqual(hdmi["rect"], (1920, 0, 4480, 1440))
        self.assertEqual(hdmi["scale"], 1.65)
        self.assertTrue(hdmi["primary"])
        self.assertFalse(mons[1]["primary"])


class TestPlasmaScriptBuilding(unittest.TestCase):
    def test_script_targets_screens(self):
        s = linux_theme._wallpaper_script({0: "/cache/x/wallpaper_hero_1.jpg"})
        self.assertIn("file:///cache/x/wallpaper_hero_1.jpg", s)
        self.assertIn("writeConfig('Image'", s)
        self.assertIn("d.screen", s)

    def test_script_path_cannot_break_js_string(self):
        evil = '/tmp/x");malware();//.jpg'
        s = linux_theme._wallpaper_script({0: evil})
        # json.dumps backslash-escapes the quotes — the path must not appear
        # as a raw unescaped JS string break
        self.assertNotIn('"' + evil, s)
        self.assertIn('\\"', s)


class TestLinuxThemeManifest(unittest.TestCase):
    def test_write_theme_file_maps_roles(self):
        with tempfile.TemporaryDirectory() as d:
            for role in ("hero", "logo"):
                p = os.path.join(d, f"wallpaper_{role}_20260101_000000.jpg")
                with open(p, "wb") as f:
                    f.write(b"x")
            hero = os.path.join(d, "wallpaper_hero_20260101_000000.jpg")
            out = os.path.join(d, "theme.json")
            linux_theme.write_theme_file(out, "My Theme", hero,
                                         {"accent": "#66C0F4",
                                          "appearance": "dark"})
            with open(out, encoding="utf-8") as f:
                manifest = json.load(f)
        self.assertEqual(manifest["name"], "My Theme")
        self.assertEqual(sorted(manifest["wallpapers"]), ["hero", "logo"])
        self.assertEqual(manifest["accent"], "#66C0F4")

    def test_manifest_name_is_ini_safe(self):
        with tempfile.TemporaryDirectory() as d:
            hero = os.path.join(d, "wallpaper_hero_1.jpg")
            with open(hero, "wb") as f:
                f.write(b"x")
            out = os.path.join(d, "theme.json")
            linux_theme.write_theme_file(out, "Bad\nName\r\nInjected=1", hero,
                                         {})
            with open(out, encoding="utf-8") as f:
                manifest = json.load(f)
        self.assertNotIn("\n", manifest["name"])


class TestKdeColors(unittest.TestCase):
    def test_dark_scheme_with_accent(self):
        calls = []

        class R:
            returncode = 0
            stderr = ""
            stdout = ""

        with mock.patch.object(linux_theme, "_run",
                               lambda a, timeout=30: calls.append(a) or R()):
            linux_theme.write_registry_colors(
                {"accent": "#66C0F4", "appearance": "dark"}, lambda m: None,
                "match", "match")
        # scheme and accent must be SEPARATE invocations — a combined call
        # silently skips the scheme on Plasma 6.7
        self.assertEqual(calls[0], ["plasma-apply-colorscheme", "BreezeDark"])
        self.assertEqual(calls[1], ["plasma-apply-colorscheme",
                                    "--accent-color", "#66C0F4"])

    def test_bad_accent_not_forwarded(self):
        calls = []

        class R:
            returncode = 0
            stderr = ""
            stdout = ""

        with mock.patch.object(linux_theme, "_run",
                               lambda a, timeout=30: calls.append(a) or R()):
            linux_theme.write_registry_colors(
                {"accent": 'red"; rm -rf ~', "appearance": "light"},
                lambda m: None, "match", "match")
        self.assertEqual(calls, [["plasma-apply-colorscheme", "BreezeLight"]])

    def test_system_mode_wins_over_app_mode(self):
        calls = []

        class R:
            returncode = 0
            stderr = ""
            stdout = ""

        with mock.patch.object(linux_theme, "_run",
                               lambda a, timeout=30: calls.append(a) or R()):
            linux_theme.write_registry_colors(
                {"accent": "#112233", "appearance": "dark"}, lambda m: None,
                "light", "dark")
        self.assertIn("BreezeLight", calls[0])


class TestKonsoleScheme(unittest.TestCase):
    PAL = {"accent": "#66C0F4", "background": "#101418",
           "appearance": "dark",
           "colors": [f"#{i:02x}{i:02x}{i:02x}" for i in range(16)]}

    def test_scheme_contents(self):
        out = extras.render_konsole_colorscheme("Ashen Vigil", self.PAL)
        self.assertIn("[General]", out)
        self.assertIn("Description=Ashen Vigil", out)
        self.assertIn("Color=16,20,24", out)          # background as r,g,b
        self.assertIn("[Color7Intense]", out)

    def test_name_injection_stripped(self):
        out = extras.render_konsole_colorscheme(
            "X\n[evil]\nColor=1,2,3", self.PAL)
        # newlines are stripped, so the injected text collapses into the
        # Description VALUE — it can never become its own INI section
        self.assertFalse(any(line.strip() == "[evil]"
                             for line in out.splitlines()))
        self.assertTrue(any(line.startswith("Description=X[evil]")
                            for line in out.splitlines()))

    def test_bad_color_rejected(self):
        with self.assertRaises(ValueError):
            extras._hex_to_rgb_csv("red")
        self.assertEqual(extras._hex_to_rgb_csv("#66C0F4"), "102,192,244")


class TestTerminalDispatch(unittest.TestCase):
    def test_linux_dispatches_to_konsole(self):
        with mock.patch.object(extras.sys, "platform", "linux"), \
                mock.patch.object(extras, "apply_konsole") as kon, \
                mock.patch.object(extras, "apply_windows_terminal") as wt:
            extras.apply_terminal({}, {}, "Name", None, lambda m: None)
        kon.assert_called_once()
        wt.assert_not_called()

    def test_windows_dispatches_to_wt(self):
        with mock.patch.object(extras.sys, "platform", "win32"), \
                mock.patch.object(extras, "apply_konsole") as kon, \
                mock.patch.object(extras, "apply_windows_terminal") as wt:
            extras.apply_terminal({}, {}, "Name", None, lambda m: None)
        wt.assert_called_once()
        kon.assert_not_called()


class FakeRGBColor:
    def __init__(self, r, g, b):
        self.r, self.g, self.b = r, g, b


class FakeMode:
    def __init__(self, name):
        self.name = name


class FakeDevice:
    def __init__(self, name, modes, n_leds):
        self.name = name
        self.modes = modes
        self.leds = [object()] * n_leds
        self.mode_set = None
        self.colors_set = None

    def set_mode(self, idx):
        self.mode_set = idx

    def set_colors(self, colors):
        assert len(colors) == len(self.leds)
        self.colors_set = colors


class TestOpenRgbSync(unittest.TestCase):
    PAL = {"accent": "#66C0F4", "colors": ["#112233", "#445566"]}

    def _client(self, devices):
        mod = types.ModuleType("openrgb")
        utils = types.ModuleType("openrgb.utils")
        utils.RGBColor = FakeRGBColor

        class FakeClient:
            def __init__(self, host, port, name=None):
                self.devices = devices

            def disconnect(self):
                pass

        mod.OpenRGBClient = FakeClient
        return {"openrgb": mod, "openrgb.utils": utils}

    def test_pushes_palette_to_direct_devices(self):
        direct = FakeDevice("KB", [FakeMode("Rainbow"), FakeMode("Direct")], 5)
        nodirect = FakeDevice("Fan", [FakeMode("Rainbow")], 3)
        logs = []
        with mock.patch.dict(sys.modules, self._client([direct, nodirect])):
            openrgb_sync.apply_openrgb(
                {"openrgb": {"enabled": True}}, self.PAL, logs.append)
        self.assertEqual(direct.mode_set, 1)
        self.assertEqual(len(direct.colors_set), 5)
        # accent first
        self.assertEqual((direct.colors_set[0].r, direct.colors_set[0].g,
                          direct.colors_set[0].b), (0x66, 0xC0, 0xF4))
        # palette cycles
        self.assertEqual(direct.colors_set[1].r, 0x11)
        self.assertIsNone(nodirect.colors_set)
        self.assertTrue(any("1 device" in m for m in logs))

    def test_disabled_is_noop(self):
        openrgb_sync.apply_openrgb({"openrgb": {"enabled": False}},
                                   self.PAL, lambda m: None)

    def test_unreachable_server_logged_not_raised(self):
        mod = types.ModuleType("openrgb")
        utils = types.ModuleType("openrgb.utils")
        utils.RGBColor = FakeRGBColor

        class Boom:
            def __init__(self, *a, **k):
                raise ConnectionRefusedError("nope")

        mod.OpenRGBClient = Boom
        logs = []
        with mock.patch.dict(sys.modules, {"openrgb": mod,
                                           "openrgb.utils": utils}):
            openrgb_sync.apply_openrgb({"openrgb": {"enabled": True}},
                                       self.PAL, logs.append)
        self.assertTrue(any("not reachable" in m for m in logs))


if __name__ == "__main__":
    unittest.main()
