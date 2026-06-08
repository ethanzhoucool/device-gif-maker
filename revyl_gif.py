#!/usr/bin/env python3
"""device-gif-maker -- any flow -> a clean looping GIF on a pristine device frame.

Drives a device through a flow (defined in YAML/JSON), captures a screenshot at
every UI state, frames each one in a pristine device mockup, and encodes a
smooth, seamlessly-looping GIF (+ optional MP4) for a README or a tweet.

    revyl-gif flows/example.yaml              # Revyl cloud device (default)
    revyl-gif local flows/example.yaml        # local iOS simulator (xcrun simctl)
    revyl-gif local --manual                  # local sim: press Enter to grab each screen
    revyl-gif flows/example.yaml --dry-run    # re-render from captured frames, no device

Cloud mode uses the Revyl CLI (`revyl device ...`) with natural-language
targeting. Local mode uses `xcrun simctl` (+ `idb` for taps) against a booted
simulator. See README.md.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import Image  # noqa: E402
import device_frame  # noqa: E402

try:
    import yaml  # noqa: E402
except ImportError:  # pragma: no cover
    yaml = None


# --------------------------------------------------------------------------- #
# Flow spec loading + defaults
# --------------------------------------------------------------------------- #

DEFAULTS = {
    "platform": "ios",
    "frame": {
        "style": "iphone-pro",
        "color": "black",
        "background": "light",
        "shadow": True,
        "width": 440,
        "buttons": True,
    },
    "output": {
        "gif": True,
        "mp4": True,
        "fps": 30,
        "hold": 1.3,        # seconds each state is held
        "xfade": 0.45,      # crossfade seconds between consecutive states
        "loop": True,       # seamless crossfade from last state back to first
        "pingpong": False,
        "matte": "#ffffff", # fill for transparent backgrounds when encoding
    },
    "capture": {
        "initial": True,    # capture the launch/opening state before step 1
        "settle": 0.6,      # seconds to wait after an action before screenshotting
        "launch_settle": 2.5,  # seconds after session start before first screenshot
        "dedup": True,      # merge consecutive identical frames into one longer hold
    },
}

# Actions that take no value -- only fire when the flow sets them to a truthy
# value, so `back: false` (e.g. a disabled step in a template) is a no-op
# instead of silently firing.
FLAG_ACTIONS = {
    "back": "back",
    "home": "home",
    "shake": "shake",
    "kill_app": "kill-app",
}


def _deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def default_flow(name):
    """A flow with no steps -- used by `local --manual` / no-flow runs."""
    return {
        "platform": "ios",
        "name": name,
        "app": {},
        "steps": [],
        "frame": dict(DEFAULTS["frame"]),
        "output": dict(DEFAULTS["output"]),
        "capture": dict(DEFAULTS["capture"]),
    }


def load_flow(path):
    text = Path(path).read_text()
    if path.endswith((".yaml", ".yml")):
        if yaml is None:
            sys.exit("PyYAML not installed; use a .json flow or `pip install pyyaml`.")
        spec = yaml.safe_load(text)
    else:
        spec = json.loads(text)

    flow = dict(spec)
    flow["frame"] = _deep_merge(DEFAULTS["frame"], spec.get("frame"))
    flow["output"] = _deep_merge(DEFAULTS["output"], spec.get("output"))
    flow["capture"] = _deep_merge(DEFAULTS["capture"], spec.get("capture"))
    flow.setdefault("platform", DEFAULTS["platform"])
    flow.setdefault("steps", [])
    flow.setdefault("name", Path(path).stem)
    return flow


class DeviceError(RuntimeError):
    """Any failure talking to a device backend (cloud or local sim)."""


# --------------------------------------------------------------------------- #
# Cloud backend: the Revyl CLI (`revyl device ...`)
# --------------------------------------------------------------------------- #

def revyl(args, *, quiet=False, check=True, capture=True):
    cmd = ["revyl", *args]
    if not quiet:
        print(f"  $ {' '.join(cmd)}")
    proc = subprocess.run(cmd, capture_output=capture, text=True)
    if check and proc.returncode != 0:
        raise DeviceError(
            f"`{' '.join(cmd)}` failed ({proc.returncode}):\n{proc.stderr or proc.stdout}"
        )
    return proc


def start_session(flow, timeout):
    args = ["device", "start", "--platform", flow["platform"],
            "--timeout", str(timeout), "--open=false", "--json"]
    src = flow.get("app", {})
    for key, opt in (("app_id", "--app-id"), ("app_url", "--app-url"),
                     ("build_version_id", "--build-version-id"),
                     ("app_link", "--app-link")):
        if src.get(key):
            args += [opt, str(src[key])]
    if flow.get("device_model"):
        args += ["--device-model", str(flow["device_model"])]
    if flow.get("os_version"):
        args += ["--os-version", str(flow["os_version"])]
    for var in flow.get("launch_vars", []):
        args += ["--launch-var", str(var)]

    revyl(args)
    print("  -> cloud device ready")


def revyl_screenshot(out_path, retries=2):
    last = None
    for _ in range(retries + 1):
        try:
            revyl(["device", "screenshot", "--out", str(out_path)], quiet=True)
            with Image.open(out_path) as im:
                im.verify()
            return True
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.6)
    print(f"  ! screenshot failed after {retries + 1} tries: {last}")
    return False


def step_to_argv(step):
    """Translate one flow step into `revyl device ...` arguments (cloud mode).

    A step has one action key plus optional control keys (capture / hold /
    label / settle). Targets may be natural language (`target:`) or coordinates
    (`x:`/`y:`). Returns the argv list, `None` for a recognized-but-disabled
    flag action (e.g. `back: false`), or raises DeviceError on an unknown key.
    """
    def tgt(d):
        a = []
        if "target" in d:
            a += ["--target", str(d["target"])]
        if "x" in d:
            a += ["--x", str(d["x"])]
        if "y" in d:
            a += ["--y", str(d["y"])]
        return a

    disabled_flag = False
    for key, cmd in FLAG_ACTIONS.items():
        if key in step:
            if step[key]:
                return ["device", cmd]
            disabled_flag = True

    if "tap" in step:
        return ["device", "tap", *tgt(step["tap"])]
    if "double_tap" in step:
        return ["device", "double-tap", *tgt(step["double_tap"])]
    if "long_press" in step:
        d = step["long_press"]
        extra = ["--duration", str(d["duration"])] if "duration" in d else []
        return ["device", "long-press", *tgt(d), *extra]
    if "type" in step:
        d = step["type"]
        return ["device", "type", *tgt(d), "--text", str(d.get("text", ""))]
    if "clear_text" in step:
        return ["device", "clear-text", *tgt(step["clear_text"])]
    if "swipe" in step:
        d = step["swipe"]
        extra = ["--direction", str(d["direction"])] if "direction" in d else []
        return ["device", "swipe", *tgt(d), *extra]
    if "drag" in step:
        d = step["drag"]
        return ["device", "drag",
                "--start-x", str(d["start_x"]), "--start-y", str(d["start_y"]),
                "--end-x", str(d["end_x"]), "--end-y", str(d["end_y"])]
    if "pinch" in step:
        d = step["pinch"]
        extra = ["--scale", str(d["scale"])] if "scale" in d else []
        return ["device", "pinch", *tgt(d), *extra]
    if "navigate" in step:
        return ["device", "navigate", str(step["navigate"])]
    if "launch" in step:
        return ["device", "launch", str(step["launch"])]
    if "open_app" in step:
        return ["device", "open-app", str(step["open_app"])]
    if "key" in step:
        return ["device", "key", str(step["key"])]
    if "wait" in step:
        # `wait` is always milliseconds (matches the README); `revyl device
        # wait` takes seconds.
        return ["device", "wait", str(float(step["wait"]) / 1000.0)]
    if "instruction" in step:
        return ["device", "instruction", str(step["instruction"])]
    if disabled_flag:
        return None  # only a disabled flag action present -> capture-only no-op
    raise DeviceError(f"Unrecognized step (no known action key): {step}")


class CloudDriver:
    """Backend: a Revyl cloud device with natural-language targeting."""

    label = "Revyl cloud device"
    source = "a Revyl cloud device"
    supports_attach = True

    def start(self, flow, timeout):
        start_session(flow, timeout)

    def attach(self, session_id):
        revyl(["device", "attach", session_id])

    def screenshot(self, path):
        return revyl_screenshot(path)

    def translate(self, step):
        return step_to_argv(step)

    def describe(self, action):
        return action[1] if action else "(disabled)"

    def perform(self, action):
        if action is not None:
            revyl(action)

    def teardown(self, keep, attach):
        if attach:
            return
        if keep:
            print("  -> session kept alive")
            return
        revyl(["device", "stop", "--all"], check=False, quiet=True)
        print("  -> session stopped")


# --------------------------------------------------------------------------- #
# Local backend: a booted iOS simulator (`xcrun simctl`, optional `idb`)
# --------------------------------------------------------------------------- #

def simctl(args, *, check=True, quiet=True):
    cmd = ["xcrun", "simctl", *args]
    if not quiet:
        print(f"  $ {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        raise DeviceError("`xcrun` not found. Install Xcode / the command line tools "
                          "to use local simulator mode.")
    if check and proc.returncode != 0:
        raise DeviceError(f"`{' '.join(cmd)}` failed:\n{proc.stderr or proc.stdout}")
    return proc


def _ensure_booted_sim():
    proc = simctl(["list", "devices", "booted"], check=False)
    if "Booted" not in (proc.stdout or ""):
        raise DeviceError("No booted iOS simulator found. Open Simulator.app and boot a "
                          "device first (or `xcrun simctl boot <udid>`), then retry.")


def simctl_screenshot(out_path, retries=2):
    # simctl resolves relative paths against the CoreSimulator service's working
    # directory (effectively /), not our cwd -- always hand it an absolute path.
    target = os.path.abspath(str(out_path))
    last = None
    for _ in range(retries + 1):
        try:
            simctl(["io", "booted", "screenshot", target])
            with Image.open(target) as im:
                im.verify()
            return True
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.4)
    print(f"  ! simulator screenshot failed after {retries + 1} tries: {last}")
    return False


def _idb_available():
    return shutil.which("idb") is not None


def idb(args, *, check=True):
    proc = subprocess.run(["idb", *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise DeviceError(f"`idb {' '.join(args)}` failed:\n{proc.stderr or proc.stdout}")
    return proc


def _sim_coords(d, action):
    if "x" not in d or "y" not in d:
        if "target" in d:
            raise DeviceError(
                f"local mode can't ground the natural-language target "
                f"{d['target']!r}. Use x/y coordinates, capture by hand with "
                f"--manual, or use the cloud (Revyl) backend.")
        raise DeviceError(f"local `{action}` needs x/y coordinates: {d}")
    return str(d["x"]), str(d["y"])


def sim_translate(step, bundle):
    """Translate a flow step into a local-simulator action, or raise if the
    local backend can't perform it. Returns (kind, payload) or None (disabled).
    """
    disabled_flag = False
    for key in FLAG_ACTIONS:
        if key in step:
            if not step[key]:
                disabled_flag = True
                continue
            if key == "kill_app":
                if not bundle:
                    raise DeviceError("local `kill_app` needs app.bundle_id in the flow")
                return ("term", bundle)
            raise DeviceError(f"`{key}` isn't supported on a local iOS simulator")

    if "navigate" in step:
        return ("url", str(step["navigate"]))
    if "launch" in step:
        return ("launch", str(step["launch"]))
    if "wait" in step:
        return ("wait", float(step["wait"]) / 1000.0)
    if "tap" in step:
        x, y = _sim_coords(step["tap"], "tap")
        return ("idb", ["ui", "tap", x, y])
    if "double_tap" in step:
        x, y = _sim_coords(step["double_tap"], "double_tap")
        return ("idb2", ["ui", "tap", x, y])
    if "long_press" in step:
        d = step["long_press"]
        x, y = _sim_coords(d, "long_press")
        return ("idb", ["ui", "tap", "--duration", str(d.get("duration", 1.0)), x, y])
    if "drag" in step:
        d = step["drag"]
        return ("idb", ["ui", "swipe",
                        str(d["start_x"]), str(d["start_y"]),
                        str(d["end_x"]), str(d["end_y"])])
    if "swipe" in step:
        raise DeviceError("local mode: use `drag` with explicit start/end coords "
                          "instead of `swipe` (no NL grounding locally)")
    if "type" in step:
        return ("idb", ["ui", "text", str(step["type"].get("text", ""))])
    if "instruction" in step:
        raise DeviceError("natural-language `instruction` needs the cloud (Revyl) "
                          "backend; not available on a local simulator")
    if disabled_flag:
        return None
    raise DeviceError(f"Unrecognized/unsupported local step: {step}")


class SimDriver:
    """Backend: a booted local iOS simulator via `xcrun simctl` (+ `idb`)."""

    label = "local simulator"
    source = "a local iOS simulator"
    supports_attach = False

    def __init__(self):
        self.bundle = None

    def start(self, flow, timeout):
        _ensure_booted_sim()
        app = flow.get("app", {})
        self.bundle = app.get("bundle_id")
        path = app.get("app_path") or app.get("app_url")
        if path and os.path.exists(str(path)):
            simctl(["install", "booted", str(path)])
            print(f"  -> installed {path}")
        if self.bundle:
            simctl(["launch", "booted", self.bundle])
            print(f"  -> launched {self.bundle}")
        else:
            print("  -> using the app already on the booted simulator")

    def attach(self, session_id):
        raise DeviceError("--attach isn't supported on a local simulator")

    def screenshot(self, path):
        return simctl_screenshot(path)

    def translate(self, step):
        return sim_translate(step, self.bundle)

    def describe(self, action):
        return action[0] if action else "(disabled)"

    def perform(self, action):
        if action is None:
            return
        kind, payload = action
        if kind == "url":
            simctl(["openurl", "booted", payload])
        elif kind == "launch":
            simctl(["launch", "booted", payload])
        elif kind == "term":
            simctl(["terminate", "booted", payload])
        elif kind == "wait":
            time.sleep(payload)
        elif kind in ("idb", "idb2"):
            if not _idb_available():
                raise DeviceError("this step needs `idb` to control the simulator UI. "
                                  "Install it (https://fbidb.io), or capture by hand "
                                  "with --manual.")
            idb(payload)
            if kind == "idb2":  # double tap
                idb(payload)
        else:  # pragma: no cover
            raise DeviceError(f"unknown sim action: {kind}")

    def teardown(self, keep, attach):
        print("  -> left the simulator running")


# --------------------------------------------------------------------------- #
# Capture
# --------------------------------------------------------------------------- #

def _write_manifest(frames_dir, frames):
    manifest = [{"file": p.name, "hold": h, "label": lbl} for p, h, lbl in frames]
    (frames_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))


def capture_flow(flow, frames_dir, driver, attach=None, timeout=600, keep=False):
    cap = flow["capture"]
    frames_dir.mkdir(parents=True, exist_ok=True)
    out_hold = flow["output"]["hold"]

    # Translate + validate every step up front so a bad/unsupported action or a
    # non-numeric hold/settle fails fast, before we provision a device.
    plan = []
    for step in flow["steps"]:
        action = driver.translate(step)
        try:
            float(step.get("hold", out_hold))
            float(step.get("settle", cap["settle"]))
        except (TypeError, ValueError):
            raise DeviceError(f"step has a non-numeric hold/settle: {step}")
        plan.append((step, action))

    print(f"\n== Capturing flow '{flow['name']}' on the {driver.label} ==")
    if attach:
        if not driver.supports_attach:
            raise DeviceError(f"--attach isn't supported on the {driver.label}")
        driver.attach(attach)
    else:
        driver.start(flow, timeout)

    frames = []  # list of (path, hold, label)

    def grab(idx, hold, label):
        path = frames_dir / f"state_{idx:03d}.png"
        if driver.screenshot(path):
            frames.append((path, hold, label))
            print(f"  [{idx:02d}] captured {label or ''}")

    try:
        idx = 0
        if cap["initial"]:
            time.sleep(cap["launch_settle"])
            grab(idx, out_hold, "initial")
            idx += 1

        for step, action in plan:
            label = step.get("label", "")
            if action is None:
                print(f"  step: (disabled) {label}")
            else:
                print(f"  step: {driver.describe(action)} {label}")
                try:
                    driver.perform(action)
                except DeviceError as exc:
                    # Salvage the run: stop here and render what we captured.
                    print(f"  ! step failed, ending flow early: {exc}")
                    break
            settle = float(step.get("settle", cap["settle"]))
            if settle:
                time.sleep(settle)
            if step.get("capture", True):
                grab(idx, float(step.get("hold", out_hold)), step.get("label"))
                idx += 1
    finally:
        driver.teardown(keep, attach)
        # Always record a manifest (even on error) so whatever frames were
        # captured can still be re-rendered with `--dry-run`.
        _write_manifest(frames_dir, frames)

    return frames


def capture_manual(flow, frames_dir, driver, keep=False, attach=None):
    """Interactive capture: you drive the device, press Enter to grab each
    screen. The natural fit for a local simulator (no UI automation needed)."""
    if not sys.stdin.isatty():
        sys.exit("--manual needs an interactive terminal.")
    frames_dir.mkdir(parents=True, exist_ok=True)
    out_hold = flow["output"]["hold"]

    print(f"\n== Manual capture on the {driver.label} ==")
    if attach and driver.supports_attach:
        driver.attach(attach)
    else:
        driver.start(flow, timeout=flow.get("timeout", 600))

    print("Drive the app yourself. Press Enter to capture each screen; "
          "type q then Enter to finish.")
    frames = []
    try:
        idx = 0
        while True:
            resp = input(f"  [{idx:02d}] Enter = capture, q = finish: ").strip().lower()
            if resp in ("q", "quit", "done"):
                break
            path = frames_dir / f"state_{idx:03d}.png"
            if driver.screenshot(path):
                frames.append((path, out_hold, f"state {idx}"))
                print(f"       captured {path.name}")
                idx += 1
    finally:
        driver.teardown(keep, attach)
        _write_manifest(frames_dir, frames)

    return frames


def load_captured(frames_dir, default_hold, use_manifest=True):
    """Reload frames for a `--dry-run`. Prefers the capture manifest (so
    per-state holds/labels survive) and falls back to a uniform hold."""
    frames_dir = Path(frames_dir)
    manifest = frames_dir / "manifest.json"
    if use_manifest and manifest.exists():
        try:
            entries = json.loads(manifest.read_text())
        except (json.JSONDecodeError, OSError):
            entries = []
        out = []
        for e in entries:
            p = frames_dir / e["file"]
            if p.exists():
                out.append((p, float(e.get("hold", default_hold)), e.get("label")))
        if out:
            return out

    paths = sorted(frames_dir.glob("state_*.png"))
    if not paths:
        sys.exit(f"No captured frames in {frames_dir} (expected state_*.png).")
    return [(p, default_hold, p.stem) for p in paths]


# --------------------------------------------------------------------------- #
# Compositing + timeline
# --------------------------------------------------------------------------- #

def _phash(image, size=8):
    """Tiny average-hash for de-duping near-identical consecutive frames."""
    small = image.convert("L").resize((size, size), Image.LANCZOS)
    pixels = list(small.getdata())
    avg = sum(pixels) / len(pixels)
    return tuple(1 if p > avg else 0 for p in pixels)


def _hamming(a, b):
    return sum(x != y for x, y in zip(a, b))


def composite_states(frames, frame_opts, dedup):
    """Frame each captured screenshot; optionally merge identical neighbours."""
    states = []  # list of (PIL RGBA image, hold seconds, label)
    prev_hash = None
    for path, hold, label in frames:
        with Image.open(path) as raw:
            # Hash the raw screenshot, NOT the framed composite -- the bezel and
            # background are identical across every frame and would otherwise
            # dominate the hash and merge genuinely distinct UI states.
            h = _phash(raw)
            if dedup and prev_hash is not None and _hamming(h, prev_hash) <= 1:
                # Same UI state: extend the previous hold instead of adding a frame.
                img, prev_hold, prev_label = states[-1]
                states[-1] = (img, prev_hold + hold, prev_label)
                continue
            framed = device_frame.build_device_frame(
                raw,
                style=frame_opts["style"],
                color=frame_opts["color"],
                background=frame_opts["background"],
                shadow=frame_opts["shadow"],
                screen_width=frame_opts["width"],
                buttons=frame_opts["buttons"],
            )
        states.append((framed, hold, label))
        prev_hash = h
    return states


def build_sequence(states, out_dir, fps, xfade, loop, pingpong, matte):
    """Render the full PNG frame sequence (holds + crossfades) for ffmpeg."""
    out_dir.mkdir(parents=True, exist_ok=True)
    # Clear any stale frames from a previous render so ffmpeg's sequential
    # %05d reader can't pick up leftovers when the new render is shorter.
    for old in out_dir.glob("*.png"):
        old.unlink()

    matte_rgb = _matte(matte)
    flat = [(device_frame.flatten(img, matte_rgb), hold) for img, hold, _ in states]

    # Normalize every state to a common canvas so crossfade blends never hit a
    # size mismatch (e.g. if a step rotated the device to landscape).
    tw = max(im.width for im, _ in flat)
    th = max(im.height for im, _ in flat)
    norm = []
    for im, hold in flat:
        if im.size != (tw, th):
            canvas = Image.new("RGB", (tw, th), matte_rgb)
            canvas.paste(im, ((tw - im.width) // 2, (th - im.height) // 2))
            im = canvas
        norm.append((im, hold))
    flat = norm

    order = list(range(len(flat)))
    if pingpong and len(flat) > 2:
        order += list(range(len(flat) - 2, 0, -1))

    n = 0

    def write(img):
        nonlocal n
        img.save(out_dir / f"{n:05d}.png")
        n += 1

    xfade_frames = max(0, int(round(xfade * fps)))

    for pos, si in enumerate(order):
        img, hold = flat[si]
        for _ in range(max(1, int(round(hold * fps)))):
            write(img)
        # crossfade into the next state in the order
        nxt = order[pos + 1] if pos + 1 < len(order) else (order[0] if loop else None)
        if nxt is not None and xfade_frames and nxt != si:
            target = flat[nxt][0]
            for k in range(1, xfade_frames + 1):
                write(Image.blend(img, target, k / (xfade_frames + 1)))

    return n


def _matte(value):
    if isinstance(value, str) and value.startswith("#"):
        try:
            return device_frame.hex_to_rgb(value)
        except ValueError:
            pass
    return (255, 255, 255)


# --------------------------------------------------------------------------- #
# Encoding (ffmpeg)
# --------------------------------------------------------------------------- #

def encode_gif(seq_dir, fps, out_path, loop):
    pattern = str(seq_dir / "%05d.png")
    vf = ("[0:v]split[a][b];[a]palettegen=stats_mode=diff[p];"
          "[b][p]paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle")
    # ffmpeg gif: -loop 0 = loop forever, -loop -1 = play once.
    cmd = ["ffmpeg", "-y", "-framerate", str(fps), "-i", pattern,
           "-filter_complex", vf, "-loop", "0" if loop else "-1", str(out_path)]
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def encode_mp4(seq_dir, fps, out_path):
    pattern = str(seq_dir / "%05d.png")
    cmd = ["ffmpeg", "-y", "-framerate", str(fps), "-i", pattern,
           "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
           "-movflags", "+faststart", str(out_path)]
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def _human(num_bytes):
    for unit in ("B", "KB", "MB"):
        if num_bytes < 1024:
            return f"{num_bytes:.0f}{unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f}GB"


# --------------------------------------------------------------------------- #
# Preview (a self-contained HTML viewer compiled next to the outputs)
# --------------------------------------------------------------------------- #

_PREVIEW_STYLE = """<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh;
    background: radial-gradient(120% 120% at 50% 0%, #1c1833 0%, #0c0b14 60%, #07070b 100%);
    color: #e9e7f2;
    font: 15px/1.5 -apple-system, BlinkMacSystemFont, "SF Pro Text", Segoe UI, sans-serif;
    display: flex; flex-direction: column; align-items: center; padding: 48px 24px 64px;
  }
  h1 { font-size: 22px; font-weight: 650; margin: 0 0 4px; letter-spacing: -0.01em; }
  .sub { color: #a59fc9; margin: 0 0 36px; font-size: 14px; }
  .row { display: flex; gap: 40px; flex-wrap: wrap; justify-content: center; align-items: flex-start; }
  .card { display: flex; flex-direction: column; align-items: center; gap: 14px; }
  .frame { background: #ffffff08; border: 1px solid #ffffff14; border-radius: 22px;
           padding: 22px; box-shadow: 0 24px 70px -20px #000a; }
  .frame img, .frame video { display: block; width: 300px; height: auto; border-radius: 10px; }
  .label { font-weight: 600; }
  .meta { color: #8e88b5; font-size: 12.5px; }
  .pill { font-size: 11px; color: #cfc9f0; background: #ffffff12; border: 1px solid #ffffff1c;
          padding: 2px 9px; border-radius: 999px; }
  footer { margin-top: 40px; color: #6f6a93; font-size: 12.5px; text-align: center; }
</style>"""


def _preview_card(path):
    attr = html.escape(path.name, quote=True)   # used inside "..." attributes
    text = html.escape(path.name)
    meta = _human(path.stat().st_size)
    if path.suffix == ".gif":
        media = f'<img src="{attr}" alt="{attr}">'
        pill = "GIF · loops natively"
    else:
        media = f'<video src="{attr}" autoplay loop muted playsinline controls></video>'
        pill = "MP4 · autoplay loop"
    return (f'<div class="card"><span class="pill">{pill}</span>'
            f'<div class="frame">{media}</div>'
            f'<div class="label">{text}</div><div class="meta">{meta}</div></div>')


def write_preview(out_dir, flow, outputs, loop_seconds, source="a device"):
    """Compile a self-contained HTML viewer next to the GIF/MP4 outputs."""
    f = flow["frame"]
    cards = "\n      ".join(_preview_card(p) for p in outputs
                            if p.suffix in (".gif", ".mp4"))
    name = html.escape(str(flow["name"]))
    sub = html.escape(f"{f['style']} · {f['color']} · {f['background']} bg · "
                      f"{loop_seconds:.1f}s loop")
    footer = html.escape(f"Generated by device-gif-maker on {source}")
    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>{name} — device-gif-maker</title>
{_PREVIEW_STYLE}
</head><body>
  <h1>{name}</h1>
  <p class="sub">{sub}</p>
  <div class="row">
      {cards}
  </div>
  <footer>{footer}</footer>
</body></html>
"""
    path = out_dir / "preview.html"
    path.write_text(doc)
    return path


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main():
    # Optional leading subcommand: `revyl-gif local ...` selects the local
    # simulator backend; otherwise the Revyl cloud backend is used.
    raw = sys.argv[1:]
    mode = "cloud"
    if raw and raw[0] == "local":
        mode, raw = "local", raw[1:]

    p = argparse.ArgumentParser(
        prog="revyl-gif",
        description="Any flow -> a clean looping GIF on a pristine device frame. "
                    "Prefix with `local` to drive a booted iOS simulator instead "
                    "of a Revyl cloud device.")
    p.add_argument("flow", nargs="?", default=None,
                   help="Flow spec (.yaml/.yml/.json). Optional in `local` mode.")
    p.add_argument("--out", default=None, help="Output directory (default: ./out/<name>)")
    p.add_argument("--attach", help="Attach to an existing cloud session id")
    p.add_argument("--timeout", type=int, default=600, help="Cloud session idle timeout (s)")
    p.add_argument("--keep-session", action="store_true",
                   help="Don't stop the cloud session when done")
    p.add_argument("--manual", action="store_true",
                   help="Drive the device yourself; press Enter to capture each screen")
    p.add_argument("--dry-run", action="store_true",
                   help="Skip the device; re-render from already-captured frames")
    # quick overrides (otherwise taken from the flow spec / defaults)
    p.add_argument("--style", choices=["iphone-pro", "iphone", "android"])
    p.add_argument("--color", choices=list(device_frame.COLOR_SCHEMES))
    p.add_argument("--background")
    p.add_argument("--width", type=int)
    p.add_argument("--fps", type=int)
    p.add_argument("--hold", type=float)
    p.add_argument("--xfade", type=float)
    p.add_argument("--no-shadow", action="store_true")
    p.add_argument("--pingpong", action="store_true")
    p.add_argument("--no-loop", action="store_true")
    p.add_argument("--no-gif", action="store_true")
    p.add_argument("--no-mp4", action="store_true")
    p.add_argument("--open", action="store_true",
                   help="Open the preview HTML when done (default: only in a TTY)")
    p.add_argument("--no-open", action="store_true",
                   help="Never open the preview HTML")
    args = p.parse_args(raw)

    if args.flow:
        flow = load_flow(args.flow)
    elif mode == "local" or args.dry_run:
        flow = default_flow(Path(args.out).name if args.out else "sim-capture")
    else:
        p.error("a flow file is required for cloud mode (or use `local` / --manual)")

    # apply CLI overrides (use `is not None` so 0 / 0.0 are honoured, not dropped)
    f, o = flow["frame"], flow["output"]
    if args.style:
        f["style"] = args.style
    if args.color:
        f["color"] = args.color
    if args.background:
        f["background"] = args.background
    if args.width is not None:
        f["width"] = args.width
    if args.no_shadow:
        f["shadow"] = False
    if args.fps is not None:
        o["fps"] = args.fps
    if args.hold is not None:
        o["hold"] = args.hold
    if args.xfade is not None:
        o["xfade"] = args.xfade
    if args.pingpong:
        o["pingpong"] = True
    if args.no_loop:
        o["loop"] = False
    if args.no_gif:
        o["gif"] = False
    if args.no_mp4:
        o["mp4"] = False

    # validate the values that would otherwise blow up mid-render
    if o["fps"] <= 0:
        sys.exit(f"output.fps must be > 0 (got {o['fps']}).")
    if f["width"] <= 0:
        sys.exit(f"frame.width must be > 0 (got {f['width']}).")
    bg = f["background"]
    if (isinstance(bg, str) and not bg.startswith("#")
            and bg not in device_frame.BACKGROUND_PRESETS):
        print(f"  ! unknown background '{bg}'; falling back to 'light'. "
              f"Options: {', '.join(device_frame.BACKGROUND_PRESETS)} or a #hex color.")

    out_dir = Path(args.out or Path("out") / flow["name"])
    frames_dir = out_dir / "frames"
    seq_dir = out_dir / "seq"
    out_dir.mkdir(parents=True, exist_ok=True)

    driver = SimDriver() if mode == "local" else CloudDriver()

    # 1. capture (or reuse) raw screenshots
    try:
        if args.dry_run:
            print(f"== Dry run: re-rendering from {frames_dir} ==")
            # An explicit --hold means "uniform hold"; else honour the manifest.
            frames = load_captured(frames_dir, o["hold"], use_manifest=(args.hold is None))
        elif args.manual or (mode == "local" and not flow["steps"]):
            frames = capture_manual(flow, frames_dir, driver,
                                    keep=args.keep_session, attach=args.attach)
        else:
            frames = capture_flow(flow, frames_dir, driver, attach=args.attach,
                                  timeout=args.timeout, keep=args.keep_session)
    except DeviceError as exc:
        sys.exit(f"error: {exc}")
    if not frames:
        sys.exit("No frames captured -- aborting.")

    print(f"\n== Framing {len(frames)} states ==")
    states = composite_states(frames, f, flow["capture"]["dedup"])
    print(f"  -> {len(states)} distinct states after de-dup")

    # 3. render timeline + encode
    print("== Rendering timeline ==")
    total = build_sequence(states, seq_dir, o["fps"], o["xfade"],
                           o["loop"], o["pingpong"], o["matte"])
    print(f"  -> {total} frames at {o['fps']}fps "
          f"({total / o['fps']:.1f}s loop)")

    outputs = []
    if o["gif"]:
        gif_path = out_dir / f"{flow['name']}.gif"
        print("== Encoding GIF ==")
        encode_gif(seq_dir, o["fps"], gif_path, o["loop"])
        outputs.append(gif_path)
    if o["mp4"]:
        mp4_path = out_dir / f"{flow['name']}.mp4"
        print("== Encoding MP4 ==")
        encode_mp4(seq_dir, o["fps"], mp4_path)
        outputs.append(mp4_path)

    # compile a self-contained HTML viewer next to the outputs
    preview = (write_preview(out_dir, flow, outputs, total / o["fps"], source=driver.source)
               if outputs else None)

    print("\nDone:")
    for path in outputs:
        print(f"  {path}  ({_human(path.stat().st_size)})")
    if preview:
        print(f"  {preview}  (open in a browser)")
        if args.no_open:
            open_it = False
        elif args.open:
            open_it = True
        else:
            # Auto-open only in an interactive terminal that plausibly has a GUI
            # (avoids hijacking a headless SSH TTY with a console browser).
            gui = (sys.platform in ("darwin", "win32")
                   or os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
            open_it = sys.stdout.isatty() and bool(gui)
        if open_it:
            try:
                webbrowser.open(preview.resolve().as_uri())
                print("  -> opened preview in your browser")
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    main()
