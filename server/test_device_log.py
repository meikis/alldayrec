"""Device log and status history: a record sent again is kept once, a range brings the recorder's undated records
that sit between its dated ones, and every status report is kept with the newest one at hand."""
import asyncio
import datetime as dt
import tempfile
from pathlib import Path

import db
from server import DeviceEvent, device_log, device_log_upload, device_status_report

T = 1_700_000_000_000  # the phone's TIME write, epoch ms (lr_sync.py's selftest)
iso = lambda ms: dt.datetime.fromtimestamp(ms / 1000).isoformat(timespec="milliseconds")


def rec(seq, at, boot, t, code, x=0, a=0, b=0):
    return DeviceEvent(src="rec", seq=seq, at=at, data=dict(boot=boot, t=t, code=code, x=x, a=a, b=b))


# lr_sync.py's selftest log: boot 7's TIME fell out of the log (undated), boot 8's frames dated by its TIME at frame 1000
LOG = [rec(5000, None, 7, 123456, 22, 200, 2048, 7),
       rec(5001, T - 7000, 8, 1500, 1 | 0x80, 205, 0x4, 0x1abcdef | 1 << 28),  # uptime record: the phone put it at frame 0
       rec(5002, None, 8, 3200, 3 | 0x80, 1, (-5) & 0xFFFFFFFF),               # one it could not
       rec(5003, T - 6000, 8, 400, 9, 0, 24, 400),
       rec(5004, T, 8, 1000, 14, 0, T & 0xFFFFFFFF, T >> 32),
       rec(5005, T + 10_000, 8, 2000, 10, 0x13)]
APP = [DeviceEvent(src="app", seq=T + 1, at=T + 1, data={"kind": "connect", "ms": 3200}),
       DeviceEvent(src="app", seq=T + 9_000, at=T + 9_000, data={"kind": "upload_fail", "text": "HTTP 502"})]


def seqs(lo, hi=None):
    got = asyncio.run(device_log("LR-X1-567E", iso(lo), hi and iso(hi)))
    return sorted((e["src"], e["seq"]) for e in got["events"])


def test_device_log():
    db.open_db(Path(tempfile.mkdtemp()) / "t.db")
    asyncio.run(device_log_upload("LR-X1-567E", LOG[:4] + APP))
    asyncio.run(device_log_upload("LR-X1-567E", LOG[2:]))  # an overlapping read: 5002, 5003 again
    asyncio.run(device_log_upload("LR-P1-BDAE", [rec(5000, T, 1, 0, 22, 190)]))  # another recorder, same seq
    assert db.conn().execute("SELECT count(*) FROM device_log").fetchone()[0] == 9

    every = [("app", T + 1), ("app", T + 9_000)] + [("rec", s) for s in range(5000, 5006)]
    assert seqs(T - 60_000) == every
    assert seqs(T - 60_000, T + 60_000) == every
    assert seqs(T + 5_000) == [("app", T + 9_000), ("rec", 5005)], "dated before the range: no undated ones"
    assert seqs(T - 3_600_000, T - 6500) == [("rec", 5000), ("rec", 5001), ("rec", 5002)], \
        "the undated ones up to the first dated record after the range"
    e = next(e for e in asyncio.run(device_log("LR-X1-567E", iso(T)))["events"] if e["seq"] == 5004)
    assert (e["at"], e["data"]["a"], e["data"]["b"]) == (iso(T), T & 0xFFFFFFFF, T >> 32)

    asyncio.run(device_log_upload("LR-X1-567E", [rec(5005, T + 20_000, 9, 50, 22, 180)]))  # the log was erased: seq again
    assert next(e for e in asyncio.run(device_log("LR-X1-567E", iso(T)))["events"] if e["seq"] == 5005)["data"]["boot"] == 9


def test_device_status():
    db.open_db(Path(tempfile.mkdtemp()) / "t.db")
    assert db.device_status("LR-X1-567E") is None
    lo = (dt.datetime.now() - dt.timedelta(seconds=1)).isoformat()
    for battery in (80, 79):
        asyncio.run(device_status_report("LR-X1-567E", {"battery": battery, "store": 0, "model": "LR-X1"}))
    asyncio.run(device_status_report("phone", {"battery": 50}))
    hist = asyncio.run(device_log("LR-X1-567E", lo))["status"]
    assert [h["battery"] for h in hist] == [80, 79] and hist[0]["at"] <= hist[1]["at"]
    assert db.device_status("LR-X1-567E") == hist[-1]
