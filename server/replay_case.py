"""Replay a case's audio through candidate sentence pipelines; writes <case>/hyp/<name>.jsonl for score_case.py.

    set -a; . .env; set +a
    uv run python replay_case.py <case-dir> <candidate> [<candidate> ...]

Candidates
  vad            Stream-VAD utterances + StepFun SSE per utterance, no speaker (the live first layer)
  vad-owner      same, each utterance labelled 我 if its CAM++ score against the owner >= OWNER_MIN (per-sentence rule)
  volc-window    same windows through Volcengine seed-asr-2.0 (streaming API, speaker ids), cached in work/volc/
  cloud-window   every 30 s, last 240 s to StepFun file diarization (via the file-mode ASR service), cluster mean vs owner,
                 final_rows replace the window (the live second layer as designed)
  local-window   same cadence, diarization by sherpa-onnx pyannote-segmentation-3.0 + CAM++ clustering on the Mac,
                 then StepFun SSE per speaker turn
  local-offline  sherpa-onnx diarization on the whole file (upper bound for the local segmenter), SSE per turn
ASR calls and cloud diarizations are cached under <case>/work/.
"""
import concurrent.futures as cf
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from server import (SR, OWNER_MIN, MIN_SPEAKER_S, DEVICE_PARAMS, LINK_MIN, words_to_turns, clip_turns, REFINE_EVERY_S, REFINE_WINDOW_S, Asr, LocalDiarizer, Registry, Segmenter,
                    Speakers, apply_final, diarize, final_rows)  # noqa: E402

EPOCH = dt.datetime(2000, 1, 1)  # replay clock: offset seconds since EPOCH, mapped to wall time at the end


class Case:
    def __init__(self, d: Path):
        self.dir = d
        self.meta = json.load(open(d / "meta.json"))
        self.x = np.frombuffer(subprocess.run(["ffmpeg", "-v", "error", "-i", str(d / self.meta["audio"]), "-f", "s16le", "-ac", "1",
                                              "-ar", str(SR), "-"], capture_output=True, check=True).stdout, np.int16)
        self.work = d / "work"
        self.work.mkdir(exist_ok=True)
        self.asr = Asr(os.environ["STEPFUN_API_KEY"])
        self.cache_path = self.work / "asr_cache.jsonl"
        self.cache = {}
        if self.cache_path.exists():
            for l in open(self.cache_path):
                r = json.loads(l)
                self.cache[r["k"]] = r["text"]

    def wall(self, off: float) -> dt.datetime:
        p = max((p for p in self.meta["pieces"] if p["offset_s"] <= off), key=lambda p: p["offset_s"])
        return dt.datetime.fromisoformat(p["wall"]) + dt.timedelta(seconds=off - p["offset_s"])

    def transcribe_many(self, spans: list[tuple[float, float]]) -> list[str]:
        """StepFun SSE text for each (a, b) offset span, cached, 6 in flight."""
        keys = [f"{a:.2f}-{b:.2f}" for a, b in spans]
        todo = [(k, a, b) for k, (a, b) in zip(keys, spans) if k not in self.cache and b - a >= 0.3]

        def one(item):
            k, a, b = item
            for attempt in range(8):
                try:
                    return k, self.asr._post(self.x[int(a * SR):int(b * SR)])
                except Exception as e:
                    time.sleep(2 ** attempt)  # 429 from StepFun when two replays run at once
            raise RuntimeError(f"ASR gave up on {k}: {e!r}")

        t = time.time()
        with cf.ThreadPoolExecutor(2) as ex, open(self.cache_path, "a") as f:  # the live service shares the key
            for i, (k, text) in enumerate(ex.map(one, todo)):
                self.cache[k] = text
                f.write(json.dumps({"k": k, "text": text}, ensure_ascii=False) + "\n")
                if i % 200 == 0 and todo:
                    print(f"  asr {i}/{len(todo)} {time.time() - t:.0f}s", flush=True)
        return [self.cache.get(k, "") for k in keys]

    def cloud_diarize(self, a: float, b: float) -> list[dict]:
        p = self.work / "cloud" / f"{a:.1f}-{b:.1f}.json"
        if p.exists():
            return json.load(open(p))
        p.parent.mkdir(exist_ok=True)
        segs = None
        for attempt in range(4):
            try:
                segs = diarize(self.x[int(a * SR):int(b * SR)])
                break
            except Exception as e:  # read timeouts on the worker; the job itself usually finished
                print(f"  cloud diarize {a:.0f}-{b:.0f} attempt {attempt + 1} failed: {e!r}"[:160], flush=True)
                if "'retryable': False" in repr(e):  # StepFun risk control refuses some audio outright
                    break
                time.sleep(5 * (attempt + 1))
        else:
            raise RuntimeError(f"cloud diarize gave up on {a:.0f}-{b:.0f}")
        if segs is None:
            segs = []  # recorded as an empty window: the live service would leave this stretch pending

        json.dump(segs, open(p, "w"), ensure_ascii=False)
        return segs


