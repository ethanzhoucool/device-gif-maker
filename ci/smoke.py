#!/usr/bin/env python3
"""CI smoke test: exercise the full framing + timeline + encode pipeline on
synthetic frames, with no cloud device. Proves device_frame.py + the ffmpeg
encoders work end-to-end via the `--dry-run` path.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
tmp = Path(tempfile.mkdtemp()) / "smoke"
frames = tmp / "frames"
frames.mkdir(parents=True)

# A few distinct synthetic "app screens" so de-dup keeps them and crossfades show.
SCREENS = [
    ("#0E0E12", "#1DB954"),
    ("#101427", "#4F8CFF"),
    ("#16101F", "#D8CFFE"),
    ("#0B0B0B", "#1DB954"),
]
for i, (bg, accent) in enumerate(SCREENS):
    im = Image.new("RGB", (1179, 2556), bg)
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((70, 260 + i * 60, 1109, 760 + i * 60), radius=48, fill=accent)
    for r in range(5):
        y = 900 + r * 220
        d.rounded_rectangle((70, y, 1109, y + 170), radius=32, fill="#22232A")
    im.save(frames / f"state_{i:03d}.png")

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
