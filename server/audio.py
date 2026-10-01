"""The audio store: what clients upload goes in, one endless time-stamped stream of 16 kHz mono samples comes out.

Only this module knows how audio is kept (today: each uploaded piece as its own file under the recordings dir,
indexed in the pieces table). The pipeline reads `stream(after)` and the playback reads `cut()`; neither cares
about formats, files or piece lengths. The stream pauses when nothing new has arrived and when the reader stops
asking (it pulls at its own pace), and a gap between two pieces is only a jump in their start times.
"""
import asyncio
import datetime as dt
import logging
import re
import subprocess
from pathlib import Path

import numpy as np

import db

log = logging.getLogger("live")
SR = 16000
DIR: Path | None = None  # the recordings dir
read_to_ms = 0           # start of the last piece the stream reader has finished with: later ones are still queued


def decode(src: Path | bytes) -> np.ndarray | None:
    """An audio file (a path or its bytes) as 16 kHz mono int16; None when ffmpeg cannot read it."""
    args, inp = (["-i", str(src)], None) if isinstance(src, Path) else (["-i", "pipe:0"], src)
    out = subprocess.run(["ffmpeg", "-v", "error", *args, "-f", "s16le", "-ac", "1", "-ar", str(SR), "pipe:1"], input=inp, capture_output=True)
    if out.returncode or len(out.stdout) < 2 * SR // 10:
        return None
    return np.frombuffer(out.stdout[:len(out.stdout) // 2 * 2], np.int16)


def save(device: str, start_ms: int, body: bytes, content_type: str) -> float | None:
    """Keeps one uploaded piece (start_ms: its device time); returns its seconds, None when it is not audio.
    It is on disk and in the index before this returns, so the client may delete its copy once told so."""
    x = decode(body)
    if x is None:
        return None
    ext = {"audio/ogg": "ogg", "audio/webm": "webm", "audio/aac": "aac"}.get(content_type.split(";")[0], "bin")
    path = Path("chunks") / re.sub(r"[^\w.-]", "_", device) / f"{start_ms}.{ext}"
    (DIR / path).parent.mkdir(parents=True, exist_ok=True)
    (DIR / path).write_bytes(body)
    db.conn().execute("INSERT OR REPLACE INTO pieces VALUES (?, ?, ?, ?)", (device, start_ms, round(len(x) / SR, 3), str(path)))
    return len(x) / SR


def queued() -> int:
    """Pieces the stream has not handed out yet."""
    return db.conn().execute("SELECT count(*) FROM pieces WHERE start_ms > ?", (read_to_ms,)).fetchone()[0]


async def stream(after: dt.datetime | None):
    """(start time, samples, device) in time order, from the piece holding `after` (the beginning when None), forever.
    A piece that arrives older than one already handed out is kept for playback but never streamed."""
    global read_to_ms
    ms = int(after.timestamp() * 1000) if after else 0
    p = db.conn().execute("SELECT * FROM pieces WHERE start_ms + seconds * 1000 > ? ORDER BY start_ms LIMIT 1", (ms,)).fetchone()
    read_to_ms = p["start_ms"] - 1 if p else ms
    while True:
        p = db.conn().execute("SELECT * FROM pieces WHERE start_ms > ? ORDER BY start_ms LIMIT 1", (read_to_ms,)).fetchone()
        if p is None:
            await asyncio.sleep(1)
            continue
        x = await asyncio.to_thread(decode, DIR / p["path"])
        if x is not None:
            yield dt.datetime.fromtimestamp(p["start_ms"] / 1000), x, p["device"]
        read_to_ms = p["start_ms"]  # after the yield: the reader came back for more, so this one is done


def cut(a: dt.datetime, b: dt.datetime, fmt: str) -> bytes | None:
    """[a, b] (+0.2 s each side) as `fmt` (wav, s16le), or None when no stored audio covers it."""
    a_ms, b_ms = int(a.timestamp() * 1000), int(b.timestamp() * 1000)
    pieces = [(r["start_ms"], DIR / r["path"]) for r in db.conn().execute(
        "SELECT start_ms, path FROM pieces WHERE start_ms <= ? AND start_ms + seconds * 1000 >= ? ORDER BY start_ms", (b_ms, a_ms))]
    if not pieces:
        return None
    # concatenated back to back (the small gaps between pieces are ignored)
    off = max(0.0, (a_ms - pieces[0][0]) / 1000 - 0.2)
    ins = [x for _, p in pieces for x in ("-i", str(p))]
    out = subprocess.run(["ffmpeg", "-v", "error", *ins, "-filter_complex", "".join(f"[{i}:a]" for i in range(len(pieces))) +
                          f"concat=n={len(pieces)}:v=0:a=1[a]", "-map", "[a]", "-ss", f"{off:.3f}", "-t", f"{(b - a).total_seconds() + 0.4:.3f}",
                          "-ac", "1", "-ar", str(SR), "-f", fmt, "pipe:1"], capture_output=True)
    if out.returncode or len(out.stdout) <= 44:
        log.warning("could not cut %s: %s", pieces[0][1].name, out.stderr.decode()[-200:])
        return None
    return out.stdout


if __name__ == "__main__":  # the stream's order, resume point and late pieces, on synthetic tones
    import tempfile
    DIR = Path(tempfile.mkdtemp())
    db.open_db(DIR / "t.db")
    tone = lambda s: subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=f=440:d={s}", "-c:a", "libopus", "-f", "ogg", "pipe:1"],
                                    capture_output=True, check=True).stdout
    T = lambda m: int(dt.datetime(2026, 10, 2, 10, m).timestamp() * 1000)
    for m in (0, 1, 5):  # 10:00, 10:01, a gap, 10:05
        assert save("phone", T(m), tone(60), "audio/ogg") > 59
    assert save("phone", T(9), b"not audio", "audio/ogg") is None

    async def take(after, n):
        out, s = [], stream(after)
        async for t, x, _ in s:
            out.append(t.minute)
            if len(out) == n:
                break
        await s.aclose()
        return out
    assert asyncio.run(take(None, 3)) == [0, 1, 5]
    assert asyncio.run(take(dt.datetime(2026, 10, 2, 10, 1, 30), 2)) == [1, 5], "resumes from the piece holding `after`"
    assert queued() == 1, "the last piece handed out counts until the reader comes back"
    assert len(cut(dt.datetime(2026, 10, 2, 10, 0, 59), dt.datetime(2026, 10, 2, 10, 1, 1), "s16le")) > 2 * SR * 2
    print("ok")
