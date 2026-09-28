"""Core regression tests — stdlib unittest, no extra deps.

Run from the repo root:  python -m unittest discover -s tests -v
"""

import glob
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
import artwork  # noqa: E402
import wallpaper  # noqa: E402
import extras  # noqa: E402
import srgb_effects  # noqa: E402
import steamdetect  # noqa: E402
import upscale  # noqa: E402

if sys.platform == "win32":
    import theme  # noqa: E402  (Windows-only module)
else:
    theme = None


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
            # filesystem root is absolute on every platform
            main.safe_join(base, os.path.abspath(os.sep))

    def test_safe_join_windows_separators(self):
        # Windows-only: backslash is a separator there, so '..\\..' escapes.
        # On Linux backslashes are ordinary filename characters — the path
        # stays inside base, which is still safe (just a weird filename).
        base = tempfile.mkdtemp()
        if sys.platform == "win32":
            with self.assertRaises(ValueError):
                main.safe_join(base, "..\\..\\evil")
        else:
            self.assertTrue(main.safe_join(base, "..\\..\\evil")
                            .startswith(base))

    def test_cfg_path_falls_back_to_default(self):
        cfg = {"cache_dir": os.path.join("..", "..", "evil")}
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
        out = wallpaper._ini_safe(evil)
        self.assertNotIn("\n", out)
        self.assertNotIn("\r", out)

    def test_strips_control_chars_and_limits_length(self):
        out = wallpaper._ini_safe("ok\x00\x1f" + "a" * 500)
        self.assertLessEqual(len(out), 128)
        self.assertNotIn("\x00", out)

    @unittest.skipUnless(sys.platform == "win32",
                         "theme.py is the Windows-only implementation")
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
        # the hold must SURVIVE the forced rerun — it deliberately ignored
        # the detected game, so clearing it would let the next poll revert
        # the theme the user just chose (the redo-buttons revert bug)
        self.assertEqual(state["manual_hold"]["identity"], "440")
        self.assertEqual(state["last_identity"], "440")

    def test_stale_poll_after_forced_rerun_still_holds(self):
        self._check(force=True)          # e.g. Full refetch on the held theme
        self.assertEqual(self.pipeline_calls[-1]["appid"], 440)
        state = self._check()            # next normal poll, nothing new played
        self.assertEqual(len(self.pipeline_calls), 1)   # NO revert to Dota 2
        self.assertEqual(state["manual_hold"]["identity"], "440")

    def test_new_play_after_forced_rerun_releases_hold(self):
        self._check(force=True)
        games = [dict(self.GAMES[0]),
                 dict(self.GAMES[1], rtime_last_played=self.hold_at + 60)]
        with mock.patch.object(main.steamdetect, "get_owned_games",
                               lambda k, s: games):
            state = self._check()
        self.assertEqual(self.pipeline_calls[-1]["appid"], 570)
        self.assertNotIn("manual_hold", state)


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
        with open(os.path.join(cache, main.theme_mod.THEME_FILENAME),
                  "w") as f:
            f.write("[Theme]")
        with open(os.path.join(cache, "palette.json"), "w") as f:
            json.dump({"theme_name": "Ashen Vigil", "game_name": "Witcher 3"},
                      f)
        with mock.patch.object(main.theme_mod, "write_registry_colors"), \
                mock.patch.object(main.theme_mod, "apply_theme"), \
                mock.patch.object(main.extras, "apply_terminal"), \
                mock.patch.object(main.extras, "apply_rgb"), \
                mock.patch.object(main.time, "sleep"):
            main.reapply_from_cache(cfg, lambda m: None, "440")
        state = main.load_state(cfg)
        self.assertEqual(state["manual_hold"]["identity"], "440")
        self.assertEqual(state["manual_hold"]["name"], "Witcher 3")
        self.assertGreater(state["manual_hold"]["held_at"], 0)
        self.assertEqual(state["last_identity"], "440")


class TestHeroCandidates(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.dir, ignore_errors=True))

    def _mk(self, name):
        p = os.path.join(self.dir, name)
        with open(p, "wb") as f:
            f.write(b"x")
        return p

    def _names(self, paths):
        return [os.path.basename(p) for p in paths]

    def test_numeric_sort_order(self):
        # numbers can pass 9 as files are pruned/re-fetched — lexical sort
        # would put hero_10 before hero_2
        self._mk("hero_10.jpg"); self._mk("hero_2.jpg"); self._mk("hero_1.jpg")
        self.assertEqual(self._names(artwork.hero_candidates(self.dir)),
                         ["hero_1.jpg", "hero_2.jpg", "hero_10.jpg"])

    def test_legacy_hero_jpg_sorts_first(self):
        self._mk("hero_1.jpg"); self._mk("hero.jpg")
        self.assertEqual(self._names(artwork.hero_candidates(self.dir)),
                         ["hero.jpg", "hero_1.jpg"])

    def test_next_dest_avoids_collisions(self):
        taken = [self._mk("hero_2.jpg")]
        dest = artwork._next_hero_dest(self.dir, taken)
        self.assertEqual(os.path.basename(dest), "hero_3.jpg")
        self.assertEqual(os.path.basename(artwork._next_hero_dest(self.dir, [])),
                         "hero_1.jpg")

    def test_choice_defaults_and_clamps(self):
        self._mk("hero_1.jpg")
        self.assertEqual(artwork.hero_choice(self.dir), 0)  # no choice file
        artwork.set_hero_choice(self.dir, 0)
        with open(os.path.join(self.dir, "hero_choice.json"), "w") as f:
            json.dump({"index": 9}, f)  # tampered past the end
        self.assertEqual(artwork.hero_choice(self.dir), 0)  # clamped to n-1

    def test_set_choice_validates_and_persists(self):
        self._mk("hero_1.jpg"); self._mk("hero_2.jpg")
        artwork.set_hero_choice(self.dir, 1)
        self.assertEqual(artwork.hero_choice(self.dir), 1)
        self.assertTrue(artwork.active_hero(self.dir).endswith("hero_2.jpg"))
        with self.assertRaises(ValueError):
            artwork.set_hero_choice(self.dir, 2)
        with self.assertRaises(ValueError):
            artwork.set_hero_choice(self.dir, -1)

    def test_set_choice_without_candidates(self):
        with self.assertRaises(ValueError):
            artwork.set_hero_choice(self.dir, 0)

    def test_active_hero_empty_cache(self):
        self.assertIsNone(artwork.active_hero(self.dir))

    def test_upscale_cache_files_never_pollute_candidates(self):
        # hero_N_upscaled_<model>.png files are the upscale cache — they
        # match a naive hero_*.png glob and would corrupt picker indexes
        self._mk("hero.jpg")
        self._mk("hero_upscaled_bloom-2.png")
        self._mk("hero_1.jpg")
        self._mk("hero_1_upscaled_text-refine.png")
        self._mk("hero_2.jpg")
        self.assertEqual(self._names(artwork.hero_candidates(self.dir)),
                         ["hero.jpg", "hero_1.jpg", "hero_2.jpg"])
        artwork.set_hero_choice(self.dir, 2)
        self.assertTrue(artwork.active_hero(self.dir).endswith("hero_2.jpg"))