def vad_utterances(case: Case) -> list[tuple[float, float]]:
    p = case.work / "vad_utts.json"
    if p.exists():
        return [tuple(u) for u in json.load(open(p))]
    seg, out = Segmenter(), []
    for i in range(0, len(case.x), SR):
        t = EPOCH + dt.timedelta(seconds=i / SR)
        for a, b, _ in seg.push(t, case.x[i:i + SR]):
            out.append(((a - EPOCH).total_seconds(), (b - EPOCH).total_seconds()))
    json.dump(out, open(p, "w"))
    return out


def padded_spans(segs: list[dict], base: float = 0.0) -> list[tuple[float, float]]:
    """(a, b) offsets to send to ASR: each turn widened by ASR_PAD seconds, but never into a
    neighbouring turn of another speaker (halfway into the gap at most). Tight diarizer boundaries
    clip the first/last syllable and StepFun then hears no speech in short turns."""
    pad = float(os.environ.get("ASR_PAD", "0"))
    turns = sorted((g["start_ms"] / 1000, g["end_ms"] / 1000, g["speaker"]) for g in segs)
    out = []
    for i, (a, b, who) in enumerate(turns):
        prev_end = max((t[1] for t in turns[:i] if t[2] != who and t[1] <= a), default=a - 2 * pad)
        next_start = min((t[0] for t in turns[i + 1:] if t[2] != who and t[0] >= b), default=b + 2 * pad)
        out.append((base + max(0.0, a - min(pad, (a - prev_end) / 2)), base + b + min(pad, (next_start - b) / 2)))
    return out


def hybrid_segs(utts: list[tuple[float, float]], segs: list[dict], min_piece=0.5) -> list[dict]:
    """VAD utterances (window-relative seconds) cut where the diarizer changes speaker inside them;
    each piece takes the label of the turn covering most of it. Keeps the VAD's coverage (short
    backchannels the diarizer drops) while using the diarizer only for the cut points."""
    turns = sorted(((g["start_ms"] / 1000, g["end_ms"] / 1000, g["speaker"]) for g in segs))
    out = []
    for a, b in utts:
        cuts = sorted({t[0] for t in turns if a + min_piece < t[0] < b - min_piece} |
                      {t[1] for t in turns if a + min_piece < t[1] < b - min_piece})
        edges = [a] + cuts + [b]
        for x, y in zip(edges, edges[1:]):
            if y - x < 0.2:
                continue
            best = max(turns, key=lambda t: max(0.0, min(y, t[1]) - max(x, t[0])), default=None)
            who = best[2] if best and min(y, best[1]) - max(x, best[0]) > 0 else "S?"
            out.append(dict(start_ms=int(x * 1000), end_ms=int(y * 1000), speaker=who, text=""))
    return out


def _volc_window(self, a: float, b: float) -> list[dict]:
    p = self.work / "volc" / f"{a:.1f}-{b:.1f}.json"
    if p.exists():
        return json.load(open(p))
    p.parent.mkdir(exist_ok=True)
    import volc_asr
    for attempt in range(3):
        try:
            utts, info = volc_asr.transcribe(self.x[int(a * SR):int(b * SR)])
            break
        except Exception as e:
            print(f"  volc {a:.0f}-{b:.0f} attempt {attempt + 1} failed: {e!r}"[:160], flush=True)
            time.sleep(5)
    else:
        raise RuntimeError(f"volc gave up on {a:.0f}-{b:.0f}")
    segs = [{"speaker": "SPEAKER_0", **u, "engine": "volc"} for u in utts]
    print(f"  volc {a:.0f}-{b:.0f}: {info['seconds']} s, {len(segs)} utterances", flush=True)
    json.dump(segs, open(p, "w"), ensure_ascii=False)
    return segs


