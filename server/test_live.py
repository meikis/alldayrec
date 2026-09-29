"""Segmenter must give the same utterances however the audio is chunked, on the recorder's clock."""
import datetime as dt
import os
import subprocess

import numpy as np

from server import SR, Registry, Segmenter, Speakers, clip_turns, final_rows

CLIP = os.environ.get("TEST_CLIP", "")  # any recorder file with speech; the test skips the audio part without it


def run(sizes):
    x = np.frombuffer(subprocess.run(["ffmpeg", "-v", "error", "-ss", "420", "-t", "60", "-i", CLIP, "-f", "s16le",
                                      "-ac", "1", "-ar", str(SR), "-"], capture_output=True, check=True).stdout, np.int16)
    seg, t0, i, out, k = Segmenter(), dt.datetime(2026, 9, 9, 11, 7), 0, [], 0
    while i < len(x):
        n = sizes[k % len(sizes)]; k += 1
        out += [(a, b, len(u)) for a, b, u in seg.push(t0 + dt.timedelta(seconds=i / SR), x[i:i + n])]
        i += n
    assert seg.frames == (len(x) - 400) // 160 + 1, (seg.frames, len(x))
    return out


def test_final_rows():
    import server
    from server import final_rows
    server.TURN_GAP_S = 1.0  # the merge case below assumes the default, whatever .env says
    t0 = dt.datetime(2026, 9, 28, 22, 0)
    ms = lambda s: int(s * 1000)
    segs = [dict(start_ms=ms(a), end_ms=ms(b), speaker=k, text=t) for a, b, k, t in [
        (1.0, 2.0, "A", "被边缘截断"), (10.0, 12.0, "A", "你定的吗？"), (12.1, 12.6, "B", "什么？"), (12.7, 14.0, "A", "就是额"),
        (14.2, 15.0, "A", "度。"), (50.0, 59.0, "B", "可能被窗口尾截断")]]
    lo, hi, rows = final_rows(segs, {"A": "他人", "B": "我"}, t0, 60.0)
    assert [(r["speaker"], r["text"]) for r in rows] == [("他人", "你定的吗？"), ("我", "什么？"), ("他人", "就是额度。")], rows
    assert lo == t0 + dt.timedelta(seconds=3) and hi == t0 + dt.timedelta(seconds=50), (lo, hi)


if __name__ == "__main__":
    test_final_rows()
    if not CLIP:
        print("ok: final_rows (set TEST_CLIP for the segmenter check)")
        raise SystemExit
    whole = run([SR])
    odd = run([977, 16001, 333, 8000, 12345])
    assert whole == odd, (whole[:3], odd[:3])
    a, b, n = whole[0]
    assert dt.datetime(2026, 9, 9, 11, 7) <= a < b and n == round((b - a).total_seconds() * SR), whole[0]
    print(f"ok: {len(whole)} utterances, first {a.time()}-{b.time()}")


def test_clip_turns():
    since = 100.0
    segs = [dict(start_ms=95_000, end_ms=101_000, speaker="A", text="finalized by the previous pass"),
            dict(start_ms=99_800, end_ms=104_000, speaker="B", text="the cut turn, re-cut 0.2 s early"),
            dict(start_ms=100_500, end_ms=103_000, speaker="A", text="new")]
    out = clip_turns(segs, since)
    assert [g["text"][:3] for g in out] == ["the", "new"], out
    assert out[0]["start_ms"] == 100_000


test_clip_turns()
print("ok: clip_turns")


def test_sample_at_gap():
    """A time inside the gap between two sessions maps to the first sample after the gap, not past it (09-29 17:55)."""
    s = object.__new__(Segmenter)
    T = lambda m, sec=0: dt.datetime(2026, 9, 29, 17, m, sec)
    s.anchors = [(0, T(45)), (100 * SR, T(52))]          # 100 s of audio from 17:45, then a new session at 17:52
    assert s.sample_at(T(45, 50)) == 50 * SR
    assert s.sample_at(T(50)) == 100 * SR               # in the gap: clamp, do not extrapolate to 300 s
    assert s.sample_at(T(52, 10)) == 110 * SR


def test_zero_context_keeps_first_sentence():
    """With no context the window starts 1 s before the sentence the last pass left open: it must be finalized."""
    segs = [dict(start_ms=1000, end_ms=4000, speaker="我", text="开头这句"), dict(start_ms=5000, end_ms=8000, speaker="我", text="第二句")]
    t0 = dt.datetime(2026, 9, 29, 18)
    lo, hi, rows = final_rows(segs, {"我": "我"}, t0, 20.0, since=1.0)
    assert [r["text"] for r in rows] == ["开头这句", "第二句"] and lo == t0 + dt.timedelta(seconds=1)


def test_sentence_speakers():
    """Per sentence: closer to the owner than to anyone known -> 我; a long unknown opens S1, a short one does not."""
    me, other, third = np.eye(4)[0], np.eye(4)[1], np.eye(4)[2]
    s = object.__new__(Speakers)
    s.owner, s.warned = {"phone": me}, set()
    by_start = {0: me, 2000: other, 6000: third, 8000: other * 0.9 + me * 0.3, 11000: me}
    s.embed = lambda x: by_start[next(k for k in by_start if abs(len_to_ms[len(x)] - k) < 1)]
    segs = [dict(start_ms=a, end_ms=b, speaker="SPEAKER_0", text="x") for a, b in
            [(0, 1600), (2000, 5000), (6000, 7500), (8000, 10500), (11000, 11500)]]
    len_to_ms = {int((min(b, a + 15000) - a) / 1000 * SR): a for a, b in [(g["start_ms"], g["end_ms"]) for g in segs]}
    reg = Registry()
    s.sentences(segs, np.zeros(SR * 20, np.int16), "phone", reg, 0.55)
    assert [g["speaker"] for g in segs] == ["我", "S1", "S?", "S1", "S?"], [g["speaker"] for g in segs]
