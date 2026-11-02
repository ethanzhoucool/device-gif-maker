"""Unit tests for flow loading, step translation, de-dup and the timeline.
Run with: python -m unittest discover -s tests -v"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import revyl_gif  # noqa: E402

FRAME = revyl_gif.DEFAULTS["frame"]


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class LoadFlowTests(TempDirTest):
    def test_defaults_are_deep_merged(self):
        path = self.dir / "my-flow.yaml"
        path.write_text("app: {app_id: abc}\nframe: {color: silver}\noutput: {fps: 24}\n")
        flow = revyl_gif.load_flow(str(path))
        self.assertEqual(flow["name"], "my-flow")  # defaults to the file stem
        self.assertEqual(flow["platform"], "ios")
        self.assertEqual(flow["steps"], [])
        self.assertEqual(flow["frame"]["color"], "silver")
        self.assertEqual(flow["frame"]["style"], "iphone-pro")  # untouched default
        self.assertEqual(flow["output"]["fps"], 24)
        self.assertEqual(flow["output"]["hold"], revyl_gif.DEFAULTS["output"]["hold"])
        self.assertTrue(flow["capture"]["dedup"])

    def test_json_flow(self):
        path = self.dir / "f.json"
        path.write_text(json.dumps({"name": "j", "steps": [{"home": True}]}))
        self.assertEqual(revyl_gif.load_flow(str(path))["steps"], [{"home": True}])

    def test_shipped_flows_load_and_translate(self):
        for path in sorted((ROOT / "flows").glob("*.yaml")):
            with self.subTest(flow=path.name):
                flow = revyl_gif.load_flow(str(path))
                for step in flow["steps"]:
                    self.assertIsNotNone(revyl_gif.step_to_argv(step))


class CloudStepTests(unittest.TestCase):
    """Every action in the README's table maps to the right `revyl device` argv."""

    CASES = [
        ({"tap": {"target": "Sign In"}}, ["device", "tap", "--target", "Sign In"]),
        ({"tap": {"x": 10, "y": 20}}, ["device", "tap", "--x", "10", "--y", "20"]),
        ({"double_tap": {"x": 200, "y": 400}}, ["device", "double-tap", "--x", "200", "--y", "400"]),
        ({"long_press": {"target": "photo", "duration": 1.0}},
         ["device", "long-press", "--target", "photo", "--duration", "1.0"]),
        ({"type": {"target": "email", "text": "a@b.com"}},
         ["device", "type", "--target", "email", "--text", "a@b.com"]),
        ({"clear_text": {"target": "search field"}},
         ["device", "clear-text", "--target", "search field"]),
        ({"swipe": {"target": "card", "direction": "left"}},
         ["device", "swipe", "--target", "card", "--direction", "left"]),
        ({"drag": {"start_x": 100, "start_y": 800, "end_x": 300, "end_y": 800}},
         ["device", "drag", "--start-x", "100", "--start-y", "800",
          "--end-x", "300", "--end-y", "800"]),
        ({"pinch": {"target": "map", "scale": 2.0}},
         ["device", "pinch", "--target", "map", "--scale", "2.0"]),
        ({"navigate": "myapp://profile/42"}, ["device", "navigate", "myapp://profile/42"]),
        ({"launch": "com.example.app"}, ["device", "launch", "com.example.app"]),
        ({"open_app": "settings"}, ["device", "open-app", "settings"]),
        ({"back": True}, ["device", "back"]),
        ({"home": True}, ["device", "home"]),
        ({"key": "ENTER"}, ["device", "key", "ENTER"]),
        ({"shake": True}, ["device", "shake"]),
        ({"kill_app": True}, ["device", "kill-app"]),
        ({"wait": 1500}, ["device", "wait", "1.5"]),  # README: ms; revyl: seconds
        ({"instruction": "log in"}, ["device", "instruction", "log in"]),
        # control keys ride along without changing the action
        ({"tap": {"target": "x"}, "label": "l", "hold": 2, "capture": False, "settle": 1},
         ["device", "tap", "--target", "x"]),
    ]

    def test_actions(self):
        for step, argv in self.CASES:
            with self.subTest(step=step):
                self.assertEqual(revyl_gif.step_to_argv(step), argv)

    def test_disabled_flag_is_a_noop(self):
        self.assertIsNone(revyl_gif.step_to_argv({"back": False, "label": "skip"}))

    def test_unknown_action(self):
        with self.assertRaises(revyl_gif.DeviceError):
            revyl_gif.step_to_argv({"teleport": True})