Case.volc_window = _volc_window


def rows_from_windows(case: Case, spk: Speakers, diar, name: str, need_asr: bool, utts=None):
    """The live second layer: windows of REFINE_WINDOW_S every REFINE_EVERY_S, later windows replace earlier rows."""
    total = len(case.x) / SR
    ends = list(np.arange(REFINE_WINDOW_S, total, REFINE_EVERY_S)) + [total]
    records, t_run, reg = [], time.time(), Registry()
    if diar.__name__ == "cloud":  # independent calls, 4 in flight; results land in the cache
        with cf.ThreadPoolExecutor(4) as ex:
            list(ex.map(lambda e: diar(max(0.0, e - REFINE_WINDOW_S), e), ends))
        print(f"  cloud diarizations cached in {time.time() - t_run:.0f}s", flush=True)
    for n, e in enumerate(ends):
        a = max(0.0, e - REFINE_WINDOW_S)
        x = case.x[int(a * SR):int(e * SR)]
        if diar.__name__ == "cloud":
            segs = diar(a, e)
        else:  # cache local diarizations per window so relabelling is cheap
            emb = os.path.basename(os.environ.get("EMB_MODEL", "campplus")).split("_")[2] if os.environ.get("EMB_MODEL") else "campplus"
            p = (Path(os.environ["DIAR_SEGS_DIR"]) if os.environ.get("DIAR_SEGS_DIR")
                 else case.work / f"local-{os.environ.get('DIAR_THRESHOLD', '1.0')}-{emb}") / f"{a:.1f}-{e:.1f}.json"
            if p.exists():
                segs = json.load(open(p))
            else:
                p.parent.mkdir(exist_ok=True)
                segs = diar(x)
                json.dump(segs, open(p, "w"))
        if utts is not None:
            segs = hybrid_segs([(u - a, v - a) for u, v in utts if u >= a and v <= e], segs)
        all_segs = segs
        since = 3.0 if n == 0 else max(3.0, (records[-1]["to_s"] - a))
        segs = clip_turns(segs, since)
        if need_asr and os.environ.get("TEXT_SOURCE") == "file":
            words_to_turns(segs, case.cloud_diarize(a, e))  # cached StepFun file-mode windows carry words
        elif need_asr and os.environ.get("SKIP_ASR"):
            for g in segs:
                g["text"] = "·"  # speaker-only experiments: keep the row, skip StepFun
        elif need_asr:
            texts = case.transcribe_many(padded_spans(segs, a))
            for g, t in zip(segs, texts):
                g["text"] = t
        try:
            names, score, means, secs = spk.label(all_segs, x, case.meta["device"])
        except Exception as err:  # no cluster long enough
            print("  label failed", a, repr(err)[:80])
            names, means, secs = {g["speaker"]: "他人" for g in segs}, {}, {}
        labels = {}  # same rule as the live service: 我, or a conversation-local id from the registry
        for k, who in names.items():
            if who == "我":
                labels[k] = "我"
            elif k in means and secs[k] >= MIN_SPEAKER_S:
                labels[k] = reg.assign(means[k], secs[k], DEVICE_PARAMS.get(case.meta["device"], {}).get("link", LINK_MIN))[0]
            else:
                labels[k] = "S?"
        lo, hi, rows = final_rows(segs, labels, EPOCH + dt.timedelta(seconds=a), e - a, since=since)
        records.append({"from": lo.isoformat(timespec="milliseconds"), "to": hi.isoformat(timespec="milliseconds"), "rows": rows,
                        "to_s": (hi - EPOCH).total_seconds()})
        if n % 20 == 0:
            print(f"  {name} window {n}/{len(ends)} {time.time() - t_run:.0f}s", flush=True)
    return apply_final(records)


