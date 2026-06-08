#!/usr/bin/env python3
"""device-gif-maker -- any flow -> a clean looping GIF on a pristine device frame.

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
# instead of silently navigating away.
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

    revyl(args)
    print("  -> device session ready")


def step_to_argv(step):
    """Translate one flow step into `revyl device ...` arguments.

    A step is a dict with exactly one action key plus optional control keys
    (capture / hold / label / settle). Targets may be natural language
    (`target:`) or coordinates (`x:`/`y:`). Returns the argv list, or `None`
    for a recognized-but-disabled flag action (e.g. `back: false`) so the
    runner can treat it as a capture-only no-op. Raises RevylError if no known
    action key is present, so flow-authoring typos fail fast (before any
    device is provisioned).
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

    # No-value flag actions: dispatch on truthiness, not mere presence.
    for key, cmd in FLAG_ACTIONS.items():
        if key in step:
            return ["device", cmd] if step[key] else None

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

    # Translate every step up front so an unknown action key fails fast,
    # before we provision a (billable, concurrency-limited) device.
    plan = [(step, step_to_argv(step)) for step in flow["steps"]]

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

    try:
        idx = 0
        if cap["initial"]:
            time.sleep(cap["launch_settle"])
            grab(idx, flow["output"]["hold"], "initial")
            idx += 1

        for step, argv in plan:
            label = step.get("label", "")
            if argv is None:
                print(f"  step: (disabled) {label}")
            else:
                print(f"  step: {argv[1]} {label}")
                try:
                    revyl(argv)
                except RevylError as exc:
                    # Salvage the run: stop here and render what we captured
                    # rather than discarding the session's work.
                    print(f"  ! step failed, ending flow early: {exc}")
                    break
            settle = float(step.get("settle", cap["settle"]))
            if settle:
                time.sleep(settle)
            if step.get("capture", True):
                grab(idx, float(step.get("hold", flow["output"]["hold"])),
                     step.get("label"))
                idx += 1
    finally:
        # Always release the cloud session we started, even on error -- a
        # leaked session blocks every later run under a concurrency cap.
        if not keep and not attach:
            revyl(["device", "stop", "--all"], check=False, quiet=True)
            print("  -> session stopped")

    # Record per-frame hold/label so `--dry-run` can reproduce the exact
    # timeline without re-driving the device.
    manifest = [{"file": p.name, "hold": h, "label": lbl} for p, h, lbl in frames]
    (frames_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

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
    name = path.name
    meta = _human(path.stat().st_size)
    if path.suffix == ".gif":
        media = f'<img src="{name}" alt="{name}">'
        pill = "GIF · loops natively"
    else:
        media = f'<video src="{name}" autoplay loop muted playsinline controls></video>'
        pill = "MP4 · autoplay loop"
    return (f'<div class="card"><span class="pill">{pill}</span>'
            f'<div class="frame">{media}</div>'
            f'<div class="label">{name}</div><div class="meta">{meta}</div></div>')


def write_preview(out_dir, flow, outputs, loop_seconds):
    """Compile a self-contained HTML viewer next to the GIF/MP4 outputs."""
    f = flow["frame"]
    cards = "\n      ".join(_preview_card(p) for p in outputs
                            if p.suffix in (".gif", ".mp4"))
    sub = (f"{f['style']} · {f['color']} · {f['background']} bg · "
           f"{loop_seconds:.1f}s loop")
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>{flow['name']} — device-gif-maker</title>
{_PREVIEW_STYLE}
</head><body>
  <h1>{flow['name']}</h1>
  <p class="sub">{sub}</p>
  <div class="row">
      {cards}
  </div>
  <footer>Generated by device-gif-maker on a real Revyl cloud device</footer>
</body></html>
"""
    path = out_dir / "preview.html"
    path.write_text(html)
    return path


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
    p.add_argument("--open", action="store_true",
                   help="Open the preview HTML when done (default: only in a TTY)")
    p.add_argument("--no-open", action="store_true",
                   help="Never open the preview HTML")
    args = p.parse_args()

    flow = load_flow(args.flow)

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

    # 1. capture (or reuse) raw screenshots
    if args.dry_run:
        print(f"== Dry run: re-rendering from {frames_dir} ==")
        # An explicit --hold means "uniform hold"; otherwise honour the manifest.
        frames = load_captured(frames_dir, o["hold"], use_manifest=(args.hold is None))
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

    # compile a self-contained HTML viewer next to the outputs
    preview = write_preview(out_dir, flow, outputs, total / o["fps"]) if outputs else None

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
            open_it = sys.stdout.isatty()
        if open_it:
            try:
                webbrowser.open(preview.resolve().as_uri())
                print("  -> opened preview in your browser")
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    main()
