"""backfill_health on pieces stored before 10-07 (no rms, hum, zero_ms, full): measured once, measured ones left alone."""
import datetime as dt
import subprocess
import tempfile
from pathlib import Path

import audio
import backfill_health
import db


def test_backfill():
    rec = Path(tempfile.mkdtemp())
    db.open_db(rec / "t.db")
    audio.DIR = rec
    tone = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=f=440:d=5", "-c:a", "libopus", "-f", "ogg", "pipe:1"],
                          capture_output=True, check=True).stdout
    T = lambda m: int(dt.datetime(2026, 10, 2, 10, m).timestamp() * 1000)
    for m in (0, 1):
        assert audio.save("phone", T(m), tone, "audio/ogg")
    measured = dict(db.conn().execute("SELECT rms, hum, zero_ms, full FROM pieces WHERE start_ms = ?", (T(0),)).fetchone())
    db.conn().execute("UPDATE pieces SET rms = NULL, hum = NULL, zero_ms = NULL, full = NULL, received_ms = NULL WHERE start_ms = ?", (T(0),))
    db.conn().execute("UPDATE pieces SET rms = -99 WHERE start_ms = ?", (T(1),))  # measured already: not again
    backfill_health.backfill(rec / "t.db", rec)
    got = {r["start_ms"]: dict(r) for r in db.conn().execute("SELECT * FROM pieces")}
    assert {k: got[T(0)][k] for k in measured} == measured and got[T(0)]["received_ms"] is None
    assert got[T(1)]["rms"] == -99


if __name__ == "__main__":
    test_backfill()
    print("ok")
