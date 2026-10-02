"""The audio store: what clients upload goes in, one endless time-stamped stream of 16 kHz mono samples comes out.

Only this module knows how audio is kept (today: each uploaded piece as its own file under the recordings dir,
indexed in the pieces table). Each device is its own stream: the pipeline of a device reads `stream(device, after)`, the playback reads `cut()`;
neither cares about formats, files or piece lengths. A stream pauses when nothing new has arrived and when its reader
stops asking (it pulls at its own pace), and a gap between two pieces is only a jump in their start times. A device
that sends a backlog of older audio while another one is recording is streamed on its own, not dropped.
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
read_to: dict[str, int] = {}  # per device: start of the last piece its reader has finished with; later ones are queued


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


def queued(device: str) -> int:
    """Pieces of a device its stream has not handed out yet."""
    return db.conn().execute("SELECT count(*) FROM pieces WHERE device = ? AND start_ms > ?", (device, read_to.get(device, -1))).fetchone()[0]


def backlog(device: str, after: dt.datetime | None) -> tuple[float, dt.datetime | None]:
    """Seconds of a device's stored audio after `after` (its finalized time), and where its stored audio ends."""
    ms = int(after.timestamp() * 1000) if after else 0
    s, end = db.conn().execute("SELECT sum(min(seconds, (start_ms + seconds * 1000 - ?) / 1000.0)), max(start_ms + seconds * 1000) "
                               "FROM pieces WHERE device = ? AND start_ms + seconds * 1000 > ?", (ms, device, ms)).fetchone()
    last = db.conn().execute("SELECT max(start_ms + seconds * 1000) FROM pieces WHERE device = ?", (device,)).fetchone()[0]
    return round(s or 0.0, 1), dt.datetime.fromtimestamp(last / 1000) if last else None


def pending(after: dict[str, dt.datetime | None]) -> list[str]:
    """Devices with audio after their finalized time (`after`, by device; a device not in it has none yet)."""
    return [r[0] for r in db.conn().execute("SELECT device, max(start_ms + seconds * 1000) FROM pieces GROUP BY device")
            if r[1] > (int(after[r[0]].timestamp() * 1000) if after.get(r[0]) else 0)]


async def stream(device: str, after: dt.datetime | None):
    """(start time, samples) of one device in time order, from the piece holding `after` (its first piece when None),
    forever. A piece arriving older than one already handed out is kept for playback but not streamed."""
    ms = int(after.timestamp() * 1000) if after else 0
    p = db.conn().execute("SELECT * FROM pieces WHERE device = ? AND start_ms + seconds * 1000 > ? ORDER BY start_ms LIMIT 1", (device, ms)).fetchone()
    read_to[device] = p["start_ms"] - 1 if p else ms
    while True:
        p = db.conn().execute("SELECT * FROM pieces WHERE device = ? AND start_ms > ? ORDER BY start_ms LIMIT 1", (device, read_to[device])).fetchone()
        if p is None:
            await asyncio.sleep(1)
            continue
        x = await asyncio.to_thread(decode, DIR / p["path"])
        if x is not None:
            yield dt.datetime.fromtimestamp(p["start_ms"] / 1000), x
        read_to[device] = p["start_ms"]  # after the yield: the reader came back for more, so this one is done


def cut(a: dt.datetime, b: dt.datetime, fmt: str, device: str | None = None) -> bytes | None:
    """[a, b] (+0.2 s each side) of one device (any when None) as `fmt` (wav, s16le), or None when no stored audio
    covers it."""
    a_ms, b_ms = int(a.timestamp() * 1000), int(b.timestamp() * 1000)
    pieces = [(r["start_ms"], DIR / r["path"]) for r in db.conn().execute(
        "SELECT start_ms, path FROM pieces WHERE start_ms <= ? AND start_ms + seconds * 1000 >= ? AND (? IS NULL OR device = ?) ORDER BY start_ms",
        (b_ms, a_ms, device, device))]
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

    async def take(after, n, device="phone"):
        out, s = [], stream(device, after)
        async for t, x in s:
            out.append(t.minute)
            if len(out) == n:
                break
        await s.aclose()
        return out
    assert asyncio.run(take(None, 3)) == [0, 1, 5]
    assert asyncio.run(take(dt.datetime(2026, 10, 2, 10, 1, 30), 2)) == [1, 5], "resumes from the piece holding `after`"
    assert queued("phone") == 1, "the last piece handed out counts until the reader comes back"
    assert save("iphone", T(2), tone(30), "audio/aac") > 29  # another device, inside phone's gap
    assert asyncio.run(take(None, 1, "iphone")) == [2], "each device streams on its own"
    assert set(pending({"phone": dt.datetime(2026, 10, 2, 10, 6)})) == {"iphone"}, "phone done, iphone has none finalized"
    assert cut(dt.datetime(2026, 10, 2, 10, 2, 5), dt.datetime(2026, 10, 2, 10, 2, 6), "s16le", "phone") is None
    left, end = backlog("phone", dt.datetime(2026, 10, 2, 10, 1, 30))
    assert 89 < left < 91 and end == dt.datetime(2026, 10, 2, 10, 6), (left, end)  # 30 s of 10:01 + all of 10:05
    assert len(cut(dt.datetime(2026, 10, 2, 10, 0, 59), dt.datetime(2026, 10, 2, 10, 1, 1), "s16le")) > 2 * SR * 2
    print("ok")