def run(case: Case, name: str) -> list[dict]:
    if name in ("vad", "vad-owner"):
        utts = vad_utterances(case)
        texts = case.transcribe_many(utts)
        rows = []
        spk = Speakers() if name == "vad-owner" else None
        owner = spk.owner[case.meta["device"]] if spk else None
        for (a, b), t in zip(utts, texts):
            if not t:
                continue
            who = None
            if spk and b - a >= 0.5:
                who = "我" if float(spk.embed(case.x[int(a * SR):int(b * SR)]) @ owner) >= OWNER_MIN else "他人"
            rows.append(dict(start=(EPOCH + dt.timedelta(seconds=a)).isoformat(timespec="milliseconds"),
                             end=(EPOCH + dt.timedelta(seconds=b)).isoformat(timespec="milliseconds"), speaker=who, text=t))
        return rows
    spk = Speakers()
    if name == "cloud-window":
        def cloud(a, e):
            return case.cloud_diarize(a, e)
        return rows_from_windows(case, spk, cloud, name, need_asr=False)
    if name == "volc-window":  # Volcengine seed-asr-2.0 streaming API with speaker ids, one connection per window
        def volc(a, e):
            return case.volc_window(a, e)
        return rows_from_windows(case, spk, volc, name, need_asr=False)
    if name in ("local-window", "hybrid-window"):
        d = LocalDiarizer()

        def local(x):
            return d(x)
        return rows_from_windows(case, spk, local, name, need_asr=True, utts=vad_utterances(case) if name == "hybrid-window" else None)
    if name == "hybrid-offline":
        p = case.work / f"local-offline-{os.environ.get('DIAR_THRESHOLD', '1.0')}.json"
        segs = hybrid_segs(vad_utterances(case), json.load(open(p)))
        texts = case.transcribe_many([(g["start_ms"] / 1000, g["end_ms"] / 1000) for g in segs])
        for g, tx in zip(segs, texts):
            g["text"] = tx
        names, score, *_ = spk.label(segs, case.x, case.meta["device"])
        _, _, rows = final_rows(segs, names, EPOCH, len(case.x) / SR + 10)
        return rows
    if name == "local-offline":
        p = Path(os.environ["DIAR_SEGS"]) if os.environ.get("DIAR_SEGS") else \
            case.work / f"local-offline-{os.environ.get('DIAR_THRESHOLD', '1.0')}{os.environ.get('DIAR_MIN_ON', '')}.json"
        if p.exists():
            segs = json.load(open(p))
        else:
            t = time.time()
            segs = LocalDiarizer()(case.x)
            json.dump(segs, open(p, "w"))
            print(f"  local-offline diarized {len(case.x) / SR:.0f}s audio in {time.time() - t:.0f}s, {len({g['speaker'] for g in segs})} speakers")
        if os.environ.get("SKIP_ASR"):
            for g in segs:
                g["text"] = "·"
        else:
            texts = case.transcribe_many(padded_spans(segs))
            for g, tx in zip(segs, texts):
                g["text"] = tx
        names, score, means, secs = spk.label(segs, case.x, case.meta["device"])
        print("  cluster owner scores", {k: round(v, 3) for k, v in score.items()})
        reg, labels = Registry(), {}
        link = DEVICE_PARAMS.get(case.meta["device"], {}).get("link", LINK_MIN)
        for k, who in names.items():
            labels[k] = "我" if who == "我" else (reg.assign(means[k], secs[k], link)[0] if k in means and secs[k] >= MIN_SPEAKER_S else "S?")
        _, _, rows = final_rows(segs, labels, EPOCH, len(case.x) / SR + 10)
        return rows
    raise SystemExit(f"unknown candidate {name}")


def main():
    case = Case(Path(sys.argv[1]))
    (case.dir / "hyp").mkdir(exist_ok=True)
    for name in sys.argv[2:]:
        t = time.time()
        rows = run(case, name)
        out = case.dir / "hyp" / f"{name}.jsonl"
        with open(out, "w") as f:
            for r in rows:
                if not r.get("text"):
                    continue
                a = (dt.datetime.fromisoformat(r["start"]) - EPOCH).total_seconds()
                b = (dt.datetime.fromisoformat(r["end"]) - EPOCH).total_seconds()
                f.write(json.dumps({**r, "start": case.wall(a).isoformat(timespec="milliseconds"),
                                    "end": case.wall(b).isoformat(timespec="milliseconds")}, ensure_ascii=False) + "\n")
        print(f"{name}: {len(rows)} rows -> {out} ({time.time() - t:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
