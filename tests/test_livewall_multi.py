"""Live-wallpaper multi-hero tests: every candidate gets a video, the queue
is reconciled per art set, one job drains at a time in the background, and
rotation swaps the applied video."""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import livewall  # noqa: E402


def _mp4_bytes():
    return b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048


def fake_get(url, **kw):
    """ComfyUI up + idle queue."""
    if url.endswith("/queue"):
        return mock.Mock(status_code=200,
                         json=lambda: {"queue_running": [],
                                       "queue_pending": []})
    return mock.Mock(status_code=200, json=lambda: {})


class MultiHeroFixture(unittest.TestCase):
    def setUp(self):
        from PIL import Image
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.dir, ignore_errors=True))
        self.heroes = []
        for i, color in enumerate(((10, 20, 30), (200, 40, 40),
                                   (40, 200, 40)), 1):
            p = os.path.join(self.dir, f"hero_{i}.jpg")
            Image.new("RGB", (32, 32), color).save(p, "JPEG")
            self.heroes.append(p)
        self.cfg = {"cache_dir": self.dir,
                    "livewall": {"enabled": True, "provider": "comfy",
                                 "host": "127.0.0.1", "port": 8188,
                                 "workflow": "", "all_heroes": True,
                                 "defer_while_gaming": True,
                                 "timeout_seconds": 600}}
        self.qfile = os.path.join(self.dir, "video_queue.json")
        self.logs = []
        self.log = self.logs.append

    def q(self):
        try:
            with open(self.qfile, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return []


class TestEveryHeroQueued(MultiHeroFixture):
    def test_all_candidates_are_queued(self):
        livewall.maybe_generate(self.cfg, self.heroes, {"game": "G"},
                                "440", "T", "", self.log)
        q = self.q()
        self.assertEqual(len(q), 3)
        self.assertEqual({j["cache_key"] for j in q}, {"440"})
        self.assertEqual(len({j["dst"] for j in q}), 3)
        self.assertTrue(any("3 to make" in m for m in self.logs))

    def test_second_call_dedupes(self):
        livewall.maybe_generate(self.cfg, self.heroes, {}, "440", "T", "",
                                self.log)
        livewall.maybe_generate(self.cfg, self.heroes, {}, "440", "T", "",
                                self.log)
        self.assertEqual(len(self.q()), 3)

    def test_new_art_prunes_stale_jobs(self):
        livewall.maybe_generate(self.cfg, self.heroes, {}, "440", "T", "",
                                self.log)
        old = {j["dst"] for j in self.q()}
        # a refetch replaces the art: new content hashes
        from PIL import Image
        fresh = []
        for i in range(2):
            p = os.path.join(self.dir, f"hero_{i + 1}.jpg")
            Image.new("RGB", (32, 32), (7, 7, i)).save(p, "JPEG")
            fresh.append(p)
        livewall.maybe_generate(self.cfg, fresh, {}, "440", "T", "", self.log)
        q = self.q()
        self.assertEqual(len(q), 2)
        self.assertFalse(old & {j["dst"] for j in q})

    def test_all_heroes_off_queues_only_active(self):
        self.cfg["livewall"]["all_heroes"] = False
        livewall.maybe_generate(self.cfg, self.heroes, {}, "440", "T", "",
                                self.log)
        self.assertEqual(len(self.q()), 1)

    def test_cached_hero_is_skipped_and_applied(self):
        # a video already on disk is not re-queued; the active one is applied
        _h, d, dst = livewall._hero_target(self.cfg, self.heroes[0], "440")
        os.makedirs(d, exist_ok=True)
        with open(dst, "wb") as f:
            f.write(_mp4_bytes())
        with mock.patch.object(livewall, "apply_we") as apply:
            apply.return_value = True
            livewall.maybe_generate(self.cfg, self.heroes, {}, "440", "T",
                                    "\\\\.\\DISPLAY1", self.log)
        self.assertEqual(len(self.q()), 2)   # only the missing two
        apply.assert_called_once()

    def test_apply_false_never_touches_the_wallpaper(self):
        # cache passes (Regenerate All) must never change what's on screen
        _h, d, dst = livewall._hero_target(self.cfg, self.heroes[0], "440")
        os.makedirs(d, exist_ok=True)
        with open(dst, "wb") as f:
            f.write(_mp4_bytes())
        with mock.patch.object(livewall, "apply_we") as apply:
            out = livewall.maybe_generate(self.cfg, self.heroes, {}, "440",
                                          "T", "", self.log, apply=False)
        apply.assert_not_called()
        self.assertFalse(out["applied"])
        self.assertEqual(len(self.q()), 2)   # the missing ones still queue

    def test_disabled_feature_does_nothing(self):
        self.cfg["livewall"]["enabled"] = False
        livewall.maybe_generate(self.cfg, self.heroes, {}, "440", "T", "",
                                self.log)
        self.assertEqual(self.q(), [])


class TestDrainWorker(MultiHeroFixture):
    def setUp(self):
        super().setUp()
        livewall.maybe_generate(self.cfg, self.heroes, {}, "440", "T", "",
                                self.log)

    def test_one_job_per_drain(self):
        with mock.patch.object(livewall, "_game_running",
                               return_value=False), \
                mock.patch.object(livewall, "requests") as rq, \
                mock.patch.object(livewall, "generate") as gen, \
                mock.patch.object(livewall, "apply_we"):
            rq.get.side_effect = fake_get

            def fake(cfg, src, dst, ctx, log, defer=True):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(dst, "wb") as f:
                    f.write(_mp4_bytes())
                return dst
            gen.side_effect = fake
            done = livewall.drain_queue(self.cfg, self.log)
        self.assertEqual(len(done), 1)       # only one clip per call
        self.assertEqual(len(self.q()), 2)   # the rest stay queued

    def test_result_applies_when_theme_is_current(self):
        with mock.patch.object(livewall, "_game_running",
                               return_value=False), \
                mock.patch.object(livewall, "_current_identity",
                                  return_value="440"), \
                mock.patch.object(livewall, "requests") as rq, \
                mock.patch.object(livewall, "generate") as gen, \
                mock.patch.object(livewall, "apply_we") as apply:
            rq.get.side_effect = fake_get

            def fake(cfg, src, dst, ctx, log, defer=True):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(dst, "wb") as f:
                    f.write(_mp4_bytes())
                return dst
            gen.side_effect = fake
            livewall.drain_queue(self.cfg, self.log)
        apply.assert_called_once()

    def test_result_kept_when_another_theme_is_on_screen(self):
        with mock.patch.object(livewall, "_game_running",
                               return_value=False), \
                mock.patch.object(livewall, "_current_identity",
                                  return_value="570"), \
                mock.patch.object(livewall, "requests") as rq, \
                mock.patch.object(livewall, "generate") as gen, \
                mock.patch.object(livewall, "apply_we") as apply:
            rq.get.side_effect = fake_get

            def fake(cfg, src, dst, ctx, log, defer=True):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(dst, "wb") as f:
                    f.write(_mp4_bytes())
                return dst
            gen.side_effect = fake
            done = livewall.drain_queue(self.cfg, self.log)
        apply.assert_not_called()
        self.assertEqual(len(done), 1)   # cached and reported

    def test_busy_comfy_queue_is_left_alone(self):
        # don't inject an hour-long render into someone else's ComfyUI work
        def busy(url, **kw):
            if url.endswith("/queue"):
                return mock.Mock(status_code=200,
                                 json=lambda: {"queue_running": [[1]],
                                               "queue_pending": []})
            return mock.Mock(status_code=200, json=lambda: {})
        with mock.patch.object(livewall, "_game_running",
                               return_value=False), \
                mock.patch.object(livewall, "requests") as rq, \
                mock.patch.object(livewall, "generate") as gen:
            rq.get.side_effect = busy
            self.assertEqual(livewall.drain_queue(self.cfg, self.log), [])
        gen.assert_not_called()
        self.assertEqual(len(self.q()), 3)

    def test_gaming_defers_the_worker(self):
        with mock.patch.object(livewall, "_game_running", return_value=True):
            done = livewall.drain_queue(self.cfg, self.log)
        self.assertEqual(done, [])
        self.assertEqual(len(self.q()), 3)

    def test_start_drain_is_single_flight(self):
        started = []

        def slow(cfg, log, max_jobs=1):
            started.append(1)
            livewall._draining = True  # pretend it is mid-render
            return []
        with mock.patch.object(livewall, "drain_queue", side_effect=slow), \
                mock.patch.object(livewall.threading, "Thread") as th:
            livewall._draining = False
            self.assertTrue(livewall.start_drain(self.cfg, self.log))
            self.assertFalse(livewall.start_drain(self.cfg, self.log))
        th.assert_called_once()


class TestRotation(MultiHeroFixture):
    def _state(self, **kw):
        st = {"last_cache_key": "440"}
        st.update(kw)
        return st

    def test_rotates_to_next_cached_video(self):
        import main as app
        state = self._state()
        calls = []

        def apply_for(cfg, hero, key, title, dev, log):
            calls.append(hero)
            return hero == self.heroes[1]     # only candidate 2 has a video
        with mock.patch.object(app, "load_state", lambda cfg: state), \
                mock.patch.object(app, "save_state",
                                  lambda cfg, s: calls.append("saved")), \
                mock.patch.object(livewall, "apply_for_hero",
                                  side_effect=apply_for):
            out = livewall.rotate_step(self.cfg, self.heroes, "440", "T",
                                       "", self.log)
        self.assertEqual(out, self.heroes[1])
        self.assertEqual(state["livewall_rotate"]["index"], 1)
        self.assertIn("saved", calls)

    def test_no_cached_videos_means_no_rotation(self):
        import main as app
        state = self._state()
        with mock.patch.object(app, "load_state", lambda cfg: state), \
                mock.patch.object(livewall, "apply_for_hero",
                                  return_value=False):
            out = livewall.rotate_step(self.cfg, self.heroes, "440", "T",
                                       "", self.log)
        self.assertIsNone(out)
        self.assertNotIn("livewall_rotate", state)

    def test_single_candidate_is_a_noop(self):
        out = livewall.rotate_step(self.cfg, [self.heroes[0]], "440", "T",
                                   "", self.log)
        self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main()