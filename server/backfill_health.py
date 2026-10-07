"""One-off (10-07, #155): pieces stored before audio.save measured how they sound get rms, hum, zero_ms and full from
audio.health() of what audio.samples() returns for them, newest first; received_ms stays NULL (unknown). Only those
four columns of rows that lack them are written, so it is safe to run again and beside the running server (nice it).
    python backfill_health.py LIVE_DATA/live.db RECORDINGS_DIR
"""
import sys
from pathlib import Path

import audio
import db


def backfill(db_path: Path, rec: Path):
    db.open_db(db_path)
    audio.DIR = rec
    rows = db.conn().execute("SELECT * FROM pieces WHERE rms IS NULL ORDER BY start_ms DESC").fetchall()
    unreadable = 0
    for i, r in enumerate(rows):
        x = audio.samples(r)
        if x is None:
            unreadable += 1
            continue
        db.conn().execute("UPDATE pieces SET rms = ?, hum = ?, zero_ms = ?, full = ? WHERE device = ? AND start_ms = ? AND rms IS NULL",
                          (*audio.health(x), r["device"], r["start_ms"]))
        if i % 1000 == 999:
            print(f"{i + 1} of {len(rows)}", flush=True)
    print(f"{len(rows)} pieces measured, {unreadable} unreadable (left alone)")


if __name__ == "__main__":
    backfill(Path(sys.argv[1]), Path(sys.argv[2]))
