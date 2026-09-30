"""Conversation-level speaker labels. Every new sentence re-clusters the open conversation's voiceprints
(ReDimNet2, average-link cosine) and takes its cluster's label: 我 when that cluster's mean voiceprint is
close to the owner bank (>= OWNER_MIN; a split owner stays 我 in every piece), else a person's name when that
close to a voiceprint named by hand on the conversation page (>= PEOPLE_MIN), else the S<n> most of the cluster's earlier sentences
carry, so ids follow membership as clusters grow and merge. Only the new sentence is labelled; earlier
rows are never rewritten. Case replays 09-29: see artifacts/record/voiceprint.

    uv run python diarist.py   # self-check on synthetic voiceprints
"""
import os

import numpy as np

TH = float(os.environ.get("DIAR_TH", "0.5"))  # cosine distance; lower splits one person into several ids (mergeable later), higher merges people (not undoable): 0.6 merged two colleagues in the 05-26 meeting
OWNER_MIN = 0.7   # ReDimNet2 cluster vs owner bank: my clusters 0.79-0.99, others <= 0.63 (incl. unseen channels)
MIN_DUR = 1.0     # shorter sentences are not clustered: they join the nearest labelled centroid
JOIN = float(os.environ.get("DIAR_JOIN", "0.35"))  # ... when at least this close, else S?; also for a lone long sentence
MIN_ME = 3        # a cluster needs this many sentences before it can be 我 or a name
PEOPLE_MIN = float(os.environ.get("PEOPLE_MIN", "0.7"))  # same evidence as OWNER_MIN: other people score <= 0.63 against one person's voiceprint
KEEP = 1500       # clustering is O(n^2): a four-hour conversation keeps its latest sentences
SR = 16000


def load_embedder():
    """ReDimNet2 B6, VoxBlink2 + VoxCeleb2 + CN-Celeb (Interspeech 2026); int16 audio -> unit vector."""
    import torch
    m = torch.hub.load("PalabraAI/redimnet2", "redimnet2", model_name="b6", train_type="lm",
                       dataset="vb2+vox2+cnc2_v0", pretrained=True, trust_repo=True).eval()

    def embed(x: np.ndarray) -> np.ndarray:
        with torch.no_grad():
            e = m(torch.from_numpy(x.astype(np.float32) / 32768)[None]).numpy()[0]
        return e / np.linalg.norm(e)
    return embed


def _unit(v):
    return v / np.linalg.norm(v)


class Diarist:
    def __init__(self, bank: dict[str, np.ndarray], th: float = TH, people: dict[str, np.ndarray] | None = None):
        self.bank, self.th = [_unit(v) for v in bank.values()], th
        self.people = {k: _unit(v) for k, v in (people or {}).items()}  # the server adds names as they are given
        self.reset()

    def reset(self):
        self.E, self.dur, self.ids, self.nid = [], [], [], 0

    def mark(self) -> tuple[int, int]:
        return len(self.E), self.nid

    def rollback(self, mark: tuple[int, int]):
        """Forget sentences added after mark (labelled in a pass but left open for the next one)."""
        n, self.nid = mark
        del self.E[n:], self.dur[n:], self.ids[n:]

    def add(self, e: np.ndarray, dur: float) -> tuple[str, float]:
        """Label one sentence (its unit voiceprint and seconds); returns (label, owner score of its cluster)."""
        from scipy.optimize import linear_sum_assignment
        from sklearn.cluster import AgglomerativeClustering
        self.E.append(e); self.dur.append(dur); self.ids.append(None)
        i = len(self.E) - 1
        lo = max(0, i + 1 - KEEP)
        big = [j for j in range(lo, i + 1) if self.dur[j] >= MIN_DUR]
        E = np.array(self.E)
        if i not in big:
            return self._nearest(i, big)
        lab = AgglomerativeClustering(n_clusters=None, distance_threshold=self.th, metric="cosine",
                                      linkage="average").fit_predict(E[big]) if len(big) > 1 else np.zeros(1, int)
        owner = {c: max(float(_unit(E[big][lab == c].mean(0)) @ o) for o in self.bank) for c in set(lab)}
        me = {c for c in owner if (lab == c).sum() >= MIN_ME and owner[c] >= OWNER_MIN}
        c = lab[big.index(i)]
        if c in me:
            self.ids[i] = "我"
            return "我", round(owner[c], 3)
        if self.people and (lab == c).sum() >= MIN_ME:
            m = _unit(E[big][lab == c].mean(0))
            score, name = max((float(m @ v), k) for k, v in self.people.items())
            if score >= PEOPLE_MIN:
                self.ids[i] = name
                return name, round(score, 3)
        clusters = sorted(set(lab) - me)
        known = sorted({v for v in self.ids if v not in (None, "S?", "我")})
        cmap = {}
        if known:
            M = np.array([[sum(1 for j, l in zip(big, lab) if l == cl and self.ids[j] == k) for k in known] for cl in clusters])
            r, cc = linear_sum_assignment(-M)
            cmap = {clusters[a]: known[b] for a, b in zip(r, cc) if M[a, b] > 0}
        members = [j for j, l in zip(big, lab) if l == c]
        if c in cmap:
            self.ids[i] = cmap[c]
        elif len(members) >= 2:  # a new person once two sentences agree; the earlier one adopts the id for later rounds
            self.nid += 1
            self.ids[i] = f"S{self.nid}"
            for j in members:
                if self.ids[j] in (None, "S?"):
                    self.ids[j] = self.ids[i]
        else:  # alone in its cluster: the nearest speaker if close enough
            return self._nearest(i, [j for j in big if j != i])
        return self.ids[i], round(owner[c], 3)

    def _nearest(self, i: int, pool: list[int]) -> tuple[str, float]:
        cents = {}
        for j in pool:
            if self.ids[j] not in (None, "S?"):
                cents.setdefault(self.ids[j], []).append(self.E[j])
        best = max(((k, float(self.E[i] @ _unit(np.mean(v, 0)))) for k, v in cents.items()), key=lambda t: t[1], default=("S?", 0.0))
        self.ids[i] = best[0] if best[1] >= JOIN else "S?"
        return self.ids[i], round(best[1], 3)


if __name__ == "__main__":
    rng = np.random.default_rng(0)
    me, a, b = (_unit(rng.normal(size=192)) for _ in range(3))
    noisy = lambda v: _unit(v + rng.normal(size=192) * 0.035)  # cos to own centre ~0.8, like real sentences
    d = Diarist({"dji-mac": noisy(me)})
    seq = [me, a, me, a, b, a, me, b, b, me, a] * 3
    out = [d.add(noisy(v), 2.0)[0] for v in seq]
    tail = out[11:]
    assert all(l == "我" for l, v in zip(tail, seq[11:]) if v is me), out
    assert len({l for l, v in zip(tail, seq[11:]) if v is a}) == 1 and len({l for l, v in zip(tail, seq[11:]) if v is b}) == 1, out
    m = d.mark(); d.add(noisy(a), 2.0); d.rollback(m)
    assert d.mark() == m
    assert d.add(noisy(a), 0.4)[0] == tail[1], "a short sentence joins its speaker"
    d = Diarist({"dji-mac": noisy(me)}, people={"李某": noisy(a)})
    named = [d.add(noisy(v), 2.0)[0] for v in seq]
    assert set(l for l, v in zip(named[11:], seq[11:]) if v is a) == {"李某"}, named
    assert "李某" not in {l for l, v in zip(named, seq) if v is b}, named
    print("ok", out, named)
