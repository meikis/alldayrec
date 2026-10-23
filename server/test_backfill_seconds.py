"""backfill_seconds on AAC rows stored with their padded length, next to an Ogg row it must leave alone."""
import datetime as dt
import subprocess
import tempfile
from pathlib import Path

import audio
import db
import backfill_seconds


def test_backfill():
    rec = Path(tempfile.mkdtemp())
    db.open_db(rec / "t.db")
    enc = lambda c, f: subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=f=440:d=5", "-c:a", c, "-f", f, "pipe:1"],
                                      capture_output=True, check=True).stdout
    T = lambda m: int(dt.datetime(2026, 10, 2, 10, m).timestamp() * 1000)
    for m in (0, 1):
        assert audio.save("iphone", T(m), enc("aac", "adts"), "audio/aac")
    assert audio.save("phone", T(0), enc("libopus", "ogg"), "audio/ogg")
    right = {r["start_ms"]: r["seconds"] for r in db.conn().execute("SELECT * FROM pieces WHERE device = 'iphone'")}
    db.conn().execute("UPDATE pieces SET seconds = seconds + 0.18 WHERE device = 'iphone'")  # as stored before the AAC trim
    db.conn().execute("UPDATE pieces SET seconds = 99 WHERE device = 'phone'")
    db.conn().execute("VACUUM INTO ?", (str(rec / "copy.db"),))  # the script runs on a copy, as on a server
    backfill_seconds.backfill(rec / "copy.db", rec)
    assert {r["start_ms"]: r["seconds"] for r in db.conn().execute("SELECT * FROM pieces WHERE device = 'iphone'")} == right
    assert db.conn().execute("SELECT seconds FROM pieces WHERE device = 'phone'").fetchone()[0] == 99, "not AAC: left alone"
    backfill_seconds.backfill(rec / "copy.db", rec)  # again: nothing changes


if __name__ == "__main__":
    test_backfill()
    print("ok")
