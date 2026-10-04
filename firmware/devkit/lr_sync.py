"""Reference client for the live-record offline store (src/lr_store.h): sync the clock, take the stored files,
put every frame back on the wall clock, write one WAV per continuous run; with --server, send the dated runs as Ogg
pieces to /api/chunks under the recorder's name, as the phone app does. --delete removes what was kept (sent, or
written here when undated) from the card. `log` prints the recorder's event log (src/lr_log.h) as a timeline.
    uv run --no-project --with bleak python lr_sync.py OUT_DIR [--delete] [--name LR-X1-567E] [--server URL --token T]
    uv run --no-project --with bleak python lr_sync.py log [--name LR-X1-567E] [--from SEQ]
"""
import asyncio
import ctypes
import errno
import random
import urllib.request
import datetime as dt
import struct
import sys
import time
import wave
from pathlib import Path

from bleak import BleakClient, BleakScanner

U = lambda n: f"7c3e{n:04x}-1a2b-4c5d-8e9f-6c6976657265"
TIME, CTRL, DATA = U(1), U(2), U(3)
AUDIO_SVC = "19b10000-e8f2-537e-4f6c-d104768a1214"  # advertised; the store service is not
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


_CRC = []
for _i in range(256):
    _r = _i << 24
    for _ in range(8):
        _r = ((_r << 1) ^ 0x04C11DB7 if _r & 0x80000000 else _r << 1) & 0xFFFFFFFF
    _CRC.append(_r)


def ogg_opus(frames, frame_ms=FRAME_MS, rate=16000) -> bytes:
    """Opus packets into one Ogg Opus file (RFC 7845), as web/src/lib/ogg.ts and the app's muxer make them."""
    serial = random.getrandbits(32)

    def page(packets, flags, granule, seq):
        lacing = []
        for p in packets:
            n = len(p)
            while True:
                lacing.append(min(n, 255))
                if n < 255:
                    break
                n -= 255
        out = bytearray(b"OggS\0" + bytes([flags]) + struct.pack("<qIII", granule, serial, seq, 0) + bytes([len(lacing)] + lacing))
        out += b"".join(packets)
        crc = 0
        for b in out:
            crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC[((crc >> 24) ^ b) & 0xFF]
        struct.pack_into("<I", out, 22, crc)
        return bytes(out)

    head = b"OpusHead" + bytes([1, 1]) + struct.pack("<HIhB", 0, rate, 0, 0)
    pages = [page([head], 2, 0, 0), page([b"OpusTags" + bytes(8)], 0, 0, 1)]
    for i in range(0, len(frames), 50):
        last = i + 50 >= len(frames)
        pages.append(page(frames[i:i + 50], 4 if last else 0, min(i + 50, len(frames)) * frame_ms * 48, len(pages)))
    return b"".join(pages)


PIECE_S = 60  # an offline run goes up in pieces of this length


def upload(server: str, token: str, device: str, start: int, frames) -> None:
    """One run, cut into pieces; raises if the server does not take one."""
    n = PIECE_S * 1000 // FRAME_MS
    for i in range(0, len(frames), n):
        req = urllib.request.Request(f"{server}/api/chunks?device={urllib.request.quote(device)}&start={start + i * FRAME_MS}",
                                     ogg_opus(frames[i:i + n]), {"Content-Type": "audio/ogg", "Authorization": f"Bearer {token}"})
        urllib.request.urlopen(req, timeout=60).read()


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


