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
import time
import wave
from pathlib import Path

import numpy as np

import db

log = logging.getLogger("live")
SR = 16000
DIR: Path | None = None  # the recordings dir
read_to: dict[str, int] = {}  # per device: start of the last piece its reader has finished with; later ones are queued
HOLD_MAX_S = 12 * 3600  # a hold not said again for this long is let go: a phone that never comes back must not stop finals for good
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
    if out.returncode or len(out.stdout) < 2:  # an Opus piece can be one 20 ms packet (the last one of a run)
        return None
    x = np.frombuffer(out.stdout[:len(out.stdout) // 2 * 2], np.int16)
    if aac and n:
        return x[AAC_DELAY:AAC_DELAY + n]
    return unpad(x) if aac else x


HUM = np.exp(-2j * np.pi * np.outer(np.arange(SR), np.arange(50, 251, 50)) / SR)  # a second's DFT at 50..250 Hz
ZERO_RUN = SR // 100  # exact zeros this long (10 ms) in a row: the mic sent nothing (the recorder's high-pass turns a stuck input into 0)


def health(x: np.ndarray) -> tuple[float, float, int, int]:
    """How a piece sounds, measured as lr_listen.py does each second (no content: levels only): RMS and mains hum (the
    50..250 Hz harmonics, each second's, averaged) in dBFS, ms in runs of exact zeros of 10 ms or more (no data from
    the mic: a loose wire, #155), and full-scale samples (clipping)."""
    f = x.astype(np.float64)
    hum = sum((2 * np.abs(f[i:i + SR] @ HUM[:len(f[i:i + SR])]) ** 2).sum() / len(f[i:i + SR]) for i in range(0, len(f), SR))
    edges = np.flatnonzero(np.diff(np.r_[0, x == 0, 0]))  # where each run of zeros starts and ends
    runs = edges[1::2] - edges[::2]
    dbfs = lambda ms: round(10 * np.log10(max(ms, 1e-3) / 32768 ** 2), 1)
    return (dbfs(np.mean(f * f) if len(f) else 0), dbfs(hum / max(len(f), 1)), int(runs[runs >= ZERO_RUN].sum()) * 1000 // SR,
            int(np.count_nonzero((x >= 32767) | (x <= -32767))))


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
    delete its copy once told so. Of a piece sent again (same device and start) the longer copy (in bytes) is kept: a
    resend as long or shorter only gets the kept copy's seconds back; a longer one replaces the row, appended to the
    hour file (an Android batch retried with more pieces after the answer to the first try was lost)."""
    kept = lambda: db.conn().execute("SELECT seconds FROM pieces WHERE device = ? AND start_ms = ? AND size >= ?",
                                     (device, start_ms, len(body))).fetchone()
    if r := kept():
        return r[0]
    ext = {"audio/ogg": "ogg", "audio/webm": "webm", "audio/aac": "aac"}.get(content_type.split(";")[0], "bin")
    x = decode(body, aac=ext == "aac", n=n)
    if x is None:  # the client drops it on the 400 that follows: its bytes stay here, for a look later (#155)
        f = DIR / "rejected" / re.sub(r"[^\w.-]", "_", device) / f"{start_ms}.{ext}"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(body)
        return None
    path = hour_file(device, start_ms, ext)
    h = health(x)
    with _append:
        if r := kept():  # the same piece in two requests at once
            return r[0]
        pos = append(path, body)
        db.conn().execute("INSERT OR REPLACE INTO pieces (device, start_ms, seconds, path, pos, size, n, received_ms, rms, hum, zero_ms, full) "
                          "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          (device, start_ms, round(len(x) / SR, 3), str(path), pos, len(body), n, int(time.time() * 1000), *h))
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


def minutes(device: str, lo_ms: int, hi_ms: int) -> list[dict]:
    """How a device's audio starting in [lo_ms, hi_ms) sounded and arrived, a row per minute it has audio in (pieces
    from before 10-07 have neither): its seconds; RMS and hum in dBFS (energy-weighted); s in zero runs and full-scale
    samples (audio.health); lag_s: how long after its end the minute's median piece arrived."""
    out = {}
    for p in db.conn().execute("SELECT * FROM pieces WHERE device = ? AND start_ms >= ? AND start_ms < ? AND rms IS NOT NULL ORDER BY start_ms",
                               (device, lo_ms, hi_ms)):
        m = out.setdefault(p["start_ms"] // 60_000, {"s": 0.0, "rms": 0.0, "hum": 0.0, "zero_s": 0.0, "full": 0, "lag": []})
        m["s"] += p["seconds"]
        m["rms"] += 10 ** (p["rms"] / 10) * p["seconds"]
        m["hum"] += 10 ** (p["hum"] / 10) * p["seconds"]
        m["zero_s"] += p["zero_ms"] / 1000
        m["full"] += p["full"]
        if p["received_ms"]:
            m["lag"].append((p["received_ms"] - p["start_ms"]) / 1000 - p["seconds"])
    dbfs = lambda e, s: round(10 * np.log10(max(e / s, 1e-12)), 1) if s else None
    return [{"t": dt.datetime.fromtimestamp(k * 60).isoformat(timespec="seconds"), "s": round(m["s"], 1), "rms": dbfs(m["rms"], m["s"]),
             "hum": dbfs(m["hum"], m["s"]), "zero_s": round(m["zero_s"], 2), "full": m["full"],
             "lag_s": round(sorted(m["lag"])[len(m["lag"]) // 2], 1) if m["lag"] else None} for k, m in out.items()]


def arrival(device: str) -> dict | None:
    """The device's newest audio: when it came (unix ms) and how long after its end (s): what is said now, how late."""
    p = db.conn().execute("SELECT * FROM pieces WHERE device = ? ORDER BY start_ms DESC LIMIT 1", (device,)).fetchone()
    return p and p["received_ms"] and {"at": p["received_ms"], "lag_s": round((p["received_ms"] - p["start_ms"]) / 1000 - p["seconds"], 1)}


def pending(after: dict[str, dt.datetime | None]) -> list[str]:
    """Devices with audio after their finalized time (`after`, by device; a device not in it has none yet)."""
    return [r[0] for r in db.conn().execute("SELECT device, max(start_ms + seconds * 1000) FROM pieces GROUP BY device")
            if r[1] > (int(after[r[0]].timestamp() * 1000) if after.get(r[0]) else 0)]


def hold(device: str, at_ms: int | None):
    """A phone still has audio of `device` older than at_ms to send (a backlog it reads off the recorder's card while it
    sends what is said now, #130): the device's queue (stream without edge_s) hands out nothing from at_ms on until
    that audio is in, so none of it arrives behind the reader. None: nothing older is coming. Every upload says it
    again; kept across a restart."""
    db.put_kv(f"hold:{device}", None if at_ms is None else {"at": at_ms, "seen": time.time()})


def held(device: str) -> int | None:
    """Where the device's queue waits (unix ms); None when it does not (no hold, or one not said for HOLD_MAX_S)."""
    h = db.kv(f"hold:{device}")
    return h["at"] if h and time.time() - h["seen"] < HOLD_MAX_S else None


def hold_seen(device: str) -> float | None:
    """When a phone last said the device's hold (unix s): it lets go HOLD_MAX_S after that."""
    h = db.kv(f"hold:{device}")
    return h and h["seen"]


async def stream(device: str, after: dt.datetime | None, edge_s: float | None = None):
    """(start time, samples) of one device in time order, from the piece holding `after` (its first piece when None),
    forever. A piece arriving older than one already handed out is kept for playback but not streamed: a phone that
    sends what is said now ahead of older audio holds the queue (hold()) until the older audio is in. edge_s: only
    the device's newest edge_s seconds (before its newest piece's end or the clock, whichever is later), for a reader
    of what is said now: older pieces, a backlog arriving, are skipped; such a reader is not the device's queue
    (read_to, queued)."""
    ms = int(after.timestamp() * 1000) if after else 0
    p = db.conn().execute("SELECT * FROM pieces WHERE device = ? AND start_ms + seconds * 1000 > ? ORDER BY start_ms LIMIT 1", (device, ms)).fetchone()
    # from `after` (or the piece holding it), not from the first piece there is now: that one may be held, older
    # audio after `after` still on its way (#130: a pipeline started mid catch-up took 3 such pieces as late)
    at, lo = min(p["start_ms"] - 1, ms) if p else ms, 0
    while True:
        if edge_s is None:
            read_to[device] = at  # the reader came back for more, so the piece it had is done
        else:
            last = db.conn().execute("SELECT start_ms + seconds * 1000 FROM pieces WHERE device = ? ORDER BY start_ms DESC LIMIT 1", (device,)).fetchone()
            lo = max(last[0] if last else 0, time.time() * 1000) - edge_s * 1000
        p = db.conn().execute("SELECT * FROM pieces WHERE device = ? AND start_ms > ? AND start_ms + seconds * 1000 > ? ORDER BY start_ms LIMIT 1",
                              (device, at, lo)).fetchone()
        if p is not None and edge_s is None and (h := held(device)) is not None and p["start_ms"] >= h:
            p = None  # older audio is still on its way: it goes first
        if p is None:
            await asyncio.sleep(1)
            continue
        x = await asyncio.to_thread(samples, p)
        if x is not None:
            yield dt.datetime.fromtimestamp(p["start_ms"] / 1000), x
        at = p["start_ms"]


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
    return x.tobytes() if fmt == "s16le" else wav(x)


def wav(x: np.ndarray) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(x.tobytes())
    return out.getvalue()


LISTEN_BACK_S = 30  # a listener asking from further back than this before a device's newest audio skips ahead


def newer(device: str, after: int | None) -> tuple[int, int, np.ndarray] | None:
    """For listening in on a device: its pieces starting after `after` (unix ms; None: its newest piece only), within
    LISTEN_BACK_S of its newest audio (a page back from the background, a backlog arriving), back to back; with the
    first one's start and the last one's (the next `after`). None when there is none."""
    last = db.conn().execute("SELECT start_ms, seconds FROM pieces WHERE device = ? ORDER BY start_ms DESC LIMIT 1", (device,)).fetchone()
    if last is None:
        return None
    lo = last["start_ms"] + last["seconds"] * 1000 - LISTEN_BACK_S * 1000
    ps = db.conn().execute("SELECT * FROM pieces WHERE device = ? AND start_ms > ? AND start_ms + seconds * 1000 > ? ORDER BY start_ms",
                           (device, last["start_ms"] - 1 if after is None else after, lo)).fetchall()
    xs = [x for x in map(samples, ps) if x is not None]
    return (ps[0]["start_ms"], ps[-1]["start_ms"], np.concatenate(xs)) if xs else None


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
    assert (DIR / f"rejected/phone/{T(9)}.ogg").read_bytes() == b"not audio", "an undecodable piece's bytes are kept"
    assert 0 < save("phone", T(8), tone(0.02), "audio/ogg") < 0.1, "a 20 ms piece is audio too"
    db.conn().execute("DELETE FROM pieces WHERE start_ms = ?", (T(8),))
    files = [p.relative_to(DIR).as_posix() for p in (DIR / "chunks").rglob("*.ogg")]
    assert files == ["chunks/phone/2026-10-02/10.ogg"], files
    # how pieces sound (no content): ffmpeg's sine is at 1/8 of full scale (-21 dBFS RMS); 50 Hz is hum, 440 Hz is not
    i = np.arange(2 * SR)
    hum = (0.25 * 32767 * np.sin(2 * np.pi * 50 * i / SR)).astype(np.int16)
    assert health(hum)[:2] == (-15.1, -15.1), health(hum)
    assert health((0.25 * 32767 * np.sin(2 * np.pi * 440 * i / SR)).astype(np.int16))[1] < -60, "440 Hz is not hum"
    broken = hum.copy()
    broken[1000:1800], broken[5000:5100], broken[9000:9010], broken[9500] = 0, 0, 32767, -32768
    assert health(broken)[2:] == (50, 11), "50 ms of zero runs >= 10 ms (the 6 ms one does not count), 11 full-scale samples"
    assert health(np.zeros(1, np.int16)) == (-120.3, -120.3, 0, 0)
    p = db.conn().execute("SELECT * FROM pieces WHERE start_ms = ?", (T(0),)).fetchone()
    assert abs(p["rms"] + 21.1) < 0.3 and p["hum"] < -60 and p["zero_ms"] == p["full"] == 0, dict(p)
    assert 0 <= time.time() * 1000 - p["received_ms"] < 60_000, "when it came"
    m = minutes("phone", T(0), T(2))
    assert [r["t"][11:16] for r in m] == ["10:00", "10:01"] and m[0]["s"] > 59 and abs(m[0]["rms"] + 21.1) < 0.3, m
    assert m[0]["lag_s"] > 3600, "arrived long after (this test runs after 2026-10-02 10:01)"
    db.conn().execute("UPDATE pieces SET rms = -40, hum = -40, zero_ms = 1500, full = 3, received_ms = start_ms + 61000 WHERE start_ms = ?", (T(1),))
    assert minutes("phone", T(1), T(2))[0] | {"t": 0, "s": 0} == {"t": 0, "s": 0, "rms": -40.0, "hum": -40.0, "zero_s": 1.5, "full": 3, "lag_s": 1.0}
    assert arrival("phone")["lag_s"] > 3600 and arrival("nobody") is None
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

    async def held_back():  # 10:05 came first, the phone still has older audio: 10:03 is to go before it
        out, s = [], stream("phone", None)
        go = asyncio.create_task(anext(s))
        hold("phone", T(5))
        for _ in range(2):
            out.append((await go)[0].minute)
            go = asyncio.create_task(anext(s))
        await asyncio.sleep(2.5)
        assert out == [0, 1] and not go.done(), "10:05 waits"
        assert save("phone", T(3), tone(60), "audio/ogg") > 59
        out.append((await asyncio.wait_for(go, 5))[0].minute)
        go = asyncio.create_task(anext(s))
        await asyncio.sleep(2.5)
        assert out == [0, 1, 3] and not go.done(), "still held"
        hold("phone", None)
        out.append((await asyncio.wait_for(go, 5))[0].minute)
        assert out == [0, 1, 3, 5], out
        await s.aclose()
    asyncio.run(held_back())
    hold("phone", T(5))
    assert held("phone") == T(5) and asyncio.run(take(dt.datetime(2026, 10, 2, 10, 4), 1, "iphone")) == [20], "a hold is the device's own"

    async def started_held():  # the reader starts while all there is after `after` is held: older audio still goes first
        s = stream("phone", dt.datetime(2026, 10, 2, 10, 4))
        go = asyncio.create_task(anext(s))
        await asyncio.sleep(1.5)
        assert not go.done(), "10:05 is held"
        assert save("phone", T(4) + 30_000, tone(20), "audio/ogg") > 19  # 10:04:30, after `after`, before 10:05
        t = (await asyncio.wait_for(go, 5))[0]
        await s.aclose()
        return t
    assert asyncio.run(started_held()) == dt.datetime(2026, 10, 2, 10, 4, 30), "not passed over for the held piece"
    db.put_kv("hold:phone", {"at": T(5), "seen": time.time() - HOLD_MAX_S - 1})
    assert held("phone") is None, "a hold not said again for HOLD_MAX_S is let go"
    assert asyncio.run(take(dt.datetime(2026, 10, 2, 10, 4), 2)) == [4, 5]
    print("ok")
