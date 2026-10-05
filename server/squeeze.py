"""Silence squeezed out of the audio the ASR engine gets (#87): the engine bills by audio seconds, and a final piece goes
whole when it has any speech. Each VAD silence longer than `keep` is cut down to `keep` (keep/2 after the speech before
it, keep/2 before the speech after it; at the window's edges keep/2 next to the speech). unsqueeze maps the engine's
times back to the window, so nothing after the engine sees the cut."""
import numpy as np

SR = 16000
MS = SR // 1000  # samples per ms: spans lie on whole ms, so whole-ms engine times map back exactly
FADE = 10 * MS   # ramps at each splice: no click


def squeeze(x: np.ndarray, speech: list[tuple[float, float]], keep: float) -> tuple[np.ndarray, list[tuple[float, float]]]:
    """x: int16 PCM; speech: (start, end) seconds into x. Returns the audio to send and the kept spans of x (ms) for
    unsqueeze. keep 0 or no speech in x: x whole."""
    n = len(x) / MS
    spans = []
    for a, b in sorted(speech):
        a, b = max(0, round(a * 1000 - keep * 500)), min(n, round(b * 1000 + keep * 500))
        if b <= a:
            continue
        if spans and a <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], b)
        else:
            spans.append([a, b])
    if keep <= 0 or not spans:
        return x, [(0, n)]
    parts = []
    for a, b in spans:
        p = x[a * MS:round(b * MS)].astype(np.float32)
        f = min(FADE, len(p) // 2)
        if a > 0:
            p[:f] *= np.linspace(0, 1, f)
        if b < n:
            p[len(p) - f:] *= np.linspace(1, 0, f)
        parts.append(p)
    return np.concatenate(parts).astype(np.int16), [tuple(s) for s in spans]


def at(ms: float, spans: list[tuple[float, float]], end: bool = False) -> float:
    """A time in the sent audio (ms) → ms into x. A sentence's end on a splice stays in the span before it, a start
    goes to the span after it."""
    off = 0
    for k, (a, b) in enumerate(spans):
        if ms < off + b - a or (end and ms == off + b - a) or k == len(spans) - 1:
            return ms - off + a
        off += b - a


def unsqueeze(segs: list[dict], spans: list[tuple[float, float]]) -> list[dict]:
    """The engine's sentences (and file-mode words) with times in x, in place; a sentence over a splice maps its start
    and end each on its own side."""
    for g in segs:
        for u in [g] + (g.get("words") or []):
            u["start_ms"], u["end_ms"] = at(u["start_ms"], spans), at(u["end_ms"], spans, end=True)
    return segs


def check():
    rng = np.random.default_rng(0)
    for _ in range(300):  # random speech: a sent sample (off the fades) is the sample of x its time maps back to
        x = (np.arange(int(rng.uniform(0.5, 300) * SR)) % 30011).astype(np.int16)
        n = len(x) / MS
        sp = sorted((a, a + rng.exponential(2)) for a in rng.uniform(-2, n / 1000 + 1, rng.integers(0, 12)))
        y, spans = squeeze(x, sp, 3.0)
        assert len(y) == sum(round(b * MS) - a * MS for a, b in spans)
        splices = np.cumsum([0] + [b - a for a, b in spans])
        for t in rng.integers(0, len(y) // MS, 100):
            if min(abs(t - s) for s in splices) * MS > FADE:
                assert y[t * MS] == x[int(at(t, spans)) * MS], (t, spans)
        pad = [(max(0, round(a * 1000 - 1500)), min(n, round(b * 1000 + 1500))) for a, b in sp]
        pad = [(a, b) for a, b in pad if b > a]  # speech within keep/2 of x
        for a, b in pad:  # all of it goes, with keep/2 around it
            assert any(lo <= a and b <= hi for lo, hi in spans), (a, b, spans)
        if pad:  # and nothing else: every span edge is a padded speech edge, cut silences are over keep
            assert all(lo in {a for a, _ in pad} and hi in {b for _, b in pad} for lo, hi in spans), (pad, spans)
            assert all(a2 > b1 for (_, b1), (a2, _) in zip(spans, spans[1:]))
    x = np.zeros(60 * SR, np.int16)
    y, spans = squeeze(x, [(10, 12), (30, 31.5)], 3.0)  # 0-8.5 cut, 13.5-28.5 cut, 33-60 cut
    assert spans == [(8500, 13500), (28500, 33000)] and len(y) == 9.5 * SR, spans
    assert at(0, spans) == 8500 and at(1500, spans) == 10000 and at(5000, spans) == 28500 and at(5000, spans, True) == 13500
    assert at(9500, spans, True) == 33000 and at(9999, spans) == 33499, "past the end: on from the last span"
    g = unsqueeze([{"start_ms": 1000, "end_ms": 6000, "words": [{"start_ms": 4900, "end_ms": 5000}, {"start_ms": 5000, "end_ms": 5100}]}], spans)[0]
    assert (g["start_ms"], g["end_ms"]) == (9500, 29500), "a sentence over the splice: start and end on their own sides"
    assert [(w["start_ms"], w["end_ms"]) for w in g["words"]] == [(13400, 13500), (28500, 28600)]
    for sp in ([], [(-5, -2), (62, 80)]):  # no speech within keep/2 of x: sent whole, times unchanged
        y, spans = squeeze(x, sp, 3.0)
        assert y is x and at(12345, spans) == 12345
    y, spans = squeeze(x, [(0.5, 59.9)], 3.0)  # speech throughout: whole
    assert y is not x and np.array_equal(y, x) and spans == [(0, 60000)] and at(777, spans, True) == 777
    y, spans = squeeze(x, [(10, 12)], 0)  # keep 0: off
    assert y is x and spans == [(0, 60000)]
    y, _ = squeeze(np.full(20 * SR, 1000, np.int16), [(3, 4), (12, 13)], 3.0)  # splices fade to 0 and back
    assert len(y) == 8 * SR and y[0] == y[4 * SR - 1] == y[4 * SR] == y[-1] == 0 and y[4 * SR + FADE] == y[2 * SR] == 1000


if __name__ == "__main__":
    check()
    print("ok")
