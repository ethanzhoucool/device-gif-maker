#!/usr/bin/env python3
"""revyl-gifmaker -- any flow -> a clean looping GIF on a pristine device frame.

Drives a Revyl cloud device through a flow (defined in YAML/JSON), captures a
screenshot at every UI state, frames each one in a pristine device mockup, and
encodes a smooth, seamlessly-looping GIF (+ optional MP4) ready to drop into a
README or a tweet.

    revyl-gif flows/example.yaml
    revyl-gif flows/example.yaml --attach <session-id>
    revyl-gif flows/example.yaml --dry-run        # re-render from captured frames

Built on the Revyl CLI (`revyl device ...`). See README.md.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
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


def _deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


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


# --------------------------------------------------------------------------- #
# Revyl CLI plumbing
# --------------------------------------------------------------------------- #

class RevylError(RuntimeError):
    pass


def revyl(args, *, quiet=False, check=True, capture=True):
    cmd = ["revyl", *args]
    if not quiet:
        print(f"  $ {' '.join(cmd)}")
    proc = subprocess.run(cmd, capture_output=capture, text=True)
    if check and proc.returncode != 0:
        raise RevylError(
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

    proc = revyl(args)
    session_id = _find_session_id(proc.stdout)
    print(f"  -> session {session_id or '(active)'} ready")
    return session_id


def _find_session_id(stdout):
    """Best-effort: pull a session id out of `device start --json` output."""
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return None

    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, str) and "session" in k.lower() and "id" in k.lower():
                    return v
                found = walk(v)
                if found:
                    return found
        elif isinstance(obj, list):
            for item in obj:
                found = walk(item)
                if found:
                    return found
        return None

    return walk(data)


def step_to_argv(step):
    """Translate one flow step into `revyl device ...` arguments.

    A step is a dict with exactly one action key plus optional control keys
    (capture / hold / label / settle). Targets may be natural language
    (`target:`) or coordinates (`x:`/`y:`).
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
    if "kill_app" in step:
        return ["device", "kill-app"]
    if "key" in step:
        return ["device", "key", str(step["key"])]
    if "back" in step:
        return ["device", "back"]
    if "home" in step:
        return ["device", "home"]
    if "shake" in step:
        return ["device", "shake"]
    if "wait" in step:
        secs = float(step["wait"]) / (1000.0 if float(step["wait"]) > 50 else 1.0)
        return ["device", "wait", str(secs)]
    if "instruction" in step:
        return ["device", "instruction", str(step["instruction"])]
    raise RevylError(f"Unrecognized step (no known action key): {step}")


def screenshot(out_path, retries=2):
    last = None
    for attempt in range(retries + 1):
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


# --------------------------------------------------------------------------- #
# Capture
# --------------------------------------------------------------------------- #

def capture_flow(flow, frames_dir, attach=None, timeout=600, keep=False):
    cap = flow["capture"]
    frames_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n== Capturing flow '{flow['name']}' on a cloud device ==")
    if attach:
        revyl(["device", "attach", attach])
    else:
        start_session(flow, timeout)

    frames = []  # list of (path, hold, label)

    def grab(idx, hold, label):
        path = frames_dir / f"state_{idx:03d}.png"
        if screenshot(path):
            frames.append((path, hold, label))
            print(f"  [{idx:02d}] captured {label or ''}")

    idx = 0
    if cap["initial"]:
        time.sleep(cap["launch_settle"])
        grab(idx, flow["output"]["hold"], "initial")
        idx += 1

    for step in flow["steps"]:
        argv = step_to_argv(step)
        print(f"  step: {argv[1]} {step.get('label', '')}")
        revyl(argv)
        settle = float(step.get("settle", cap["settle"]))
        if settle:
            time.sleep(settle)
        if step.get("capture", True):
            grab(idx, float(step.get("hold", flow["output"]["hold"])),
                 step.get("label"))
            idx += 1

    if not keep and not attach:
        try:
            revyl(["device", "stop", "--all"], check=False)
            print("  -> session stopped")
        except RevylError:
            pass

    return frames


def load_captured(frames_dir, default_hold):
    paths = sorted(Path(frames_dir).glob("state_*.png"))
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
            framed = device_frame.build_device_frame(
                raw,
                style=frame_opts["style"],
                color=frame_opts["color"],
                background=frame_opts["background"],
                shadow=frame_opts["shadow"],
                screen_width=frame_opts["width"],
                buttons=frame_opts["buttons"],
            )
        h = _phash(framed)
        if dedup and prev_hash is not None and _hamming(h, prev_hash) <= 1:
            # Identical UI state: extend the previous hold instead of adding a frame.
            img, prev_hold, prev_label = states[-1]
            states[-1] = (img, prev_hold + hold, prev_label)
            continue
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
    flat = [(device_frame.flatten(img, _matte(matte)), hold) for img, hold, _ in states]

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
        return device_frame._hex_to_rgb(value)
    return (255, 255, 255)


# --------------------------------------------------------------------------- #
# Encoding (ffmpeg)
# --------------------------------------------------------------------------- #

def encode_gif(seq_dir, fps, out_path, loop):
    pattern = str(seq_dir / "%05d.png")
    vf = ("[0:v]split[a][b];[a]palettegen=stats_mode=diff[p];"
          "[b][p]paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle")
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
# CLI
# --------------------------------------------------------------------------- #

def main():
    p = argparse.ArgumentParser(
        description="Any flow -> a clean looping GIF on a pristine device frame.")
    p.add_argument("flow", help="Path to a flow spec (.yaml / .yml / .json)")
    p.add_argument("--out", default=None, help="Output directory (default: ./out/<name>)")
    p.add_argument("--attach", help="Attach to an existing device session id")
    p.add_argument("--timeout", type=int, default=600, help="Session idle timeout (s)")
    p.add_argument("--keep-session", action="store_true",
                   help="Don't stop the device session when done")
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
    args = p.parse_args()

    flow = load_flow(args.flow)

    # apply CLI overrides
    f, o = flow["frame"], flow["output"]
    if args.style:
        f["style"] = args.style
    if args.color:
        f["color"] = args.color
    if args.background:
        f["background"] = args.background
    if args.width:
        f["width"] = args.width
    if args.no_shadow:
        f["shadow"] = False
    if args.fps:
        o["fps"] = args.fps
    if args.hold:
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

    out_dir = Path(args.out or Path("out") / flow["name"])
    frames_dir = out_dir / "frames"
    seq_dir = out_dir / "seq"
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. capture (or reuse) raw screenshots
    if args.dry_run:
        print(f"== Dry run: re-rendering from {frames_dir} ==")
        frames = load_captured(frames_dir, o["hold"])
    else:
        frames = capture_flow(flow, frames_dir, attach=args.attach,
                              timeout=args.timeout, keep=args.keep_session)
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

    print("\nDone:")
    for path in outputs:
        print(f"  {path}  ({_human(path.stat().st_size)})")


if __name__ == "__main__":
    main()
