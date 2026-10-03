"""The audio store: what clients upload goes in, one endless time-stamped stream of 16 kHz mono samples comes out.

Only this module knows how audio is kept (today: each uploaded piece appended unchanged to its device's file for the
hour it starts in, chunks/<device>/<date>/<HH>.<ext>, and indexed in the pieces table by path, pos and size; one file a
piece made 3600 tiny files an hour while the app was in front). Each device is its own stream: the pipeline of a device reads `stream(device, after)`, the playback reads `cut()`;
neither cares about formats, files or piece lengths. A stream pauses when nothing new has arrived and when its reader
stops asking (it pulls at its own pace), and a gap between two pieces is only a jump in their start times. A device
that sends a backlog of older audio while another one is recording is streamed on its own, not dropped.
"""
import asyncio
import datetime as dt
import io
import logging
import os
import re
import subprocess
import threading
import wave
from pathlib import Path

import numpy as np

import db

log = logging.getLogger("live")
SR = 16000
DIR: Path | None = None  # the recordings dir
read_to: dict[str, int] = {}  # per device: start of the last piece its reader has finished with; later ones are queued
_append = threading.Lock()  # uploads are saved in threads: one append (and its index row) at a time


AAC_DELAY = 2112  # samples an AAC encoder puts before the audio (at the iPhone's 16 kHz: 132 ms)
AAC_FRAME = 1024  # it pads the end to whole frames: 0 to 1023 samples, unknown, so half is cut


