"""End-to-end cloud mode against a fake `revyl` CLI -- never the real Revyl cloud.

The fake is a tiny executable put first on PATH: it logs every invocation and
answers `revyl device screenshot --out X` with a solid-color PNG, so the real
capture -> frame -> encode path runs exactly as it would against a device.
Run with: python -m unittest discover -s tests -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FAKE_REVYL = textwrap.dedent("""\
    #!{python}
    import json, os, sys
    from PIL import Image

    args = sys.argv[1:]
    log = os.environ["FAKE_REVYL_LOG"]
    with open(log, "a") as fh:
        fh.write(json.dumps(args) + "\\n")
    if " ".join(args) == os.environ.get("FAKE_REVYL_FAIL"):
        sys.exit("fake revyl: simulated failure")
    if args[:2] == ["device", "screenshot"]:
        shots = sum(1 for line in open(log) if '"screenshot"' in line)
        # A new color per screenshot, so de-dup keeps every state.
        color = ((shots * 70) % 256, (shots * 130) % 256, 200)
        Image.new("RGB", (390, 844), color).save(args[args.index("--out") + 1])
    """)

FLOW = textwrap.dedent("""\
    name: fake-run
    app:
      app_id: "app-123"
    device_model: "iPhone 16 Pro"
    output: {fps: 10, hold: 0.5, xfade: 0.2}
    capture: {settle: 0, launch_settle: 0}
    steps:
      - tap: { target: "Search tab" }
        label: "open search"
        hold: 1.0
      - tap: { target: "search field" }
        capture: false
      - type: { target: "search field", text: "Daft Punk" }
      - key: ENTER
        label: "results"
    """)


@unittest.skipIf(shutil.which("ffmpeg") is None, "needs ffmpeg on PATH")
class FakeRevylCloudRun(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        fake = self.bin / "revyl"
        fake.write_text(FAKE_REVYL.format(python=sys.executable))
        fake.chmod(0o755)
        self.log = self.dir / "revyl.log"
        self.flow = self.dir / "flow.yaml"
        self.flow.write_text(FLOW)
        self.out = self.dir / "out dir"  # a space, on purpose

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *extra, path=None, fail=None):
        env = dict(os.environ, FAKE_REVYL_LOG=str(self.log),
                   PATH=path if path is not None else f"{self.bin}{os.pathsep}{os.environ['PATH']}")
        if fail:
            env["FAKE_REVYL_FAIL"] = fail
        return subprocess.run(
            [sys.executable, str(ROOT / "revyl_gif.py"), str(self.flow),
             "--out", str(self.out), "--no-open", *extra],
            env=env, capture_output=True, text=True, timeout=300)

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_full_run(self):
        proc = self.run_cli("--timeout", "120")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        calls = self.calls()
        self.assertEqual(calls[0], [
            "device", "start", "--platform", "ios", "--timeout", "120", "--open=false",
            "--json", "--app-id", "app-123", "--device-model", "iPhone 16 Pro"])
        actions = [c for c in calls[1:] if c[:2] != ["device", "screenshot"]]
        self.assertEqual(actions, [
            ["device", "tap", "--target", "Search tab"],
            ["device", "tap", "--target", "search field"],
            ["device", "type", "--target", "search field", "--text", "Daft Punk"],
            ["device", "key", "ENTER"],
            ["device", "stop", "--all"],  # torn down at the end
        ])
        # initial state + every step except the `capture: false` one
        shots = [c for c in calls if c[:2] == ["device", "screenshot"]]
        self.assertEqual(len(shots), 4)

        manifest = json.loads((self.out / "frames" / "manifest.json").read_text())
        self.assertEqual([(m["file"], m["hold"], m["label"]) for m in manifest], [
            ("state_000.png", 0.5, "initial"),
            ("state_001.png", 1.0, "open search"),
            ("state_002.png", 0.5, None),
            ("state_003.png", 0.5, "results"),
        ])
        for name in ("fake-run.gif", "fake-run.mp4", "preview.html"):
            path = self.out / name
            self.assertTrue(path.exists() and path.stat().st_size > 0, name)

        # --dry-run re-renders from the capture without touching the device
        before = len(self.calls())
        proc = self.run_cli("--dry-run", "--no-mp4")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(len(self.calls()), before)

    def test_failed_step_still_renders_and_stops_the_session(self):
        proc = self.run_cli("--no-mp4", fail="device type --target search field --text Daft Punk")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("step failed, ending flow early", proc.stdout)
        calls = self.calls()
        self.assertEqual(calls[-1], ["device", "stop", "--all"])
        self.assertNotIn(["device", "key", "ENTER"], calls)
        manifest = json.loads((self.out / "frames" / "manifest.json").read_text())
        self.assertEqual(len(manifest), 2)  # initial + "open search"
        self.assertTrue((self.out / "fake-run.gif").exists())

    def test_attach_reuses_the_session(self):
        proc = self.run_cli("--attach", "sess-42", "--no-gif", "--no-mp4")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        calls = self.calls()
        self.assertEqual(calls[0], ["device", "attach", "sess-42"])
        self.assertNotIn("start", [c[1] for c in calls])
        self.assertNotIn("stop", [c[1] for c in calls])  # not ours to stop

    def test_missing_revyl_cli(self):
        proc = self.run_cli("--no-gif", "--no-mp4", path=str(self.dir / "nothing-here"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("`revyl` not found on PATH", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_missing_ffmpeg(self):
        proc = self.run_cli(path=str(self.bin))  # fake revyl, but no ffmpeg
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("`ffmpeg` not found on PATH", proc.stderr)
        self.assertEqual(self.calls(), [], "must fail before starting a device")


if __name__ == "__main__":
    unittest.main()