async def main(out: Path, delete: bool, name: str, server: str | None, token: str):
    dev = await BleakScanner.find_device_by_filter(
        lambda d, a: AUDIO_SVC in a.service_uuids and (a.local_name or "").startswith(name), 20)
    if not dev:
        sys.exit("no device")
    async with BleakClient(dev) as c:
        now = int(time.time() * 1000)
        await c.write_gatt_char(TIME, struct.pack("<Q", now), response=True)
        boot, frame, store, free_kb, total_kb = struct.unpack_from("<HIhII", await c.read_gatt_char(TIME))
        print(f"boot {boot}, frame {frame} = {dt.datetime.fromtimestamp(now / 1000)}, store {store}, "
              f"card {free_kb / 1024:.0f} of {total_kb / 1024:.0f} MB free")
        if store:
            sys.exit(f"the device stores nothing (error {store})")
        replies, data = asyncio.Queue(), {}
        await c.start_notify(CTRL, lambda _, d: replies.put_nowait(bytes(d)))
        last = [time.time()]  # the last DATA notify: a file takes as long as it takes while bytes keep coming

        def on_data(_, d):
            data[struct.unpack_from("<I", d)[0]] = bytes(d[4:])
            last[0] = time.time()

        await c.start_notify(DATA, on_data)
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
            while True:
                try:
                    if (r := await asyncio.wait_for(replies.get(), 5))[0] == 0x82:
                        break
                except TimeoutError:
                    if time.time() - last[0] > 15:
                        sys.exit(f"{f:08X}: stalled")
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
                (out / f"{f:08X}.BIN").write_bytes(b)  # as on the card, for a second look
        for i, (start, frames) in enumerate(runs(files, (boot, frame, now))):
            when = dt.datetime.fromtimestamp(start / 1000).strftime("%m%d-%H%M%S.%f")[:-3] if start else "undated"
            p = out / f"{i:03d}-{when}.wav"
            with wave.open(str(p), "wb") as w:
                w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000), w.writeframes(decode(frames))
            sent = ""
            if server and start:
                upload(server, token, dev.name, start, frames)  # a failure stops here: nothing is deleted
                sent = f", sent as {dev.name}"
            print(f"run {i}: {len(frames) * FRAME_MS / 1000:.2f} s from {when} -> {p.name}{sent}")
        if delete:
            for f in files:
                await c.write_gatt_char(CTRL, struct.pack("<BI", 4, f), response=True)
                r = await asyncio.wait_for(replies.get(), 10)
                print(f"delete {f:08X}: {struct.unpack_from('<b', r, 5)[0]}")


#
# The event log (src/lr_log.h): lr_sync.py log [--name NAME] [--from SEQ]
#
REC = struct.Struct("<HIBBII")  # boot t code x a b
UPTIME = 0x80
RESETREAS = {0: "RESETPIN", 1: "DOG", 2: "SREQ", 3: "LOCKUP", 16: "OFF", 17: "LPCOMP", 18: "DIF", 19: "NFC", 20: "VBUS"}
HCI = {0x05: "auth failure", 0x06: "PIN or key missing", 0x08: "supervision timeout", 0x13: "remote user terminated",
       0x14: "remote low resources", 0x15: "remote power off", 0x16: "local host terminated",
       0x1A: "unsupported remote feature", 0x1F: "unspecified", 0x22: "LL response timeout", 0x28: "instant passed",
       0x2A: "transaction collision", 0x3B: "unacceptable parameters", 0x3D: "MIC failure",
       0x3E: "failed to establish"}
SECURITY = ["success", "auth fail", "PIN or key missing", "OOB not available", "auth requirement",
            "pair not supported", "pair not allowed", "invalid param", "key rejected", "unspecified"]
FAULT = ["CPU exception", "spurious IRQ", "stack check", "kernel oops", "kernel panic"]
ERRNO = {**{n: errno.errorcode[n] for n in range(1, 35) if n in errno.errorcode},  # the same everywhere up to 34
         77: "EBADMSG", 105: "ENOBUFS", 116: "ETIMEDOUT", 120: "EALREADY", 128: "ENOTCONN", 134: "ENOTSUP"}  # Zephyr's


def err(v):
    v = struct.unpack("<i", struct.pack("<I", v))[0]
    return f"{v} {'-' + ERRNO[-v] if -v in ERRNO else ''}".strip() if v else "ok"


def reas(v):
    return "+".join(n for b, n in RESETREAS.items() if v >> b & 1) or "power-on"


def param(a, b):
    return f"interval {(a & 0xFFFF) * 1.25:g} ms, latency {a >> 16}, timeout {b * 10} ms"


def stack(v):
    return "-" if v == 0xFFFF else str(v)