def unpad(x: np.ndarray) -> np.ndarray:
    """An ADTS AAC piece without its encoder's delay and (on average) its last-frame padding. Each iPhone piece is a
    new encoder: decoded, a piece was 0.18 s longer than its audio, overlapped the next piece and spliced 0.1 s of
    silence into speech at every piece; this leaves at most +-32 ms."""
    return x[AAC_DELAY:len(x) - AAC_FRAME // 2] if len(x) > AAC_DELAY + AAC_FRAME else x


def decode(body: bytes, aac: bool = False, n: int | None = None) -> np.ndarray | None:
    """One piece's bytes as 16 kHz mono int16; None when ffmpeg cannot read them. aac: an ADTS piece, cut to the n
    samples its client wrote (exact), or unpadded when it did not say (older apps, a piece cut short by a kill)."""
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-f", "s16le", "-ac", "1", "-ar", str(SR), "pipe:1"], input=body, capture_output=True)
    if out.returncode or len(out.stdout) < 2 * SR // 10:
        return None
    x = np.frombuffer(out.stdout[:len(out.stdout) // 2 * 2], np.int16)
    if aac and n:
        return x[AAC_DELAY:AAC_DELAY + n]
    return unpad(x) if aac else x


def hour_file(device: str, start_ms: int, ext: str) -> Path:
    """Where a piece starting at start_ms goes, relative to DIR: its device's file for that (local) hour."""
    t = dt.datetime.fromtimestamp(start_ms / 1000)
    return Path("chunks") / re.sub(r"[^\w.-]", "_", device) / f"{t:%Y-%m-%d}" / f"{t:%H}.{ext}"


def append(path: Path, body: bytes) -> int:
    """Appends body to DIR / path and returns where it starts; on disk (fsync) when this returns. Hold _append."""
    f = DIR / path
    new = not f.exists()
    f.parent.mkdir(parents=True, exist_ok=True)
    with open(f, "ab") as h:
        pos = h.seek(0, os.SEEK_END)  # a failed write before leaves bytes no row points at: harmless
        h.write(body)
        h.flush()
        os.fsync(h.fileno())
    if new:  # the file's name is durable too
        d = os.open(f.parent, os.O_RDONLY)
        os.fsync(d)
        os.close(d)
    return pos


def save(device: str, start_ms: int, body: bytes, content_type: str, n: int | None = None) -> float | None:
    """Keeps one uploaded piece (start_ms: its device time, n: the samples its client wrote, when it says); returns
    its seconds, None when it is not audio. It is on disk and in the index before this returns, so the client may
    delete its copy once told so. A piece sent again (same device and start) is stored once: the first copy stays,
    the resend only gets its seconds back."""
    stored = lambda: db.conn().execute("SELECT seconds FROM pieces WHERE device = ? AND start_ms = ?", (device, start_ms)).fetchone()
    if r := stored():
        return r[0]
    x = decode(body, aac=content_type.split(";")[0] == "audio/aac", n=n)
    if x is None:
        return None
    ext = {"audio/ogg": "ogg", "audio/webm": "webm", "audio/aac": "aac"}.get(content_type.split(";")[0], "bin")
    path = hour_file(device, start_ms, ext)
    with _append:
        if r := stored():  # the same piece in two requests at once
            return r[0]
        pos = append(path, body)
        db.conn().execute("INSERT INTO pieces VALUES (?, ?, ?, ?, ?, ?, ?)",
                          (device, start_ms, round(len(x) / SR, 3), str(path), pos, len(body), n))
    return len(x) / SR


def samples(p) -> np.ndarray | None:
    """A pieces row's audio, decoded."""
    with open(DIR / p["path"], "rb") as f:
        f.seek(p["pos"])
        body = f.read(p["size"])
    return decode(body, aac=p["path"].endswith(".aac"), n=p["n"])


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
        x = await asyncio.to_thread(samples, p)
        if x is not None:
            yield dt.datetime.fromtimestamp(p["start_ms"] / 1000), x
        read_to[device] = p["start_ms"]  # after the yield: the reader came back for more, so this one is done


def cut(a: dt.datetime, b: dt.datetime, fmt: str, device: str | None = None) -> bytes | None:
    """[a, b] (+0.2 s each side) of one device (any when None) as `fmt` (wav, s16le), or None when no stored audio
    covers it."""
    a_ms, b_ms = int(a.timestamp() * 1000), int(b.timestamp() * 1000)
    pieces = db.conn().execute(
        "SELECT * FROM pieces WHERE start_ms <= ? AND start_ms + seconds * 1000 >= ? AND (? IS NULL OR device = ?) ORDER BY start_ms",
        (b_ms, a_ms, device, device)).fetchall()
    xs = [x for x in map(samples, pieces) if x is not None]
    if not xs:
        return None
    # concatenated back to back (the small gaps between pieces are ignored)
    i = int(max(0.0, (a_ms - pieces[0]["start_ms"]) / 1000 - 0.2) * SR)
    x = np.concatenate(xs)[i:i + int(((b - a).total_seconds() + 0.4) * SR)]
    if not len(x):
        return None
    if fmt == "s16le":
        return x.tobytes()
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(x.tobytes())
    return out.getvalue()


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
    files = [p.relative_to(DIR).as_posix() for p in DIR.rglob("*.ogg")]
    assert files == ["chunks/phone/2026-10-02/10.ogg"], files
    size = (DIR / files[0]).stat().st_size
    assert save("phone", T(1), tone(30), "audio/ogg") > 59, "sent again: the first copy's seconds"
    assert (DIR / files[0]).stat().st_size == size and db.conn().execute("SELECT count(*) FROM pieces").fetchone()[0] == 3, \
        "a piece sent again is stored once: the hour file does not grow, the row stays"
    x = np.arange(16000, dtype=np.int16)
    assert len(unpad(x)) == 16000 - AAC_DELAY - AAC_FRAME // 2 and unpad(x)[0] == AAC_DELAY, "AAC: the encoder's delay and half a frame go"
    assert len(unpad(x[:3000])) == 3000, "too short to hold both: left alone"
    # an iPhone piece of n = 16333 samples (not whole frames). Apple's encoder puts 2112 samples before them; ffmpeg's
    # puts 1024, so 1088 of silence in front stand in for the rest (aac_at, Apple's own, exists only on a Mac)
    aac = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=f=440:r=16000", "-af", "atrim=end_sample=16333,adelay=1088S",
                          "-c:a", "aac", "-b:a", "24k", "-f", "adts", "pipe:1"], capture_output=True, check=True).stdout
    assert len(decode(aac)) == 19 * AAC_FRAME, len(decode(aac))  # 2112 + 16333, padded to whole frames
    assert save("iphone", T(20), aac, "audio/aac", n=16333) == 16333 / SR
    assert len(samples(db.conn().execute("SELECT * FROM pieces WHERE start_ms = ?", (T(20),)).fetchone())) == 16333
    assert len(decode(aac, aac=True)) == len(decode(aac)) - AAC_DELAY - AAC_FRAME // 2, "no n: unpadded as before"

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
    with wave.open(io.BytesIO(cut(dt.datetime(2026, 10, 2, 10, 0, 59), dt.datetime(2026, 10, 2, 10, 1, 1), "wav", "phone"))) as w:
        assert w.getframerate() == SR and abs(w.getnframes() - 2.4 * SR) < 2, w.getnframes()
    print("ok")