class SimStepTests(unittest.TestCase):
    def test_supported(self):
        t = revyl_gif.sim_translate
        self.assertEqual(t({"navigate": "app://x"}, None), ("url", "app://x"))
        self.assertEqual(t({"launch": "com.a"}, None), ("launch", "com.a"))
        self.assertEqual(t({"wait": 250}, None), ("wait", 0.25))
        self.assertEqual(t({"tap": {"x": 1, "y": 2}}, None), ("idb", ["ui", "tap", "1", "2"]))
        self.assertEqual(t({"double_tap": {"x": 1, "y": 2}}, None), ("idb2", ["ui", "tap", "1", "2"]))
        self.assertEqual(t({"type": {"text": "hi"}}, None), ("idb", ["ui", "text", "hi"]))
        self.assertEqual(t({"kill_app": True}, "com.a"), ("term", "com.a"))
        self.assertIsNone(t({"home": False}, None))

    def test_unsupported_locally(self):
        for step in ({"tap": {"target": "Sign In"}},  # no NL grounding locally
                     {"swipe": {"direction": "up"}},
                     {"instruction": "log in"},
                     {"home": True},
                     {"kill_app": True}):  # needs app.bundle_id
            with self.subTest(step=step):
                with self.assertRaises(revyl_gif.DeviceError):
                    revyl_gif.sim_translate(step, None)


def _screen(color, accent=None, size=(390, 844)):
    im = Image.new("RGB", size, color)
    if accent:
        im.paste(accent, (40, 200, 350, 320))
    return im


class DedupTests(TempDirTest):
    def frames(self, *images, hold=1.0):
        out = []
        for i, im in enumerate(images):
            path = self.dir / f"state_{i:03d}.png"
            im.save(path)
            out.append((path, hold, f"s{i}"))
        return out

    def test_identical_neighbours_merge_and_holds_add_up(self):
        a = _screen("#101018", "#1DB954")
        states = revyl_gif.composite_states(self.frames(a, a, a), FRAME, dedup=True)
        self.assertEqual(len(states), 1)
        self.assertAlmostEqual(states[0][1], 3.0)
        self.assertEqual(states[0][2], "s0")

    def test_tiny_change_merges_but_new_screen_does_not(self):
        a = _screen("#101018", "#1DB954")
        tick = a.copy()
        tick.paste("#FFFFFF", (10, 10, 30, 22))  # a clock tick in the status bar
        b = _screen("#101018", "#4F8CFF")  # same layout, different content
        states = revyl_gif.composite_states(self.frames(a, tick, b), FRAME, dedup=True)
        self.assertEqual([s[2] for s in states], ["s0", "s2"])

    def test_dedup_off_keeps_everything(self):
        a = _screen("#101018")
        states = revyl_gif.composite_states(self.frames(a, a), FRAME, dedup=False)
        self.assertEqual(len(states), 2)


