"""Group a time-ordered sentence stream into conversations.

A conversation closes when no sentence starts within GAP_S of its end, or when it has run MAX_S.
Conversations with less than MIN_S of speech are dropped. Sentences whose speaker is in IGNORE
(far-field, undecided) neither open nor extend a conversation but are kept inside one that is open.
"""
import datetime as dt

GAP_S, MIN_S, MAX_S = 120, 10, 4 * 3600
IGNORE = {"远处", "far", "不确定", "未知", "unknown", "S?"}  # S?: too short for a voiceprint

P = dt.datetime.fromisoformat


class Grouper:
    """Feed sentences in time order; `push` returns the conversations closed by that sentence,
    `flush(now)` those closed by the clock."""

    def __init__(self, gap_s=GAP_S, min_s=MIN_S, max_s=MAX_S):
        self.gap, self.min, self.max = gap_s, min_s, max_s
        self.open = None

    def push(self, s: dict) -> list[dict]:
        a, b = P(s["start"]), P(s["end"])
        out = self.flush(a)
        if s.get("speaker") in IGNORE:
            if self.open:
                self.open["sentences"].append(s)
            return out
        if self.open is None:
            self.open = dict(start=s["start"], end=s["end"], sentences=[], speech_s=0.0)
        self.open["sentences"].append(s)
        self.open["end"] = max(self.open["end"], s["end"])
        self.open["speech_s"] += (b - a).total_seconds()
        return out

    def flush(self, now: dt.datetime) -> list[dict]:
        c = self.open
        if c and ((now - P(c["end"])).total_seconds() >= self.gap or (now - P(c["start"])).total_seconds() >= self.max):
            self.open = None
            if c["speech_s"] >= self.min:
                c["participants"] = sorted({x.get("speaker") for x in c["sentences"] if x.get("speaker") and x["speaker"] not in IGNORE}, key=str)
                c["state"] = "closed"
                return [c]
        return []


def split(sentences: list[dict], **kw) -> list[dict]:
    g, out = Grouper(**kw), []
    for s in sorted(sentences, key=lambda s: s["start"]):
        out += g.push(s)
    if g.open:
        out += g.flush(P(g.open["end"]) + dt.timedelta(seconds=g.gap))
    return out


if __name__ == "__main__":
    T = lambda m, s=0: (dt.datetime(2026, 9, 29, 10) + dt.timedelta(minutes=m, seconds=s)).isoformat()
    rows = [dict(start=T(0), end=T(0, 20), speaker="我"), dict(start=T(1), end=T(1, 20), speaker="S1"),
            dict(start=T(2, 30), end=T(2, 40), speaker="远处"),      # far-field inside: kept, does not extend
            dict(start=T(5), end=T(5, 5), speaker="我"),             # 3 min 40 s after the last real sentence: new one
            dict(start=T(5, 7), end=T(5, 9), speaker="我"),          # only 7 s of speech: dropped
            dict(start=T(20), end=T(20, 30), speaker="S2")]
    cs = split(rows)
    assert [len(c["sentences"]) for c in cs] == [3, 1], cs
    assert cs[0]["participants"] == ["S1", "我"] and cs[0]["end"] == T(1, 20)
    print("ok", len(cs), "conversations")