class TestArtFromCache(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.dir, ignore_errors=True))

    def _mk(self, name):
        p = os.path.join(self.dir, name)
        with open(p, "wb") as f:
            f.write(b"x")
        return p

    def test_rebuilds_art_dict(self):
        self._mk("hero_1.jpg"); self._mk("hero_2.png")
        self._mk("hero_1_upscaled_bloom-2.png")  # upscale cache must not leak
        self._mk("logo.png"); self._mk("icon.jpg")
        art = artwork.art_from_cache(self.dir)
        self.assertEqual([os.path.basename(p) for p in art["hero_candidates"]],
                         ["hero_1.jpg", "hero_2.png"])
        self.assertTrue(art["hero"].endswith("hero_1.jpg"))
        self.assertTrue(art["logo"].endswith("logo.png"))
        self.assertTrue(art["icon"].endswith("icon.jpg"))

    def test_respects_hero_choice(self):
        self._mk("hero_1.jpg"); self._mk("hero_2.png")
        artwork.set_hero_choice(self.dir, 1)
        self.assertTrue(artwork.art_from_cache(self.dir)["hero"]
                        .endswith("hero_2.png"))

    def test_empty_or_missing_folder(self):
        self.assertIsNone(artwork.art_from_cache(self.dir))
        self.assertIsNone(artwork.art_from_cache(
            os.path.join(self.dir, "does-not-exist")))


