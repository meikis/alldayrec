"""Name hints (hints.py) for closed conversations that have none: the history from before hints were asked at each
close. Writes only the name_hints table: names, voices and summaries stay as they are. Newest first; safe to stop and
run again (a conversation with hints is skipped).

    cd server; set -a; . ../.env; set +a; uv run python backfill_hints.py [--days 30 | --all] [--workers 4] [--limit N]
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import os
import time
from pathlib import Path

import db
import hints
import summary


def todo(since: str | None) -> list[dict]:
    """Conversations a close would have asked (a summary call, someone besides me), newest first."""
    have = set(db.name_hints())
    out = []
    for c in db.conversations():
        if since and c["start"] < since:
            break
        s = c["summary"] or {}
        if c["id"] in have or s.get("status") != "done" or not s.get("model"):
            continue
        if set(c.get("participants") or []) - {"我", "S?"}:
            out.append(c)
    return out


def one(c: dict) -> dict:
    rows = db.sentences(c["start"], c["end"], inclusive=True, source=c.get("device"))
    media = summary.media_labels(rows, c["summary"].get("media_spans") or [])
    if not set(c["participants"]) - {"我", "S?"} - media:
        h = {"conv": c["id"], "prompt_version": hints.PROMPT_VERSION, "at": dt.datetime.now().isoformat(timespec="seconds"),
             "model": None, "asked": 0, "names": []}  # only media voices: nothing to ask, not asked again
    else:
        h = hints.hints(c, rows, media)
    db.put_name_hints(h)
    return h


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--all", action="store_true", help="every conversation, not only the last --days")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0, help="at most this many (a smoke test)")
    args = ap.parse_args()
    db.open_db(Path(os.environ["LIVE_DATA"]) / "live.db")
    since = None if args.all else (dt.date.today() - dt.timedelta(days=args.days)).isoformat()
    cs = todo(since)[:args.limit or None]
    print(f"{len(cs)} conversations to ask" + (f" since {since}" if since else ""), flush=True)
    t0, done, named, failed, tokens = time.time(), 0, 0, 0, [0, 0]
    with cf.ThreadPoolExecutor(args.workers) as ex:
        futs = {ex.submit(one, c): c for c in cs}
        for f in cf.as_completed(futs):
            c = futs[f]
            try:
                h = f.result()
            except Exception as e:  # left without hints: the next run asks it again
                failed += 1
                print(f"{c['id']} failed: {e!r}"[:200], flush=True)
                continue
            done += 1
            named += bool(h["names"])
            u = h.get("usage") or {}
            tokens[0] += u.get("prompt_tokens", 0)
            tokens[1] += u.get("completion_tokens", 0)
            if done % 20 == 0 or done == len(cs):
                print(f"{done}/{len(cs)} done, {named} with names, {failed} failed, {time.time() - t0:.0f} s, "
                      f"tokens in {tokens[0]} out {tokens[1]}", flush=True)
    print(f"finished: {done} asked, {named} with names, {failed} failed", flush=True)


if __name__ == "__main__":
    main()
