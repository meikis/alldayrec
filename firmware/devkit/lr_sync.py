"""Reference client for the live-record offline store (src/lr_store.h): sync the clock, take the stored files,
put every frame back on the wall clock, write one WAV per continuous run. The phone and browser clients do the same.
    uv run --no-project --with bleak python lr_sync.py OUT_DIR [--delete]
"""
import asyncio
import ctypes
import datetime as dt
import struct
import sys
import time
import wave
from pathlib import Path

from bleak import BleakClient, BleakScanner

U = lambda n: f"7c3e{n:04x}-1a2b-4c5d-8e9f-6c6976657265"
TIME, CTRL, DATA = U(1), U(2), U(3)
FRAME_MS = 10


def records(b: bytes):
    """('A', first_frame, [opus...]) and ('T', frame, epoch_ms) in file order; a cut-short tail is dropped."""
    i = 0
    while i < len(b):
        if b[i] == 0x41 and i + 6 <= len(b):
            n, first = b[i + 1], struct.unpack_from("<I", b, i + 2)[0]
            j, frames = i + 6, []
            for _ in range(n):
                if j >= len(b) or j + 1 + b[j] > len(b):
                    return
                frames.append(b[j + 1:j + 1 + b[j]])
                j += 1 + b[j]
            yield "A", first, frames
            i = j
        elif b[i] == 0x54 and i + 13 <= len(b):
            yield "T", *struct.unpack_from("<IQ", b, i + 1)
            i += 13
        else:
            return


def runs(files: dict[int, bytes], live: tuple[int, int, int]):
    """files: file id -> bytes. live: (boot, frame, epoch_ms) from this connection's sync. Yields
    (epoch_ms of the first frame or None, [opus...]) per run of consecutive frames."""
    sync, audio = {live[0]: [live[1:]]}, {}
    for f in sorted(files):
        for r in records(files[f]):
            (sync if r[0] == "T" else audio).setdefault(f >> 16, []).append(r[1:])
    for boot, recs in sorted(audio.items()):
        ts = sync.get(boot)
        cur = None
        for first, frames in recs:
            if cur and first == cur[0] + len(cur[1]):
                cur[1].extend(frames)
                continue
            if cur:
                yield cur_time(ts, cur[0]), cur[1]
            cur = [first, list(frames)]
        if cur:
            yield cur_time(ts, cur[0]), cur[1]


def cur_time(ts, frame):
    """The boot's sync nearest the frame (a later one is as good: the mic's clock runs on in between)."""
    if not ts:
        return None
    f, e = min(ts, key=lambda t: abs(t[0] - frame))
    return e + (frame - f) * FRAME_MS


def decode(frames) -> bytes:
    lib = ctypes.CDLL("/opt/homebrew/lib/libopus.dylib")
    lib.opus_decoder_create.restype = ctypes.c_void_p
    err = ctypes.c_int()
    dec = ctypes.c_void_p(lib.opus_decoder_create(16000, 1, ctypes.byref(err)))
    buf, out = (ctypes.c_int16 * 960)(), bytearray()
    for f in frames:
        n = lib.opus_decode(dec, f, len(f), buf, 960, 0)
        out += bytes(buf)[: 2 * max(n, 0)]
    return bytes(out)


async def main(out: Path, delete: bool):
    dev = await BleakScanner.find_device_by_filter(lambda d, a: U(0) in a.service_uuids or (a.local_name or "").startswith("Omi"), 20)
    if not dev:
        sys.exit("no device")
    async with BleakClient(dev) as c:
        now = int(time.time() * 1000)
        await c.write_gatt_char(TIME, struct.pack("<Q", now), response=True)
        boot, frame, store = struct.unpack("<HIh", await c.read_gatt_char(TIME))
        print(f"boot {boot}, frame {frame} = {dt.datetime.fromtimestamp(now / 1000)}, store {store}")
        if store:
            sys.exit(f"the device stores nothing (error {store})")
        replies, data = asyncio.Queue(), {}
        await c.start_notify(CTRL, lambda _, d: replies.put_nowait(bytes(d)))
        await c.start_notify(DATA, lambda _, d: data.__setitem__(struct.unpack_from("<I", d)[0], bytes(d[4:])))
        await c.write_gatt_char(CTRL, b"\x01", response=True)
        listing = []
        while (r := await asyncio.wait_for(replies.get(), 10))[0] != 0x80:
            listing.append(struct.unpack_from("<II", r, 1))
        print("files:", [(f"{f:08X}", s) for f, s in listing])
        files = {}
        for f, size in listing:
            data.clear()
            t = time.time()
            await c.write_gatt_char(CTRL, struct.pack("<BII", 2, f, 0), response=True)
            while (r := await asyncio.wait_for(replies.get(), 120))[0] != 0x82:
                pass
            end = struct.unpack_from("<I", r, 5)[0]
            b, off = bytearray(), 0
            for o in sorted(data):
                if o != off:
                    sys.exit(f"{f:08X}: hole at {off} (next chunk at {o})")
                b += data[o]
                off += len(data[o])
            ok = end == size == len(b)
            print(f"{f:08X}: {len(b)} B of {size} in {time.time() - t:.1f} s, {len(b) / 1024 / (time.time() - t):.0f} KB/s{'' if ok else '  INCOMPLETE'}")
            if ok:
                files[f] = bytes(b)
        for i, (start, frames) in enumerate(runs(files, (boot, frame, now))):
            when = dt.datetime.fromtimestamp(start / 1000).strftime("%H%M%S.%f")[:-3] if start else "unknown"
            p = out / f"{i:03d}-{when}.wav"
            with wave.open(str(p), "wb") as w:
                w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000), w.writeframes(decode(frames))
            print(f"run {i}: {len(frames) * FRAME_MS / 1000:.2f} s from {when} -> {p.name}")
        if delete:
            for f in files:
                await c.write_gatt_char(CTRL, struct.pack("<BI", 4, f), response=True)
                r = await asyncio.wait_for(replies.get(), 10)
                print(f"delete {f:08X}: {struct.unpack_from('<b', r, 5)[0]}")


def selftest():
    a = lambda first, n: bytes([0x41, n]) + struct.pack("<I", first) + b"".join(bytes([2, i, i]) for i in range(n))
    t = lambda frame, epoch: b"T" + struct.pack("<IQ", frame, epoch)
    boot1 = {1 << 16: a(0, 3) + t(2, 10_000) + a(3, 2) + a(9, 1), 1 << 16 | 1: a(10, 1)[:-1]}  # gap at 5..8, cut tail
    boot2 = {2 << 16: a(0, 2)}  # rebooted, never synced, not the live boot: time unknown
    got = list(runs({**boot1, **boot2}, (3, 50, 99_000)))
    assert [(s, len(f)) for s, f in got] == [(9_980, 5), (10_070, 1), (None, 2)], got
    print("selftest ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["--selftest"]:
        sys.exit(selftest())
    d = Path(sys.argv[1])
    d.mkdir(parents=True, exist_ok=True)
    asyncio.run(main(d, "--delete" in sys.argv))