class TestNormalizeImage(unittest.TestCase):
    """Downloads are normalized to real JPEG/PNG with matching extensions —
    Topaz rejects anything else (Steam community icons are ICO, some SGDB
    art is WebP or PNG-bytes-at-a-.jpg-name)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.dir, ignore_errors=True))

    def _save(self, name, fmt, mode="RGB", size=(32, 32)):
        from PIL import Image
        p = os.path.join(self.dir, name)
        color = (10, 20, 30, 128) if "A" in mode else (10, 20, 30)
        Image.new(mode, size, color).save(p, fmt)
        return p

    def test_ico_with_alpha_becomes_png(self):
        p = self._save("icon.jpg", "ICO", mode="RGBA", size=(256, 256))
        out = artwork._normalize_image(p, lambda m: None)
        self.assertTrue(out.endswith("icon.png"))
        self.assertFalse(os.path.exists(p))  # original removed
        from PIL import Image
        with Image.open(out) as im:
            self.assertEqual(im.format, "PNG")
            self.assertEqual(im.mode, "RGBA")  # transparency preserved

    def test_webp_becomes_jpeg(self):
        p = self._save("hero_1.jpg", "WEBP")
        out = artwork._normalize_image(p, lambda m: None)
        self.assertTrue(out.endswith("hero_1.jpg"))
        from PIL import Image
        with Image.open(out) as im:
            self.assertEqual(im.format, "JPEG")

    def test_png_content_in_jpg_is_renamed_not_reencoded(self):
        p = self._save("logo.jpg", "PNG", mode="RGBA")
        with open(p, "rb") as f:
            before = f.read()
        out = artwork._normalize_image(p, lambda m: None)
        self.assertTrue(out.endswith("logo.png"))
        with open(out, "rb") as f:
            self.assertEqual(f.read(), before)  # plain rename, lossless

    def test_canonical_jpeg_untouched(self):
        p = self._save("hero.jpg", "JPEG")
        with open(p, "rb") as f:
            before = f.read()
        out = artwork._normalize_image(p, lambda m: None)
        self.assertEqual(out, p)
        with open(out, "rb") as f:
            self.assertEqual(f.read(), before)

    def test_corrupt_file_is_left_alone(self):
        p = os.path.join(self.dir, "hero_1.jpg")
        with open(p, "wb") as f:
            f.write(b"not an image at all")
        self.assertEqual(artwork._normalize_image(p, lambda m: None), p)

    def test_mixed_extension_candidates(self):
        self._save("hero.jpg", "JPEG")
        self._save("hero_1.png", "PNG")
        self._save("hero_2.jpg", "JPEG")
        names = [os.path.basename(p)
                 for p in artwork.hero_candidates(self.dir)]
        self.assertEqual(names, ["hero.jpg", "hero_1.png", "hero_2.jpg"])
        artwork.set_hero_choice(self.dir, 1)
        self.assertTrue(artwork.active_hero(self.dir).endswith("hero_1.png"))


class TestArtCompleteness(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.dir, ignore_errors=True))

    def _mk(self, name):
        p = os.path.join(self.dir, name)
        with open(p, "wb") as f:
            f.write(b"x")
        return p

    def test_hero_count_for(self):
        self.assertEqual(artwork.hero_count_for({}), 6)
        self.assertEqual(artwork.hero_count_for({"hero": {"count": 3}}), 3)
        self.assertEqual(artwork.hero_count_for({"hero": {"count": 99}}),
                         artwork.MAX_HERO_CANDIDATES)
        self.assertEqual(artwork.hero_count_for({"hero": {"count": "x"}}), 6)
        self.assertEqual(artwork.hero_count_for({"hero": "junk"}), 6)

    def test_art_is_cached(self):
        self.assertFalse(artwork.art_is_cached(self.dir, 2))
        self._mk("hero_1.jpg"); self._mk("hero_2.jpg")
        self.assertFalse(artwork.art_is_cached(self.dir, 2))  # no logo/icon
        self._mk("logo.jpg")
        self.assertFalse(artwork.art_is_cached(self.dir, 2))  # no icon
        self._mk("icon.png")  # either extension counts
        self.assertTrue(artwork.art_is_cached(self.dir, 2))
        self.assertFalse(artwork.art_is_cached(self.dir, 3))  # count too low


class TestFetchLibrary(unittest.TestCase):
    GAMES = [{"appid": 440, "name": "TF2", "icon_url": None},
             {"appid": 570, "name": "Dota 2", "icon_url": None},
             {"appid": 620, "name": "Portal 2", "icon_url": None},
             {"appid": 730, "name": "CS2", "icon_url": None}]

    def setUp(self):
        self.tmp = tempfile.mkdtemp(dir=main.BASE_DIR)
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.tmp, ignore_errors=True))
        rel = os.path.relpath(self.tmp, main.BASE_DIR)
        self.cfg = {"steam_api_key": "k", "steam_id64": "1",
                    "cache_dir": rel, "log_file": os.path.join(rel, "x.log"),
                    "exclude_appids": [570], "hero": {"count": 2}}
        # 440 is fully cached (2 heroes + logo + icon); the rest are not
        c440 = os.path.join(self.tmp, "440")
        os.makedirs(c440)
        for n in ("hero_1.jpg", "hero_2.jpg", "logo.jpg", "icon.png"):
            with open(os.path.join(c440, n), "wb") as f:
                f.write(b"x")
        self.calls = []
        patches = [
            mock.patch.object(main.steamdetect, "get_owned_games",
                              lambda k, s: list(self.GAMES)),
            mock.patch.object(main.artwork, "fetch_artwork",
                              self._fake_fetch),
            mock.patch.object(main.time, "sleep"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _fake_fetch(self, appid, name, icon_url, cfg, log, cache_key=None):
        self.calls.append(appid)
        return {"hero": "x"} if appid != 730 else None  # 730 'fails'

    def test_skips_excluded_and_complete(self):
        stats = main.fetch_library(self.cfg, lambda m: None)
        self.assertEqual(self.calls, [620, 730])
        self.assertEqual(stats["skipped"], 1)    # 440 already cached
        self.assertEqual(stats["excluded"], 1)   # 570 on the exclude list
        self.assertEqual(stats["fetched"], 1)    # 620
        self.assertEqual(stats["failed"], 1)     # 730 returned nothing
        self.assertFalse(stats["cancelled"])

    def test_cancel_stops_early(self):
        seen = []

        def cancel():
            return len(seen) >= 1  # cancel after the first processed game

        def progress(i, total, name):
            seen.append(i)
        stats = main.fetch_library(self.cfg, lambda m: None,
                                   should_cancel=cancel, progress=progress)
        self.assertTrue(stats["cancelled"])
        self.assertLess(len(self.calls), 2)

    def test_requires_steam_key(self):
        with self.assertRaises(ValueError):
            main.fetch_library({"steam_api_key": ""}, lambda m: None)


class TestFetchLibraryUpscale(unittest.TestCase):
    """Ultimate Fetch's upscale option: runs the theme-time upscale pass per
    game (freshly fetched AND already-cached), credit-safe via the per-model
    upscale cache. The fake maybe_upscale simulates real cache semantics."""

    GAMES = [{"appid": 440, "name": "TF2", "icon_url": None},
             {"appid": 570, "name": "Dota 2", "icon_url": None}]
    MONS = [{"name": "H", "role": "hero", "rect": (0, 0, 5120, 1440)},
            {"name": "L", "role": "logo", "rect": (0, 0, 2560, 1440)},
            {"name": "I", "role": "icon", "rect": (0, 0, 1600, 900)}]

    def setUp(self):
        self.tmp = tempfile.mkdtemp(dir=main.BASE_DIR)
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.tmp, ignore_errors=True))
        rel = os.path.relpath(self.tmp, main.BASE_DIR)
        self.cfg = {"steam_api_key": "k", "steam_id64": "1",
                    "cache_dir": rel, "log_file": os.path.join(rel, "x.log"),
                    "hero": {"count": 2},
                    "upscaling": {"enabled": True, "provider": "topaz"},
                    "topaz": {"model": "Bloom 2"}}
        self._mkart(os.path.join(self.tmp, "440"))  # fully cached game
        self.upscale_calls = []
        patches = [
            mock.patch.object(main.steamdetect, "get_owned_games",
                              lambda k, s: list(self.GAMES)),
            mock.patch.object(main.artwork, "fetch_artwork", self._fake_fetch),
            mock.patch.object(main.time, "sleep"),
            mock.patch.object(main.theme_mod, "enumerate_monitors",
                              lambda: list(self.MONS)),
            mock.patch.object(main.theme_mod, "assign_roles",
                              lambda mons, cfg, log: list(self.MONS)),
            mock.patch.object(main.upscale, "maybe_upscale",
                              self._fake_upscale),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _mkart(self, cache):
        """A complete 2-hero art set of real (tiny) images."""
        from PIL import Image
        os.makedirs(cache, exist_ok=True)
        for n in ("hero_1.jpg", "hero_2.jpg", "logo.jpg", "icon.jpg"):
            Image.new("RGB", (32, 32), (10, 20, 30)).save(
                os.path.join(cache, n), "JPEG")

    def _fake_fetch(self, appid, name, icon_url, cfg, log, cache_key=None):
        cache = os.path.join(cfg["cache_dir"], cache_key or str(appid))
        self._mkart(cache)
        return artwork.art_from_cache(cache)

    def _fake_upscale(self, path, w, h, cfg, log, context=None, role=None):
        """Real cache semantics: an existing cache file or an already-
        upscaled source returns without 'spending'; otherwise the job
        produces the cache file and is recorded."""
        dst = main.upscale.cache_path(path, cfg, role)
        if os.path.exists(dst):
            return dst
        if "_upscaled_" in os.path.basename(path):
            return path
        with open(dst, "wb") as f:
            f.write(b"x")
        self.upscale_calls.append((os.path.basename(path), role))
        return dst

    def _run(self, **kw):
        return main.fetch_library(self.cfg, lambda m: None, **kw)

    def test_pick_mode_upscales_active_hero_logo_icon(self):
        stats = self._run(with_upscale=True)
        self.assertEqual(stats["upscaled"], 6)  # 3 roles x 2 games
        self.assertEqual(self.upscale_calls.count(("hero_1.jpg", "hero")), 2)
        self.assertNotIn(("hero_2.jpg", "hero"), self.upscale_calls)
        self.assertEqual(self.upscale_calls.count(("logo.jpg", "logo")), 2)
        self.assertEqual(self.upscale_calls.count(("icon.jpg", "icon")), 2)

    def test_rotate_mode_upscales_every_candidate(self):
        self.cfg["hero"]["mode"] = "rotate"
        stats = self._run(with_upscale=True)
        self.assertEqual(stats["upscaled"], 8)  # (2 heroes + logo + icon) x 2
        self.assertEqual(self.upscale_calls.count(("hero_1.jpg", "hero")), 2)
        self.assertEqual(self.upscale_calls.count(("hero_2.jpg", "hero")), 2)

    def test_without_upscale_flag_nothing_upscales(self):
        stats = self._run()
        self.assertEqual(stats["upscaled"], 0)
        self.assertEqual(self.upscale_calls, [])
        self.assertEqual(stats["skipped"], 1)   # 440 already cached
        self.assertEqual(stats["fetched"], 1)   # 570

    def test_resume_does_not_respend_cached_upscales(self):
        # 440's active hero was already upscaled by an earlier run
        dst = main.upscale.cache_path(
            os.path.join(self.tmp, "440", "hero_1.jpg"), self.cfg, "hero")
        with open(dst, "wb") as f:
            f.write(b"x")
        stats = self._run(with_upscale=True)
        # 440 contributes logo + icon only; 570 contributes hero + logo + icon
        self.assertEqual(stats["upscaled"], 5)
        self.assertEqual(self.upscale_calls.count(("hero_1.jpg", "hero")), 1)

    def test_estimate_counts_pending_images(self):
        est = main.upscale_pending_estimate(self.cfg, lambda m: None)
        self.assertEqual(est, {"games": 1, "images": 3})  # 440 only; 570 unfetched

    def test_estimate_rotate_counts_all_candidates(self):
        self.cfg["hero"]["mode"] = "rotate"
        est = main.upscale_pending_estimate(self.cfg, lambda m: None)
        self.assertEqual(est, {"games": 1, "images": 4})

    def test_estimate_skips_cached_upscales(self):
        dst = main.upscale.cache_path(
            os.path.join(self.tmp, "440", "hero_1.jpg"), self.cfg, "hero")
        with open(dst, "wb") as f:
            f.write(b"x")
        est = main.upscale_pending_estimate(self.cfg, lambda m: None)
        self.assertEqual(est["images"], 2)  # logo + icon still pending

    def test_estimate_zero_when_upscaling_disabled(self):
        self.cfg["upscaling"]["enabled"] = False
        self.assertEqual(
            main.upscale_pending_estimate(self.cfg, lambda m: None),
            {"games": 0, "images": 0})


class TestSgdbMultiHero(unittest.TestCase):
    def test_heroes_sliced_to_count(self):
        def fake_get(path, key, params=None):
            if path == "/games/steam/440":
                return {"data": {"id": 7}}
            if path == "/heroes/game/7":
                return {"data": [{"url": f"u{i}"} for i in range(10)]}
            if path == "/logos/game/7":
                return {"data": [{"url": "logo"}]}
            if path == "/icons/game/7":
                return {"data": [{"url": "icon"}]}
            return None
        with mock.patch.object(artwork, "_sgdb_get", fake_get):
            out = artwork._sgdb_art(440, "k", hero_count=6)
        self.assertEqual(out["heroes"], [f"u{i}" for i in range(6)])
        self.assertEqual(out["logo"], "logo")
        self.assertEqual(out["icon"], "icon")

    def test_no_game_id_yields_empty(self):
        with mock.patch.object(artwork, "_sgdb_get", lambda *a, **k: None):
            out = artwork._sgdb_art(440, "k", hero_count=6)
        self.assertEqual(out, {"heroes": [], "logo": None, "icon": None})


class TestHeroSettings(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(main._hero_settings({}),
                         {"mode": "pick", "interval_minutes": 30,
                          "shuffle": False})

    def test_junk_sanitized(self):
        s = main._hero_settings({"hero": {"mode": "bogus",
                                          "interval_minutes": "x",
                                          "shuffle": 1}})
        self.assertEqual(s["mode"], "pick")
        self.assertEqual(s["interval_minutes"], 30)
        self.assertTrue(s["shuffle"])

    def test_non_dict_hero_tolerated(self):
        self.assertEqual(main._hero_settings({"hero": "rotate"})["mode"],
                         "pick")

    def test_interval_clamped(self):
        s = main._hero_settings({"hero": {"mode": "rotate",
                                          "interval_minutes": -5}})
        self.assertEqual(s["interval_minutes"], 1)
        s = main._hero_settings({"hero": {"mode": "rotate",
                                          "interval_minutes": 99999}})
        self.assertEqual(s["interval_minutes"], 1440)
        # 0/None are falsy -> fall back to the default rather than clamping
        self.assertEqual(main._hero_settings(
            {"hero": {"mode": "rotate", "interval_minutes": 0}}
        )["interval_minutes"], 30)


@unittest.skipUnless(sys.platform == "win32",
                     "theme.py is the Windows-only implementation")
class TestWindowsSlideshowTheme(unittest.TestCase):
    def test_slideshow_block_with_variants(self):
        pal = {"accent": "#AABBCC"}
        with tempfile.TemporaryDirectory() as d:
            v1 = os.path.join(d, "v1.jpg")
            v2 = os.path.join(d, "v2.jpg")
            for v in (v1, v2):
                with open(v, "wb") as f:
                    f.write(b"x")
            path = os.path.join(d, "t.theme")
            theme.write_theme_file(path, "Show", v1, pal, variants=[v1, v2],
                                   interval_minutes=15, shuffle=True)
            with open(path, encoding="utf-8") as f:
                content = f.read()
        self.assertIn("[Slideshow]", content)
        self.assertIn("Interval=900000", content)  # 15 min in ms
        self.assertIn("Shuffle=1", content)
        # ImagesRootPath is required by the .theme format — without it the
        # apply engine silently ignores the whole [Slideshow] section
        self.assertIn(f"ImagesRootPath={os.path.dirname(os.path.abspath(v1))}",
                      content)
        self.assertIn(f"Item0Path={os.path.abspath(v1)}", content)
        self.assertIn(f"Item1Path={os.path.abspath(v2)}", content)
        # static fallback still points at the first variant
        self.assertIn(f"Wallpaper={os.path.abspath(v1)}", content)

    def test_no_slideshow_block_without_variants(self):
        pal = {"accent": "#AABBCC"}
        with tempfile.TemporaryDirectory() as d:
            v1 = os.path.join(d, "v1.jpg")
            with open(v1, "wb") as f:
                f.write(b"x")
            path = os.path.join(d, "t.theme")
            theme.write_theme_file(path, "Static", v1, pal)
            with open(path, encoding="utf-8") as f:
                content = f.read()
            # a single variant is not a slideshow either
            theme.write_theme_file(path, "Static", v1, pal, variants=[v1],
                                   interval_minutes=5, shuffle=True)
            with open(path, encoding="utf-8") as f:
                content_one = f.read()
        self.assertNotIn("[Slideshow]", content)
        self.assertNotIn("[Slideshow]", content_one)

    def test_first_art_path_skips_metadata(self):
        art = {"hero_candidates": ["a", "b"], "_rotate": {"x": 1},
               "logo": "logo.jpg"}
        self.assertEqual(wallpaper.first_art_path(art), "logo.jpg")
        self.assertEqual(wallpaper.first_art_path({"hero": "h.jpg"}), "h.jpg")
        self.assertIsNone(wallpaper.first_art_path({"hero_candidates": [1]}))


class TestUpscalePromptTemplate(unittest.TestCase):
    CTX = {"game": "Hades II", "mood": "dark fantasy",
           "theme_name": "Ashen Vigil", "appearance": "dark",
           "palette_mode": "muted"}

    def test_all_vlm_keys_filled(self):
        out, dropped = upscale.fill_prompt(
            "{game} | {mood} | {theme_name} | {appearance} | {palette_mode}",
            self.CTX)
        self.assertEqual(out,
                         "Hades II | dark fantasy | Ashen Vigil | dark | muted")
        self.assertEqual(dropped, [])

    def test_unknown_placeholders_dropped_not_raised(self):
        # a typo'd placeholder must not crash the pipeline (str.format would
        # raise KeyError here)
        out, dropped = upscale.fill_prompt("{game} {them_name} x", self.CTX)
        self.assertEqual(out, "Hades II  x")
        self.assertEqual(dropped, ["them_name"])

    def test_empty_values_stay_empty_and_literal_braces_untouched(self):
        out, _ = upscale.fill_prompt("{game} [{mood}]", {"game": "X",
                                                         "mood": ""})
        self.assertEqual(out, "X []")
        out, _ = upscale.fill_prompt("no placeholders", self.CTX)
        self.assertEqual(out, "no placeholders")

    def _run_topaz(self, prompt_template, ctx):
        """Run _topaz with mocked HTTP; return (submitted_payload, logs, out)."""
        import io
        from PIL import Image
        submitted = {}
        buf = io.BytesIO()
        Image.new("RGB", (16, 16), (1, 2, 3)).save(buf, "PNG")
        png_bytes = buf.getvalue()

        class Resp:
            def __init__(self, code, payload=None, content=b""):
                self.status_code = code
                self._payload = payload or {}
                self.content = content
                self.headers = {"Content-Type": "application/octet-stream"}
                self.text = json.dumps(self._payload)

            def json(self):
                return self._payload

        def fake_post(url, **kw):
            submitted.update(kw.get("data") or {})
            return Resp(202, {"process_id": "p1"})

        def fake_get(url, **kw):
            if "/status/" in url:
                return Resp(200, {"status": "Completed"})
            return Resp(200, content=png_bytes)  # the download

        cfg = {"upscaling": {"enabled": True, "provider": "topaz"},
               "topaz": {"api_key": "k", "model": "Bloom 2",
                         "prompt": prompt_template}}
        logs = []
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "hero_1.png")
            with open(src, "wb") as f:
                f.write(png_bytes)
            with mock.patch.object(upscale.requests, "post", fake_post), \
                    mock.patch.object(upscale.requests, "get", fake_get), \
                    mock.patch.object(upscale.time, "sleep"):
                out = upscale._topaz(src, os.path.join(d, "out.png"), cfg,
                                     logs.append, 100, 100, ctx, "hero")
        return submitted, logs, out

    def test_topaz_submits_filled_prompt(self):
        """End-to-end through _topaz with mocked HTTP: the generative job
        payload must carry the filled prompt, placeholders resolved."""
        submitted, logs, out = self._run_topaz(
            "{game}, {mood}, {theme_name}, {nope}",
            {"game": "Hades II", "mood": "dark", "theme_name": "Ashen Vigil"})
        self.assertEqual(submitted.get("prompt"), "Hades II, dark, Ashen Vigil, ")
        self.assertIsNotNone(out)
        self.assertTrue(any("unknown prompt placeholders" in m and "nope" in m
                            for m in logs))

    def test_topaz_prompt_capped_at_1024(self):
        submitted, logs, out = self._run_topaz("{game}", {"game": "x" * 5000})
        self.assertEqual(len(submitted.get("prompt", "")), 1024)


class TestNamingPrompt(unittest.TestCase):
    PAL = {"colors": ["#112233", "#445566", "#778899"]}

    def test_default_when_unset(self):
        import palette
        out = palette.build_naming_prompt({}, "Hades II", self.PAL)
        self.assertIn("Hades II", out)
        self.assertIn("#112233, #445566, #778899", out)
        self.assertIn('"theme_name"', out)
        self.assertNotIn("{game}", out)
        self.assertNotIn("{colors}", out)

    def test_custom_prompt_with_placeholders(self):
        import palette
        cfg = {"ai": {"prompt": "Rate the vibes of {game} using {colors}."}}
        out = palette.build_naming_prompt(cfg, "Silksong", self.PAL)
        self.assertIn("Rate the vibes of Silksong using", out)
        # JSON contract appended so parsing can't silently break
        self.assertIn('"theme_name"', out)

    def test_custom_prompt_with_contract_not_duplicated(self):
        import palette
        cfg = {"ai": {"prompt": 'Name it. Return {"theme_name": "x"}.'}}
        out = palette.build_naming_prompt(cfg, "Hades II", self.PAL)
        self.assertEqual(out.count('"theme_name"'), 1)

    def test_empty_prompt_falls_back(self):
        import palette
        cfg = {"ai": {"prompt": "   "}}
        out = palette.build_naming_prompt(cfg, "Hades II", self.PAL)
        self.assertTrue(out.startswith("This is key art from the game"))


class TestUpscaleContext(unittest.TestCase):
    """Extra keys in the VLM's JSON answer are forwarded as Topaz prompt
    placeholders, so a custom naming-prompt JSON shape (e.g. a "genre" key)
    becomes usable in the Topaz prompt template."""

    PAL = {"appearance": "dark"}

    def _ctx(self, ai):
        return main._upscale_context("Hades II", self.PAL, ai,
                                     {"palette_mode": "colorful"})

    def test_fixed_five_present_without_ai(self):
        ctx = self._ctx(None)
        self.assertEqual(ctx, {"game": "Hades II", "mood": "",
                               "theme_name": "Hades II — Steam Theme",
                               "appearance": "dark",
                               "palette_mode": "colorful"})

    def test_custom_keys_forwarded_and_fillable(self):
        ai = {"theme_name": "Ashen Vigil", "mood": "dark fantasy",
              "genre": "roguelike", "player_count": 1, "coop": False,
              "tags": ["action", "mythology"]}
        ctx = self._ctx(ai)
        self.assertEqual(ctx["genre"], "roguelike")
        self.assertEqual(ctx["player_count"], "1")
        self.assertEqual(ctx["coop"], "false")
        self.assertEqual(ctx["tags"], "action, mythology")
        out, dropped = upscale.fill_prompt(
            "{game} ({genre}) — {tags} — coop: {coop}", ctx)
        self.assertEqual(out,
                         "Hades II (roguelike) — action, mythology — coop: false")
        self.assertEqual(dropped, [])

    def test_fixed_keys_not_overwritten(self):
        # effective values win over raw VLM duplicates (appearance may be a
        # post-override value; theme_name/palette_mode carry fallbacks)
        ai = {"appearance": "light", "palette_mode": "muted",
              "game": "SPOOFED", "theme_name": "Ashen Vigil"}
        ctx = self._ctx(ai)
        self.assertEqual(ctx["appearance"], "dark")     # pal's effective value
        self.assertEqual(ctx["palette_mode"], "muted")  # ai beats cfg default
        self.assertEqual(ctx["game"], "Hades II")       # never spoofable
        self.assertEqual(ctx["theme_name"], "Ashen Vigil")

    def test_unplaceholdable_values_and_keys_skipped(self):
        ai = {"nested": {"x": 1}, "nothing": None, "bool_list": [True],
              "bad key!": "x", "9lives": "x", "ok_key": "fine"}
        ctx = self._ctx(ai)
        for bad in ("nested", "nothing", "bool_list", "bad key!", "9lives"):
            self.assertNotIn(bad, ctx)
        self.assertEqual(ctx["ok_key"], "fine")

    def test_runaway_value_capped(self):
        ctx = self._ctx({"essay": "x" * 5000})
        self.assertEqual(len(ctx["essay"]), 200)


class TestComfyWorkflow(unittest.TestCase):
    def test_default_workflow_valid(self):
        wf, err = upscale.validate_workflow("")
        self.assertIsNone(err)
        types = {n["class_type"] for n in wf.values()}
        self.assertIn("LoadImage", types)
        self.assertIn("SaveImage", types)

    def test_invalid_json(self):
        wf, err = upscale.validate_workflow("{not json")
        self.assertIsNone(wf)
        self.assertIn("invalid JSON", err)

    def test_missing_required_nodes(self):
        _, err = upscale.validate_workflow(
            '{"1": {"class_type": "LoadImage", "inputs": {}}}')
        self.assertIn("SaveImage", err)
        _, err = upscale.validate_workflow(
            '{"1": {"class_type": "SaveImage", "inputs": {}}}')
        self.assertIn("LoadImage", err)

    def test_fill_workflow_nested_lists_and_scalars(self):
        wf = {"1": {"inputs": {"text": "{game} — {mood}", "seed": 20},
                    "class_type": "CLIPTextEncode"},
              "2": {"inputs": {"images": ["5", 0]}}}  # connection lists untouched
        out, dropped = upscale.fill_workflow(wf, {"game": "Hades II",
                                                  "mood": "dark"})
        self.assertEqual(out["1"]["inputs"]["text"], "Hades II — dark")
        self.assertEqual(out["1"]["inputs"]["seed"], 20)
        self.assertEqual(out["2"]["inputs"]["images"], ["5", 0])
        self.assertEqual(dropped, [])
        self.assertEqual(wf["1"]["inputs"]["text"], "{game} — {mood}")  # copy

    def test_fill_workflow_quotes_and_newlines_are_safe(self):
        # substitution happens post-parse, so JSON-hostile values can't
        # break the workflow structure
        wf = {"1": {"inputs": {"text": "{mood}"}}}
        out, _ = upscale.fill_workflow(wf, {"mood": 'dark "gritty"\nmood'})
        self.assertEqual(out["1"]["inputs"]["text"], 'dark "gritty"\nmood')

    def test_fill_workflow_unknown_dropped(self):
        wf = {"1": {"inputs": {"text": "{game} {genre}"}}}
        out, dropped = upscale.fill_workflow(wf, {"game": "X"})
        self.assertEqual(out["1"]["inputs"]["text"], "X ")
        self.assertEqual(dropped, ["genre"])

    def test_comfy_slug_stable_and_content_addressed(self):
        s1 = upscale._comfy_slug({"comfy": {"workflow": ""}})
        self.assertTrue(s1.startswith("comfy-"))
        # explicit default text hashes identically to empty (= built-in)
        s2 = upscale._comfy_slug(
            {"comfy": {"workflow": upscale.DEFAULT_COMFY_WORKFLOW}})
        self.assertEqual(s1, s2)
        s3 = upscale._comfy_slug({"comfy": {"workflow": (
            '{"1": {"class_type": "LoadImage", "inputs": {}},'
            ' "2": {"class_type": "SaveImage", "inputs": {}}}')}})
        self.assertNotEqual(s1, s3)  # editing the workflow re-generates

    def test_comfy_base_validation(self):
        self.assertEqual(upscale._comfy_base({}), "http://127.0.0.1:8188")
        self.assertEqual(upscale._comfy_base(
            {"comfy": {"host": "192.168.1.5", "port": 8189}}),
            "http://192.168.1.5:8189")
        with self.assertRaises(ValueError):
            upscale._comfy_base({"comfy": {"host": "evil.com/x"}})
        with self.assertRaises(ValueError):
            upscale._comfy_base({"comfy": {"host": "ok", "port": 99999}})


class TestComfySubmit(unittest.TestCase):
    """The Comfy provider flow against a mocked server: upload -> submit
    (nodes patched, placeholders filled) -> history poll -> download."""

    def setUp(self):
        import io
        from PIL import Image
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.dir, ignore_errors=True))
        self.src = os.path.join(self.dir, "hero_1.jpg")
        Image.new("RGB", (32, 32), (10, 20, 30)).save(self.src, "JPEG")
        buf = io.BytesIO()
        Image.new("RGB", (64, 64), (1, 2, 3)).save(buf, "PNG")
        self.png_bytes = buf.getvalue()
        self.dst = os.path.join(self.dir, "hero_1_upscaled_comfy-aaaa1111.png")
        self.workflow = json.dumps({
            "1": {"class_type": "LoadImage", "inputs": {"image": "x.jpg"}},
            "5": {"class_type": "CLIPTextEncode",
                  "inputs": {"text": "{game} key art, {mood}"}},
            "9": {"class_type": "SaveImage",
                  "inputs": {"filename_prefix": "orig"}}})
        self.cfg = {"cache_dir": self.dir,
                    "upscaling": {"enabled": True, "provider": "comfy"},
                    "comfy": {"host": "127.0.0.1", "port": 8188,
                              "workflow": self.workflow,
                              "defer_while_gaming": False}}
        self.submitted = None

    def _resp(self, status=200, payload=None, content=b""):
        r = mock.Mock()
        r.status_code = status
        r.text = payload if isinstance(payload, str) else json.dumps(payload or {})
        r.json = lambda: payload if isinstance(payload, dict) else {}
        r.content = content
        r.headers = {}
        return r

    def _post(self, url, **kw):
        if url.endswith("/upload/image"):
            return self._resp(200, {"name": "u_abc.jpg"})
        if url.endswith("/prompt"):
            self.submitted = kw["json"]["prompt"]
            return self._resp(200, {"prompt_id": "pid-1"})
        raise AssertionError(url)

    def _get(self, url, **kw):
        if "/history/" in url:
            return self._resp(200, {"pid-1": {
                "status": {"completed": True, "status_str": "success"},
                "outputs": {"9": {"images": [
                    {"filename": "steamtheme_x_00001_.png",
                     "subfolder": "", "type": "output"}]}}}})
        if url.endswith("/view"):
            return self._resp(200, content=self.png_bytes)
        raise AssertionError(url)

    def _run(self):
        import requests as real_requests
        with mock.patch.object(upscale, "requests") as rq, \
                mock.patch.object(upscale.time, "sleep"):
            rq.post.side_effect = self._post
            rq.get.side_effect = self._get
            rq.RequestException = real_requests.RequestException
            return upscale._comfy(self.src, self.dst, self.cfg, lambda m: None,
                                  5120, 1440, context={"game": "Hades II",
                                                       "mood": "dark fantasy"})

    def test_happy_path_patches_nodes_and_fills_placeholders(self):
        out = self._run()
        self.assertEqual(out, self.dst)
        with open(self.dst, "rb") as f:
            self.assertEqual(f.read(), self.png_bytes)
        self.assertEqual(self.submitted["1"]["inputs"]["image"], "u_abc.jpg")
        self.assertTrue(self.submitted["9"]["inputs"]["filename_prefix"]
                        .startswith("steamtheme_"))
        self.assertEqual(self.submitted["5"]["inputs"]["text"],
                         "Hades II key art, dark fantasy")

    def test_submit_http_error(self):
        def bad_post(url, **kw):
            if url.endswith("/prompt"):
                return self._resp(400, payload="bad workflow")
            return self._post(url, **kw)
        import requests as real_requests
        with mock.patch.object(upscale, "requests") as rq, \
                mock.patch.object(upscale.time, "sleep"):
            rq.post.side_effect = bad_post
            rq.RequestException = real_requests.RequestException
            self.assertIsNone(
                upscale._comfy(self.src, self.dst, self.cfg, lambda m: None,
                               5120, 1440, context={}))

    def test_history_error_status(self):
        def err_get(url, **kw):
            if "/history/" in url:
                return self._resp(200, {"pid-1": {
                    "status": {"status_str": "error", "messages": [
                        ["execution_error", {"exception_message": "OOM"}]]},
                    "outputs": {}}})
            return self._get(url, **kw)
        import requests as real_requests
        with mock.patch.object(upscale, "requests") as rq, \
                mock.patch.object(upscale.time, "sleep"):
            rq.post.side_effect = self._post
            rq.get.side_effect = err_get
            rq.RequestException = real_requests.RequestException
            self.assertIsNone(
                upscale._comfy(self.src, self.dst, self.cfg, lambda m: None,
                               5120, 1440, context={}))

    def test_unreachable(self):
        import requests as real_requests
        with mock.patch.object(upscale, "requests") as rq:
            rq.post.side_effect = real_requests.ConnectionError("nope")
            rq.RequestException = real_requests.RequestException
            self.assertIsNone(
                upscale._comfy(self.src, self.dst, self.cfg, lambda m: None,
                               5120, 1440, context={}))


class TestComfyPerRole(unittest.TestCase):
    """Per-role workflow overrides (comfy.workflows.hero/logo/icon): a role
    with an override uses its own workflow and its own cache slot; roles
    without one fall back to the shared workflow. {role} is a placeholder."""

    def setUp(self):
        import io
        from PIL import Image
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.dir, ignore_errors=True))
        os.makedirs(os.path.join(self.dir, "440"))
        self.src = os.path.join(self.dir, "440", "hero_1.jpg")
        Image.new("RGB", (32, 32), (10, 20, 30)).save(self.src, "JPEG")
        buf = io.BytesIO()
        Image.new("RGB", (64, 64), (1, 2, 3)).save(buf, "PNG")
        self.png_bytes = buf.getvalue()
        self.shared = json.dumps({
            "1": {"class_type": "LoadImage", "inputs": {"image": "x.jpg"}},
            "5": {"class_type": "CLIPTextEncode",
                  "inputs": {"text": "SHARED {game} ({role})"}},
            "9": {"class_type": "SaveImage", "inputs": {}}})
        self.hero_wf = json.dumps({
            "1": {"class_type": "LoadImage", "inputs": {"image": "x.jpg"}},
            "5": {"class_type": "CLIPTextEncode",
                  "inputs": {"text": "HERO-MARKER {role}"}},
            "9": {"class_type": "SaveImage", "inputs": {}}})
        self.cfg = {"cache_dir": self.dir,
                    "upscaling": {"enabled": True, "provider": "comfy"},
                    "comfy": {"host": "127.0.0.1", "port": 8188,
                              "workflow": self.shared,
                              "workflows": {"hero": self.hero_wf},
                              "defer_while_gaming": False}}
        self.submitted = None

    def _resp(self, payload=None, content=b""):
        r = mock.Mock()
        r.status_code = 200
        r.text = payload if isinstance(payload, str) else ""
        r.json = lambda: payload if isinstance(payload, dict) else {}
        r.content = content
        return r

    def _mock_server(self):
        import requests as real_requests
        cm = mock.patch.object(upscale, "requests")
        rq = cm.start()
        self.addCleanup(cm.stop)
        sl = mock.patch.object(upscale.time, "sleep")
        sl.start()
        self.addCleanup(sl.stop)
        rq.RequestException = real_requests.RequestException

        def post(url, **kw):
            if url.endswith("/upload/image"):
                return self._resp({"name": "u_abc.jpg"})
            if url.endswith("/prompt"):
                self.submitted = kw["json"]["prompt"]
                return self._resp({"prompt_id": "p1"})
            raise AssertionError(url)

        def get(url, **kw):
            if "/history/" in url:
                return self._resp({"p1": {
                    "status": {"completed": True, "status_str": "success"},
                    "outputs": {"9": {"images": [
                        {"filename": "o.png", "subfolder": "",
                         "type": "output"}]}}}})
            if url.endswith("/view"):
                return self._resp(content=self.png_bytes)
            raise AssertionError(url)

        rq.post.side_effect = post
        rq.get.side_effect = get

    def test_slug_shared_when_no_overrides(self):
        del self.cfg["comfy"]["workflows"]
        slugs = {upscale._comfy_slug(self.cfg, r)
                 for r in ("hero", "logo", "icon", None)}
        self.assertEqual(len(slugs), 1)

    def test_slug_differs_only_for_the_overridden_role(self):
        hero = upscale._comfy_slug(self.cfg, "hero")
        logo = upscale._comfy_slug(self.cfg, "logo")
        icon = upscale._comfy_slug(self.cfg, "icon")
        self.assertNotEqual(hero, logo)
        self.assertEqual(logo, icon)
        # and the cache path carries the role's slug
        self.assertIn(hero, upscale.cache_path(self.src, self.cfg, "hero"))
        self.assertIn(logo, upscale.cache_path(self.src, self.cfg, "logo"))

    def test_per_role_workflow_submitted_with_role_placeholder(self):
        self._mock_server()
        dst = upscale.cache_path(self.src, self.cfg, "hero")
        out = upscale._comfy(self.src, dst, self.cfg, lambda m: None,
                             5120, 1440, context={"game": "Hades II"},
                             role="hero")
        self.assertEqual(out, dst)
        self.assertEqual(self.submitted["5"]["inputs"]["text"],
                         "HERO-MARKER hero")
        # a role without an override gets the shared workflow, {role} filled
        dst2 = upscale.cache_path(self.src, self.cfg, "logo")
        out2 = upscale._comfy(self.src, dst2, self.cfg, lambda m: None,
                              5120, 1440, context={"game": "Hades II"},
                              role="logo")
        self.assertEqual(out2, dst2)
        self.assertEqual(self.submitted["5"]["inputs"]["text"],
                         "SHARED Hades II (logo)")

    def test_drain_recomputes_dst_after_workflow_edit(self):
        self.cfg["comfy"]["defer_while_gaming"] = True
        with mock.patch.object(upscale, "_game_running", return_value=True):
            upscale.maybe_upscale(self.src, 5120, 1440, self.cfg,
                                  lambda m: None, context={}, role="hero")
        old_dst = upscale.cache_path(self.src, self.cfg, "hero")
        # user edits the hero workflow while the job sits in the queue
        self.cfg["comfy"]["workflows"]["hero"] = self.hero_wf.replace(
            "HERO-MARKER", "HERO-V2")
        new_dst = upscale.cache_path(self.src, self.cfg, "hero")
        self.assertNotEqual(old_dst, new_dst)

        def fake_comfy(src, dst, cfg, log, w, h, context=None, defer=True,
                       role=None):
            with open(dst, "wb") as f:
                f.write(b"done")
            return dst

        with mock.patch.object(upscale, "_game_running", return_value=False), \
                mock.patch.object(upscale, "_comfy", fake_comfy), \
                mock.patch.object(upscale, "requests") as rq:
            rq.get.return_value = mock.Mock(status_code=200)
            done = upscale.drain_comfy_queue(self.cfg, lambda m: None)
        self.assertEqual(done, ["440"])
        self.assertTrue(os.path.exists(new_dst))   # landed under the NEW slug
        self.assertFalse(os.path.exists(old_dst))

    def test_drain_drops_job_whose_cache_slot_is_already_filled(self):
        self.cfg["comfy"]["defer_while_gaming"] = True
        with mock.patch.object(upscale, "_game_running", return_value=True):
            upscale.maybe_upscale(self.src, 5120, 1440, self.cfg,
                                  lambda m: None, context={}, role="hero")
        # the upscale arrives through some other path before the drain runs
        dst = upscale.cache_path(self.src, self.cfg, "hero")
        with open(dst, "wb") as f:
            f.write(b"already")
        calls = []
        with mock.patch.object(upscale, "_game_running", return_value=False), \
                mock.patch.object(upscale, "_comfy",
                                  lambda *a, **k: calls.append(1)), \
                mock.patch.object(upscale, "requests") as rq:
            rq.get.return_value = mock.Mock(status_code=200)
            done = upscale.drain_comfy_queue(self.cfg, lambda m: None)
        self.assertEqual(done, [])     # nothing NEW produced -> no re-theme
        self.assertEqual(calls, [])     # no GPU work submitted
        with open(upscale._queue_file(self.cfg), encoding="utf-8") as f:
            self.assertEqual(json.load(f), [])  # job dropped, not retried


class TestComfyDefer(unittest.TestCase):
    """defer_while_gaming: upscales queue while a game runs, drain when idle,
    dedupe repeat enqueues, drop poison jobs after 3 attempts."""

    def setUp(self):
        from PIL import Image
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.dir, ignore_errors=True))
        os.makedirs(os.path.join(self.dir, "440"))
        self.src = os.path.join(self.dir, "440", "hero_1.jpg")
        Image.new("RGB", (32, 32), (10, 20, 30)).save(self.src, "JPEG")
        self.cfg = {"cache_dir": self.dir,
                    "upscaling": {"enabled": True, "provider": "comfy"},
                    "comfy": {"host": "127.0.0.1", "port": 8188,
                              "workflow": "", "defer_while_gaming": True}}
        self.qfile = os.path.join(self.dir, "comfy_queue.json")

    def _queue(self):
        with open(self.qfile, encoding="utf-8") as f:
            return json.load(f)

    def test_defers_and_dedupes_while_gaming(self):
        with mock.patch.object(upscale, "_game_running", return_value=True):
            out1 = upscale.maybe_upscale(self.src, 5120, 1440, self.cfg,
                                         lambda m: None,
                                         context={"game": "X"}, role="hero")
            out2 = upscale.maybe_upscale(self.src, 5120, 1440, self.cfg,
                                         lambda m: None,
                                         context={"game": "X"}, role="hero")
        self.assertEqual(out1, self.src)   # fallback to the original for now
        self.assertEqual(out2, self.src)
        q = self._queue()
        self.assertEqual(len(q), 1)        # deduped
        self.assertEqual(q[0]["cache_key"], "440")
        self.assertEqual(q[0]["context"], {"game": "X"})
        self.assertIn("_upscaled_comfy-", q[0]["dst"])

    def test_drain_processes_queue_when_idle(self):
        with mock.patch.object(upscale, "_game_running", return_value=True):
            upscale.maybe_upscale(self.src, 5120, 1440, self.cfg,
                                  lambda m: None, context={}, role="hero")

        def fake_comfy(src, dst, cfg, log, w, h, context=None, defer=True,
                       role=None):
            with open(dst, "wb") as f:
                f.write(b"done")
            return dst

        with mock.patch.object(upscale, "_game_running", return_value=False), \
                mock.patch.object(upscale, "_comfy", fake_comfy), \
                mock.patch.object(upscale, "requests") as rq:
            rq.get.return_value = mock.Mock(status_code=200)  # system_stats
            done = upscale.drain_comfy_queue(self.cfg, lambda m: None)
        self.assertEqual(done, ["440"])
        self.assertEqual(self._queue(), [])
        outs = glob.glob(os.path.join(self.dir, "440",
                                      "*_upscaled_comfy-*.png"))
        self.assertEqual(len(outs), 1)
        with open(outs[0], "rb") as f:
            self.assertEqual(f.read(), b"done")

    def test_drain_skips_while_gaming(self):
        with mock.patch.object(upscale, "_game_running", return_value=True):
            upscale.maybe_upscale(self.src, 5120, 1440, self.cfg,
                                  lambda m: None, context={}, role="hero")
            done = upscale.drain_comfy_queue(self.cfg, lambda m: None)
        self.assertEqual(done, [])
        self.assertEqual(len(self._queue()), 1)  # untouched

    def test_poison_job_dropped_after_three_attempts(self):
        with mock.patch.object(upscale, "_game_running", return_value=True):
            upscale.maybe_upscale(self.src, 5120, 1440, self.cfg,
                                  lambda m: None, context={}, role="hero")
        with mock.patch.object(upscale, "_game_running", return_value=False), \
                mock.patch.object(upscale, "_comfy",
                                  lambda *a, **k: None), \
                mock.patch.object(upscale, "requests") as rq:
            rq.get.return_value = mock.Mock(status_code=200)
            for _ in range(3):
                done = upscale.drain_comfy_queue(self.cfg, lambda m: None)
        self.assertEqual(done, [])
        self.assertEqual(self._queue(), [])  # dropped, not retried forever

    def test_drain_noop_for_other_providers(self):
        self.cfg["upscaling"]["provider"] = "topaz"
        self.assertEqual(upscale.drain_comfy_queue(self.cfg, lambda m: None),
                         [])


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
