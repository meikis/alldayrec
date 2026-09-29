"""Replay recorder files into a directory at real-time pace, so server.py can be tested against a known day.

    uv run python feed.py <out-dir> <speed> <recordings/2026-09-28_21-00-00.ogg> [...]

Each file is written under its own name at `speed` x real time (bytes proportional to elapsed
audio); the sidecar JSON of the previous segment is copied first so current_device() works.
"""
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

out, speed, files = Path(sys.argv[1]), float(sys.argv[2]), [Path(p) for p in sys.argv[3:]]
out.mkdir(exist_ok=True)
for src in files:
    side = src.with_suffix(".json")
    if side.exists():
        shutil.copy(side, out / side.name)  # closed-segment sidecar: device name
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(src)],
                               capture_output=True, text=True).stdout)
    data = src.read_bytes()
    t0, done = time.time(), 0
    with open(out / src.name, "wb") as f:
        while done < len(data):
            want = min(len(data), int(len(data) * (time.time() - t0) * speed / dur))
            f.write(data[done:want]); f.flush(); done = want
            time.sleep(0.2)
    print("finished", src.name, f"{dur:.0f}s audio in {time.time() - t0:.0f}s", flush=True)
