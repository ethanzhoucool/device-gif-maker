#!/usr/bin/env python3
"""CI smoke test: exercise the full framing + timeline + encode pipeline on
synthetic frames, with no cloud device. Proves device_frame.py + the ffmpeg
encoders work end-to-end via the `--dry-run` path, and that de-dup keeps
distinct screens while merging identical ones.
"""

import atexit
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import revyl_gif  # noqa: E402

_root = Path(tempfile.mkdtemp())
atexit.register(shutil.rmtree, _root, ignore_errors=True)
tmp = _root / "smoke"
frames = tmp / "frames"
frames.mkdir(parents=True)

# Distinct synthetic "app screens": same overall layout, but a recolored and
# shifted accent block -- exactly the case a grayscale average-hash used to
# wrongly merge. De-dup must keep all four.
SCREENS = [
    ("#0E0E12", "#1DB954"),
    ("#101427", "#4F8CFF"),
    ("#16101F", "#D8CFFE"),
    ("#0B0B0B", "#1DB954"),
]


def make_screen(i, bg, accent):
    im = Image.new("RGB", (1179, 2556), bg)
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((70, 260 + i * 60, 1109, 760 + i * 60), radius=48, fill=accent)
    for r in range(5):
        y = 900 + r * 220
        d.rounded_rectangle((70, y, 1109, y + 170), radius=32, fill="#22232A")
    return im


screens = [make_screen(i, bg, accent) for i, (bg, accent) in enumerate(SCREENS)]
for i, im in enumerate(screens):
    im.save(frames / f"state_{i:03d}.png")

# --- de-dup unit checks (the regression that motivated the color signature) ---
opts = revyl_gif.DEFAULTS["frame"]
thr = revyl_gif.DEFAULTS["capture"]["dedup_threshold"]

distinct = revyl_gif.composite_states(
    [(frames / f"state_{i:03d}.png", 1.0, None) for i in range(len(screens))],
    opts, dedup=True, threshold=thr,
)
assert len(distinct) == len(screens), (
    f"de-dup dropped distinct screens: kept {len(distinct)} of {len(screens)}")

# Two pixel-identical screens must collapse to one held state.
same = revyl_gif.composite_states(
    [(frames / "state_000.png", 1.0, None), (frames / "state_000.png", 1.0, None)],
    opts, dedup=True, threshold=thr,
)
assert len(same) == 1, f"de-dup failed to merge identical frames: kept {len(same)}"
print(f"OK  de-dup kept {len(distinct)}/{len(screens)} distinct, merged identical -> 1")

# --- end-to-end render through the CLI --dry-run path ---
cmd = [
    sys.executable, str(ROOT / "revyl_gif.py"), str(ROOT / "flows" / "example.yaml"),
    "--dry-run", "--out", str(tmp), "--fps", "20", "--width", "360", "--no-open",
]
print("running:", " ".join(cmd))
subprocess.run(cmd, check=True, cwd=ROOT)

gif = tmp / "search-and-play.gif"
mp4 = tmp / "search-and-play.mp4"
assert gif.exists() and gif.stat().st_size > 0, "GIF was not produced"
assert mp4.exists() and mp4.stat().st_size > 0, "MP4 was not produced"
print(f"OK  gif={gif.stat().st_size}B  mp4={mp4.stat().st_size}B")

# --- GIF timing: one frame per held state / crossfade step, exact loop length ---
# 4 states x (1.3s hold = 26 frames + 0.45s crossfade = 9 frames) at 20fps:
# 140 timeline frames = 7.0s, but only 4 + 4 * 9 = 40 distinct GIF frames.
seq_frames = len(list((tmp / "seq").glob("*.png")))
assert seq_frames == 140, f"expected 140 timeline frames, got {seq_frames}"
with Image.open(gif) as im:
    delays = []
    for i in range(im.n_frames):
        im.seek(i)
        delays.append(im.info["duration"])
assert len(delays) == 40, f"expected 40 GIF frames (holds collapsed), got {len(delays)}"
assert sum(delays) == 7000, f"GIF loop is {sum(delays)}ms, expected 7000ms"
assert delays.count(1300) == 4, f"each held state should be one 1.3s frame: {delays}"
print(f"OK  gif timing: {len(delays)} frames for {seq_frames} timeline frames, {sum(delays)}ms loop")

# Above 50fps a GIF can't express the per-frame delay (browsers slow 0-1cs
# frames down to ~10cs), so delays are clamped to 2cs and the loop length kept.
runs = [(0, 84)] + [(84 + k, 1) for k in range(27)] + [(111, 72)]
d60 = revyl_gif._gif_delays(runs, 60)
assert min(d60) >= 2 and sum(d60) == round(183 * 100 / 60), d60
assert revyl_gif._gif_delays([(0, 26), (26, 1), (27, 1)], 20) == [130, 5, 5]
print("OK  gif delays never drop below 2cs and keep the loop length")
