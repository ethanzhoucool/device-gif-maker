"""Malformed flow files fail fast with a clear message, before any device is
started. Run with: python -m unittest discover -s tests -v"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import revyl_gif  # noqa: E402


class FlowFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name, text):
        path = self.dir / name
        path.write_text(text)
        return str(path)

    def assertExits(self, path, *fragments):
        with self.assertRaises(SystemExit) as cm:
            revyl_gif.load_flow(path)
        msg = str(cm.exception.code)
        for frag in fragments:
            self.assertIn(frag, msg)
        return msg

    def test_missing_file(self):
        self.assertExits(str(self.dir / "nope.yaml"), "Flow file not found", "nope.yaml")

    def test_directory(self):
        self.assertExits(str(self.dir), "Can't read flow file")

    def test_yaml_syntax_error_names_the_line(self):
        path = self.write("f.yaml", 'name: x\nsteps:\n  - tap: { target: "a"\n')
        self.assertExits(path, "invalid YAML", "line")

    def test_json_syntax_error(self):
        path = self.write("f.json", '{"name": "x",}')
        self.assertExits(path, "invalid JSON", "line 1")

    def test_uppercase_extension_is_yaml(self):
        path = self.write("F.YML", "name: up\nsteps: []\n")
        self.assertEqual(revyl_gif.load_flow(path)["name"], "up")

    def test_empty_steps_means_no_steps(self):
        path = self.write("f.yaml", "name: x\nsteps:\n")
        self.assertEqual(revyl_gif.load_flow(path)["steps"], [])

    def test_steps_must_be_a_list(self):
        path = self.write("f.yaml", "steps: {tap: {x: 1, y: 2}}\n")
        self.assertExits(path, "`steps` must be a list")

    def test_bare_string_step(self):
        path = self.write("f.yaml", "steps:\n  - tap: {x: 1, y: 2}\n  - home\n")
        self.assertExits(path, "step 2 must be a mapping", "'home'")

    def test_section_must_be_a_mapping(self):
        path = self.write("f.yaml", "output: 30\n")
        self.assertExits(path, "`output` must be a key/value mapping")

    def test_name_with_slash(self):
        path = self.write("f.yaml", "name: a/b\n")
        self.assertExits(path, "can't contain a slash")

    def test_missing_name_uses_file_stem(self):
        path = self.write("my-flow.yaml", "name:\nsteps: []\n")
        self.assertEqual(revyl_gif.load_flow(path)["name"], "my-flow")


class StepOptionTests(unittest.TestCase):
    def test_string_instead_of_mapping(self):
        # `"x" in "box"` used to be True and crash with a TypeError.
        for step in ({"tap": "box"}, {"type": "hello"}, {"long_press": "photo"}):
            with self.subTest(step=step):
                with self.assertRaises(revyl_gif.DeviceError):
                    revyl_gif.step_to_argv(step)
                with self.assertRaises(revyl_gif.DeviceError):
                    revyl_gif.sim_translate(step, None)

    def test_drag_missing_coordinate(self):
        step = {"drag": {"start_x": 1, "start_y": 2, "end_x": 3}}
        with self.assertRaisesRegex(revyl_gif.DeviceError, "end_y"):
            revyl_gif.step_to_argv(step)
        with self.assertRaisesRegex(revyl_gif.DeviceError, "end_y"):
            revyl_gif.sim_translate(step, None)

    def test_non_numeric_wait(self):
        with self.assertRaisesRegex(revyl_gif.DeviceError, "milliseconds"):
            revyl_gif.step_to_argv({"wait": "soon"})

    def test_valid_steps_unchanged(self):
        self.assertEqual(revyl_gif.step_to_argv({"tap": {"target": "Sign In"}}),
                         ["device", "tap", "--target", "Sign In"])
        self.assertEqual(
            revyl_gif.step_to_argv({"drag": {"start_x": 1, "start_y": 2, "end_x": 3, "end_y": 4}}),
            ["device", "drag", "--start-x", "1", "--start-y", "2", "--end-x", "3", "--end-y", "4"])
        self.assertEqual(revyl_gif.step_to_argv({"wait": 1500}), ["device", "wait", "1.5"])


class CliTests(unittest.TestCase):
    """Drive main() with the device + encoders stubbed out."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def run_main(self, flow_text, *argv):
        flow = self.dir / "flow.yaml"
        flow.write_text(flow_text)
        argv = ["revyl-gif", str(flow), "--out", str(self.dir / "out"), "--no-open", *argv]
        captured = {}

        def fake_capture(flow, frames_dir, driver, attach=None, timeout=600, keep=False):
            captured["timeout"] = timeout
            return []  # main() then exits with "No frames captured"

        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(revyl_gif.shutil, "which", return_value="/usr/bin/x"), \
                mock.patch.object(revyl_gif, "capture_flow", fake_capture), \
                self.assertRaises(SystemExit) as cm:
            revyl_gif.main()
        return str(cm.exception.code), captured

    def test_quoted_number_is_rejected_before_capture(self):
        for text, frag in (('output: {fps: "30"}\n', "output.fps must be a number"),
                           ("output: {xfade: -1}\n", "output.xfade must be >= 0"),
                           ("frame: {width: 0}\n", "frame.width must be > 0"),
                           ('capture: {launch_settle: "2s"}\n', "capture.launch_settle")):
            with self.subTest(text=text):
                msg, captured = self.run_main("app: {app_id: a}\n" + text)
                self.assertIn(frag, msg)
                self.assertEqual(captured, {}, "capture must not start")

    def test_timeout_precedence(self):
        _, captured = self.run_main("app: {app_id: a}\n")
        self.assertEqual(captured["timeout"], 600)
        _, captured = self.run_main("app: {app_id: a}\ntimeout: 42\n")
        self.assertEqual(captured["timeout"], 42)
        _, captured = self.run_main("app: {app_id: a}\ntimeout: 42\n", "--timeout", "7")
        self.assertEqual(captured["timeout"], 7)


if __name__ == "__main__":
    unittest.main()
