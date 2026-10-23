"""One-off (10-04): the iPhone's AAC pieces stored before the AAC trim still have the seconds of their padded decode
(0.18 s too long each), so their rows overlap the next piece. Recomputes `seconds` of every .aac row from what
audio.samples() now returns for it (read at its pos in its hour file). Back up live.db first; safe to run again.
    python backfill_seconds.py LIVE_DATA/live.db RECORDINGS_DIR
"""
import sys
from pathlib import Path

import audio
import db


def backfill(db_path: Path, rec: Path):
    db.open_db(db_path, rec)
    rows = db.conn().execute("SELECT * FROM pieces WHERE path LIKE '%.aac' ORDER BY device, start_ms").fetchall()
    before = after = 0.0
    changed = unreadable = 0
    for r in rows:
        x = audio.samples(r)
        if x is None:
            unreadable += 1
            continue
        s = round(len(x) / audio.SR, 3)
        before, after = before + r["seconds"], after + s
        if s != r["seconds"]:
            db.conn().execute("UPDATE pieces SET seconds = ? WHERE device = ? AND start_ms = ?", (s, r["device"], r["start_ms"]))
            changed += 1
    print(f"{len(rows)} AAC pieces, {changed} changed, {unreadable} unreadable (left alone); {before:.1f} s -> {after:.1f} s")


if __name__ == "__main__":
    backfill(Path(sys.argv[1]), Path(sys.argv[2]))