class TimelineTests(TempDirTest):
    """build_sequence: frame counts and the loop seam."""

    COLORS = ["#ff0000", "#00ff00", "#0000ff"]

    def states(self, holds=(1.0, 1.0, 1.0), size=(40, 60)):
        return [(Image.new("RGBA", size, c), h, None) for c, h in zip(self.COLORS, holds)]

    def render(self, states, fps=10, xfade=0.3, loop=True, pingpong=False):
        seq = self.dir / "seq"
        revyl_gif.build_sequence(states, seq, fps, xfade, loop, pingpong, "#ffffff")
        return [Image.open(p).convert("RGB") for p in sorted(seq.glob("*.png"))]

    @staticmethod
    def rgb(im):
        return im.getpixel((im.width // 2, im.height // 2))

    def test_loop_counts_and_seam(self):
        frames = self.render(self.states())
        # 3 holds x 10 frames + 3 crossfades (incl. last -> first) x 3 frames
        self.assertEqual(len(frames), 39)
        self.assertEqual(self.rgb(frames[0]), (255, 0, 0))
        # The loop ends mid-crossfade into the first state, never on a copy of
        # it (which would stutter at the wrap) and never on the last state.
        last = self.rgb(frames[-1])
        self.assertGreater(last[0], last[2])  # mostly red already
        self.assertNotEqual(last, (255, 0, 0))

    def test_no_loop_ends_on_last_state(self):
        frames = self.render(self.states(), loop=False)
        self.assertEqual(len(frames), 36)  # no crossfade back to the start
        self.assertEqual(self.rgb(frames[-1]), (0, 0, 255))

    def test_pingpong(self):
        frames = self.render(self.states(), pingpong=True)
        # order 0,1,2,1 -> 4 holds + 4 crossfades (1 -> 0 closes the loop)
        self.assertEqual(len(frames), 4 * 10 + 4 * 3)

    def test_per_state_holds_and_zero_xfade(self):
        frames = self.render(self.states(holds=(0.5, 2.0, 0.04)), xfade=0)
        # 5 + 20 + max(1, round(0.4)) frames, no crossfades
        self.assertEqual(len(frames), 26)

    def test_single_state_has_no_crossfade(self):
        frames = self.render(self.states()[:1])
        self.assertEqual(len(frames), 10)

    def test_mixed_sizes_are_centred_on_one_canvas(self):
        states = self.states(size=(40, 60))
        states[1] = (Image.new("RGBA", (60, 40), "#00ff00"), 1.0, None)  # landscape
        frames = self.render(states)
        self.assertEqual({f.size for f in frames}, {(60, 60)})

    def test_rerender_clears_stale_frames(self):
        self.render(self.states())
        frames = self.render(self.states()[:1])
        self.assertEqual(len(frames), 10)


class LoadCapturedTests(TempDirTest):
    def setUp(self):
        super().setUp()
        for i in range(3):
            Image.new("RGB", (10, 20), (i * 50, 0, 0)).save(self.dir / f"state_{i:03d}.png")

    def test_manifest_restores_holds_and_labels(self):
        (self.dir / "manifest.json").write_text(json.dumps([
            {"file": "state_000.png", "hold": 2.0, "label": "initial"},
            {"file": "state_002.png", "hold": 0.5, "label": "done"},
            {"file": "state_009.png", "hold": 1.0, "label": "missing"},  # skipped
        ]))
        frames = revyl_gif.load_captured(self.dir, 1.3)
        self.assertEqual([(p.name, h, lbl) for p, h, lbl in frames],
                         [("state_000.png", 2.0, "initial"), ("state_002.png", 0.5, "done")])

    def test_uniform_hold_without_manifest(self):
        (self.dir / "manifest.json").write_text("[]")
        frames = revyl_gif.load_captured(self.dir, 1.3, use_manifest=False)
        self.assertEqual([(p.name, h) for p, h, _ in frames],
                         [(f"state_{i:03d}.png", 1.3) for i in range(3)])

    def test_corrupt_manifest_falls_back_to_files(self):
        (self.dir / "manifest.json").write_text("{not json")
        self.assertEqual(len(revyl_gif.load_captured(self.dir, 1.0)), 3)

    def test_no_frames_exits(self):
        with self.assertRaises(SystemExit):
            revyl_gif.load_captured(self.dir / "empty", 1.0)


class PreviewTests(TempDirTest):
    def test_names_are_html_escaped(self):
        gif = self.dir / "x.gif"
        gif.write_bytes(b"GIF89a")
        flow = revyl_gif.default_flow('<script>alert("x")</script>')
        page = revyl_gif.write_preview(self.dir, flow, [gif], 3.0).read_text()
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn('src="x.gif"', page)


if __name__ == "__main__":
    unittest.main()