EVENTS = {
    1: ("BOOT", lambda x, a, b: f"reset {reas(a)} (0x{a:x}), battery {x * 20} mV, fw {b & 0xFFFFFFF:07x}{'+' * (b >> 28 & 1)}"),
    2: ("FAULT", lambda x, a, b: f"{FAULT[a] if a < len(FAULT) else a}, PC 0x{b:08x} (previous boot)"),
    3: ("MOUNT", lambda x, a, b: f"try {x}: {err(a)}"),
    4: ("STORE", lambda x, a, b: f"audio boot {a} (files {a:04X}xxxx), {b // 1024} MB free"),
    5: ("SLOW", lambda x, a, b: f"SD append {a} ms ({b} B)"),
    6: ("APPEND_ERR", lambda x, a, b: f"{err(a)} ({b} B)"),
    7: ("CARD_FULL", lambda x, a, b: f"{'recording stopped' if x else 'resumed'}, {a} KB free"),
    8: ("USB_DISK", lambda x, a, b: f"card lent to USB" if x else f"card back: {err(a)}"),
    9: ("CONNECT", lambda x, a, b: param(a, b)),
    10: ("DISCONNECT", lambda x, a, b: f"0x{x:02x} {HCI.get(x, '')}".strip()),
    11: ("PARAM", lambda x, a, b: param(a, b)),
    12: ("MTU", lambda x, a, b: f"{'link data length' if x else 'ATT MTU'} tx {a} rx {b}"),
    13: ("PHY", lambda x, a, b: f"tx {a} rx {b} (1: 1M, 2: 2M, 4: coded)"),
    14: ("TIME", lambda x, a, b: dt.datetime.fromtimestamp((b << 32 | a) / 1000).isoformat(" ", "milliseconds")),
    15: ("SECURITY", lambda x, a, b: f"level {x}" + (f", {SECURITY[a] if a < len(SECURITY) else a}" if a else "")),
    16: ("PAIRED", lambda x, a, b: "bonded" if x else "not bonded"),
    17: ("PAIR_FAIL", lambda x, a, b: SECURITY[x] if x < len(SECURITY) else str(x)),
    18: ("FORGET", lambda x, a, b: f"{a} bond(s): {'stale bond' if x else 'a phone asked'}"),
    19: ("DROPPED", lambda x, a, b: f"{a} frames, {b} this boot"),
    20: ("SEGMENT", lambda x, a, b: f"{a:08X}.BIN"),
    21: ("DFU", lambda x, a, b: {0x57: "into the UF2 bootloader", 0xA8: "into the OTA bootloader"}.get(x, f"0x{x:02x}")),
    22: ("HEARTBEAT", lambda x, a, b: f"battery {x * 20} mV, {a // 1024} MB free, {b} frames dropped"),
    23: ("STACKS", lambda x, a, b: f"unused: codec {stack(a >> 16)}, pusher {stack(a & 0xFFFF)}, "
                                   f"store {stack(b >> 16)}, BT RX {stack(b & 0xFFFF)} B"),
    24: ("SD_RESET", lambda x, a, b: f"CMD12 r1 0x{a:02x}, CMD0 r1 0x{b:02x}" + (" (idle)" if b == 1 else " (no idle: card not answering)")),
}


