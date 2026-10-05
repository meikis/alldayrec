"""What the test server (app/ios/e2e/server.py serve ... OUT) got from the iPhone, as evidence for uitest.sh. Times are
epoch ms. Exit 0: the evidence is there.
    python3 evidence.py OUT wait-status DEVICE SINCE SECS   a device-status report of DEVICE after SINCE
    python3 evidence.py OUT wait-event KIND SINCE SECS      an app event KIND (connect, disconnect, sync, pause, resume,
                                                            kicked, unbind) after SINCE
    python3 evidence.py OUT wait-audio DEVICE FROM TO SECS  DEVICE's audio covering [FROM, TO] has arrived (its last
                                                            piece reaches TO), then: gaps over 1 s inside, piece lengths
    python3 evidence.py OUT audio DEVICE FROM TO            the same, now
    python3 evidence.py OUT none DEVICE FROM TO             no audio of DEVICE recorded in [FROM, TO] (unbound)"""
import json
import statistics
import sys
import time
from pathlib import Path


def recs(out):
    p = Path(out) / "server.jsonl"
    return [json.loads(l) for l in open(p)] if p.exists() else []


def pieces(out, device):
    return sorted(((int(r["start"]), int(r["start"]) + round((r["seconds"] or 0) * 1000)) for r in recs(out)
                   if r["path"] == "/api/chunks" and r["device"] == device), key=lambda p: p[0])


def audio(out, device, a, b):
    """Gaps (> 1 s) of DEVICE's audio inside [a, b], whether it reaches b, the median upload length (s)."""
    ps = [p for p in pieces(out, device) if p[1] > a and p[0] < b]
    gaps, end = [], a
    for s, e in ps:
        if s - end > 1000:
            gaps.append((end, s))
        end = max(end, e)
    if b - end > 1000:
        gaps.append((end, b))
    return gaps, bool(ps) and end >= b - 1000, statistics.median([(e - s) / 1000 for s, e in ps]) if ps else None


def hms(ms):
    return time.strftime("%H:%M:%S", time.localtime(ms / 1000))


def poll(secs, f):
    until = time.time() + secs
    while True:
        r = f()
        if r or time.time() > until:
            return r
        time.sleep(5)


def main(out, cmd, *a):
    if cmd == "wait-status":
        dev, since = a[0], int(a[1])
        r = poll(float(a[2]), lambda: [x for x in recs(out) if x["path"] == "/api/device-status" and x["device"] == dev and x["at"] > since])
        print(f"status of {dev}: {r[-1]['body'] if r else 'none'}")
        return 0 if r else 1
    if cmd == "wait-event":
        kind, since = a[0], int(a[1])
        def found():
            return [(x["device"] if "device" in x else "?", e) for x in recs(out) if x["path"] == "/api/device-log"
                    for e in x["body"] or [] if e.get("data", {}).get("kind") == kind and (e.get("at") or 0) > since]
        r = poll(float(a[2]), found)
        print(f"event {kind}: {r[-1] if r else 'none'}")
        return 0 if r else 1
    if cmd in ("wait-audio", "audio"):
        dev, fr, to = a[0], int(a[1]), int(a[2])
        if cmd == "wait-audio":
            poll(float(a[3]), lambda: audio(out, dev, fr, to)[1])
        gaps, reached, med = audio(out, dev, fr, to)
        print(f"{dev} {hms(fr)}-{hms(to)}: {'complete' if reached else 'not all there'}, median upload {med} s, "
              f"gaps: {', '.join(f'{hms(s)}-{hms(e)} ({(e - s) / 1000:.1f} s)' for s, e in gaps) or 'none'}")
        return 0 if reached and not gaps else 1
    if cmd == "none":
        dev, fr, to = a[0], int(a[1]), int(a[2])
        ps = [p for p in pieces(out, dev) if p[1] > fr + 1000 and p[0] < to - 1000]
        print(f"{dev} {hms(fr)}-{hms(to)}: {len(ps)} pieces recorded in the window")
        return 0 if not ps else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