def log_records(packets):
    """DATA notifications of a LOG (seq:u32 + 16 B records) -> {seq: (boot, t, code, x, a, b)}."""
    out = {}
    for p in packets:
        seq = struct.unpack_from("<I", p)[0]
        for i in range((len(p) - 4) // REC.size):
            out[seq + i] = REC.unpack_from(p, 4 + i * REC.size)
    return out


def timeline(recs):
    """(seq, wall time or a boot-relative uptime, boot, name, args) in seq order: a frame goes on the wall clock by its
    boot's TIME nearest to it; a boot whose TIMEs fell out of the log (the oldest sectors go first) stays on frames."""
    sync = {}
    for boot, t, code, x, a, b in recs.values():
        if code == 14:
            sync.setdefault(boot, []).append((t, b << 32 | a))
    for seq in sorted(recs):
        boot, t, code, x, a, b = recs[seq]
        name, fmt = EVENTS.get(code & ~UPTIME, (f"0x{code & ~UPTIME:02x}", lambda x, a, b: f"x {x} a {a} b {b}"))
        if code & UPTIME:
            when = f"(uptime {t / 1000:.3f} s)"
        elif (e := cur_time(sync.get(boot), t)) is not None:
            when = dt.datetime.fromtimestamp(e / 1000).isoformat(" ", "milliseconds")
        else:
            when = f"(frame {t})"
        yield seq, when, boot, name, fmt(x, a, b)


async def log(name: str, start: int):
    dev = await BleakScanner.find_device_by_filter(
        lambda d, a: AUDIO_SVC in a.service_uuids and (a.local_name or "").startswith(name), 20)
    if not dev:
        sys.exit("no device")
    async with BleakClient(dev) as c:
        # this boot's frames on the wall clock: the TIME write is an event too
        await c.write_gatt_char(TIME, struct.pack("<Q", int(time.time() * 1000)), response=True)
        replies, packets = asyncio.Queue(), []
        await c.start_notify(CTRL, lambda _, d: replies.put_nowait(bytes(d)))
        await c.start_notify(DATA, lambda _, d: packets.append(bytes(d)))
        await c.write_gatt_char(CTRL, struct.pack("<BI", 7, start), response=True)
        while (r := await asyncio.wait_for(replies.get(), 30))[0] != 0x87:
            pass
        nxt = struct.unpack_from("<I", r, 1)[0]
    print(f"{dev.name}:")
    for seq, when, boot, ev, args in timeline(log_records(packets)):
        print(f"{seq:6d}  {when:26s} boot {boot:<4d} {ev:11s} {args}")
    print(f"next: lr_sync.py log --from {nxt}")


def selftest():
    a = lambda first, n: bytes([0x41, n]) + struct.pack("<I", first) + b"".join(bytes([2, i, i]) for i in range(n))
    t = lambda frame, epoch: b"T" + struct.pack("<IQ", frame, epoch)
    boot1 = {1 << 16: a(0, 3) + t(2, 10_000) + a(3, 2) + a(9, 1), 1 << 16 | 1: a(10, 1)[:-1]}  # gap at 5..8, cut tail
    boot2 = {2 << 16: a(0, 2)}  # rebooted, never synced, not the live boot: time unknown
    got = list(runs({**boot1, **boot2}, (3, 50, 99_000)))
    assert [(s, len(f)) for s, f in got] == [(9_980, 5), (10_070, 1), (None, 2)], got
    ogg = ogg_opus([bytes([0xF8, 0xFF, 0xFE])] * 120)  # 120 silent 10 ms Opus frames
    assert ogg[:4] == b"OggS" and ogg.count(b"OggS") == 5, ogg.count(b"OggS")

    # the event log, wrapped: the oldest sectors went, so boot 7 starts mid-way (its BOOT and TIME gone); LOG packets
    # as the device sends them (seq + records), one sent twice by an overlapping read, out of order
    r = lambda boot, t, code, x=0, a=0, b=0: REC.pack(boot, t, code, x, a, b)
    pkt = lambda seq, *recs: struct.pack("<I", seq) + b"".join(recs)
    when = lambda e: dt.datetime.fromtimestamp(e / 1000).isoformat(" ", "milliseconds")
    packets = [pkt(5003, r(8, 400, 9, 0, 24 | 0 << 16, 400), r(8, 1000, 14, 0, 1_700_000_000_000 & 0xFFFFFFFF, 1_700_000_000_000 >> 32)),
               pkt(5000, r(7, 123456, 22, 200, 2048, 7), r(8, 1500, 1 | UPTIME, 205, 0x4, 0x1abcdef | 1 << 28)),
               pkt(5002, r(8, 3200, 3 | UPTIME, 1, (-5) & 0xFFFFFFFF)),
               pkt(5003, r(8, 400, 9, 0, 24, 400)),
               pkt(5005, r(8, 2000, 10, 0x13), r(8, 2100, 0x63, 1, 2, 3))]
    got = [(seq, w, boot, n, a) for seq, w, boot, n, a in timeline(log_records(packets))]
    assert got == [
        (5000, "(frame 123456)", 7, "HEARTBEAT", "battery 4000 mV, 2 MB free, 7 frames dropped"),
        (5001, "(uptime 1.500 s)", 8, "BOOT", "reset SREQ (0x4), battery 4100 mV, fw 1abcdef+"),
        (5002, "(uptime 3.200 s)", 8, "MOUNT", "try 1: -5 -EIO"),
        (5003, when(1_700_000_000_000 - 6000), 8, "CONNECT", "interval 30 ms, latency 0, timeout 4000 ms"),
        (5004, when(1_700_000_000_000), 8, "TIME", when(1_700_000_000_000)),
        (5005, when(1_700_000_010_000), 8, "DISCONNECT", "0x13 remote user terminated"),
        (5006, when(1_700_000_011_000), 8, "0x63", "x 1 a 2 b 3")], got
    print("selftest ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["--selftest"]:
        sys.exit(selftest())
    arg = lambda k, default=None: sys.argv[sys.argv.index(k) + 1] if k in sys.argv else default
    if sys.argv[1] == "log":
        sys.exit(asyncio.run(log(arg("--name", "LR-"), int(arg("--from", 0)))))
    d = Path(sys.argv[1])
    d.mkdir(parents=True, exist_ok=True)
    asyncio.run(main(d, "--delete" in sys.argv, arg("--name", "LR-"), arg("--server"), arg("--token", "")))
