"""Live transcript of the phone's recording.

    set -a; . .env; set +a; uv run python server.py [--dir <recordings>] [--port 8770]   (variables: .env.example)

Takes the audio pieces the app and the /record page upload, keeps them on disk as a queue and feeds them in time
order (only as fast as finalization keeps up), finds speech with FireRed Stream-VAD, finalizes sentences with
speakers every REFINE_EVERY_S, groups them into conversations and summarizes those; all state is in data/live.db.
"""
import argparse
import asyncio
import base64
import datetime as dt
import hmac
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse, Response

import audio
import db
import speakers
import summary
from conversations import Grouper

HERE = Path(__file__).resolve().parent
def env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise SystemExit(f"{name} is not set (see .env.example)")
    return v


VAD_MODEL = Path(env("VAD_MODEL_DIR"))            # FireRedVAD Stream-VAD checkpoint directory
DATA = Path(env("LIVE_DATA"))                     # live.db, the passes trace, owner-redimnet2.npz, voice clips
SR = 16000
FRAME_LEN, FRAME_SHIFT = 400, 160          # FireRed fbank: 25 ms window, 10 ms shift
MERGE_GAP_S = 0.6                          # VAD utterances closer than this are one
MAX_UTTERANCE_S = 15.0
REFINE_EVERY_S = int(os.environ.get("REFINE_EVERY_S", "30"))  # finalization cadence
REFINE_WINDOW_S = int(os.environ.get("REFINE_WINDOW_S", "240"))    # most audio one pass takes (catching up a backlog)
REFINE_MARGIN_S = 1.0                      # audio re-read before the last finalized time: the engine's sentence starts come a little late
ENGINE = os.environ.get("ENGINE", "volc")  # second-layer engine: volc (seed-asr-2.0 streaming API) | stepfun (file-mode service)
BUFFER_S = 900                             # audio kept for refinement; pieces are fed only while finalization is within BUFFER_S - 300
TURN_GAP_S = float(os.environ.get("TURN_GAP_S", "1.0"))  # same speaker, closer than this: one final row; 0 keeps the engine's sentences as rows
EDGE_S = 3.0                               # rows touching the window edges may be cut: left to the next pass
BRIDGE = env("ASR_UPLOAD_BRIDGE_URL")             # returns {upload_url, download_url} for a file name
DIARIZE = env("ASR_SERVICE_URL")                   # file-mode ASR service: POST /audio/transcriptions, GET /jobs/{id}
AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "")      # empty: no auth (loopback only); set when a proxy exposes the server

log = logging.getLogger("live")




class Segmenter:
    """FireRed Stream-VAD over a continuous sample stream; emits (start, end, samples) utterances."""

    def __init__(self):
        from fireredvad import FireRedStreamVad, FireRedStreamVadConfig
        self.vad = FireRedStreamVad.from_pretrained(str(VAD_MODEL), FireRedStreamVadConfig())
        self.buf = np.zeros(0, np.int16)      # samples not yet turned into frames
        self.audio = np.zeros(0, np.int16)    # recent samples for cutting utterances
        self.audio_base = 0                   # stream sample index of audio[0]
        self.anchors: list[tuple[int, dt.datetime]] = []  # (stream sample index, wall time)
        self.samples = 0
        self.frames = 0
        self.pending = None                   # utterance waiting for a possible merge

    def time_at(self, sample: int) -> dt.datetime:
        idx, t = next((a for a in reversed(self.anchors) if a[0] <= sample), self.anchors[0])
        return t + dt.timedelta(seconds=(sample - idx) / SR)

    def push(self, t: dt.datetime, x: np.ndarray) -> list:
        if not self.anchors or abs((self.time_at(self.samples) - t).total_seconds()) > 0.5:
            self.anchors.append((self.samples, t))  # new file or a gap: re-anchor the clock
        self.samples += len(x)
        self.audio = np.concatenate([self.audio, x])
        self.buf = np.concatenate([self.buf, x])
        out = []
        if len(self.buf) >= FRAME_LEN:
            k = (len(self.buf) - FRAME_LEN) // FRAME_SHIFT + 1
            # Only whole frames, keeping the overlap for the next call: splitting at arbitrary
            # sample counts drops frames at every boundary and the clock drifts ~2 %.
            for r in self.vad.detect_chunk(self.buf[:(k - 1) * FRAME_SHIFT + FRAME_LEN]):
                if r.is_speech_end:
                    out += self._utterance(r.speech_start_frame - 1, r.speech_end_frame - 1)
            self.frames += k
            self.buf = self.buf[k * FRAME_SHIFT:]
        out += self._flush_if_quiet()
        keep = self.samples - BUFFER_S * SR
        if keep > self.audio_base:
            self.audio = self.audio[keep - self.audio_base:]
            self.audio_base = keep
        return out

    def speaking(self) -> bool:
        """An utterance is still open (the VAD is in speech, or one waits for a merge): last_speech does not cover it yet."""
        return self.pending is not None or self.vad.postprocessor.state.name != "SILENCE"

    def sample_at(self, t: dt.datetime) -> int:
        i = next((k for k in range(len(self.anchors) - 1, -1, -1) if self.anchors[k][1] <= t), 0)
        idx, t0 = self.anchors[i]
        s = idx + int((t - t0).total_seconds() * SR)
        # a time in the gap before the next session has no audio: the first sample after the gap
        return min(s, self.anchors[i + 1][0]) if i + 1 < len(self.anchors) else s

    def window(self, seconds: float, start: dt.datetime | None = None) -> tuple[dt.datetime, np.ndarray]:
        """Up to `seconds` of audio from `start` (default: the last `seconds` up to now)."""
        if start is None:
            a = max(self.audio_base, self.samples - int(seconds * SR))
        else:
            a = min(max(self.audio_base, self.sample_at(start)), self.samples)
        b = min(self.samples, a + int(seconds * SR))
        return self.time_at(a), self.audio[a - self.audio_base:b - self.audio_base]

    def _utterance(self, f0: int, f1: int) -> list:
        a, b = f0 * FRAME_SHIFT, f1 * FRAME_SHIFT
        p = self.pending
        if p and (a - p[1]) / SR < MERGE_GAP_S and (b - p[0]) / SR <= MAX_UTTERANCE_S:
            self.pending = (p[0], b)
            return []
        out = self._emit() if p else []
        self.pending = (a, b)
        return out

    def _flush_if_quiet(self) -> list:
        if self.pending and (self.frames * FRAME_SHIFT - self.pending[1]) / SR >= MERGE_GAP_S \
                and self.vad.postprocessor.state.name == "SILENCE":
            return self._emit()
        return []

    def _emit(self) -> list:
        a, b = self.pending
        self.pending = None
        x = self.audio[max(0, a - self.audio_base):max(0, b - self.audio_base)]
        return [(self.time_at(a), self.time_at(b), x)] if len(x) else []


def _http(url, data=None, headers=None, method=None) -> bytes:
    import urllib.request
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "curl/8", **(headers or {})}, method=method)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


FILE_MODELS = tuple(os.environ.get("FILE_MODELS", "stepaudio-2.5-asr-8006,doubao-seedasr-2.0").split(","))  # file-mode engines, in order of preference


def engine_segs(x: np.ndarray) -> list[dict]:
    """Sentences with times and speaker ids for a window of audio, from the configured engine."""
    if ENGINE != "volc":
        return diarize(x)
    import volc_asr
    t = time.perf_counter()
    utts, info = volc_asr.transcribe(x)
    log.info("volc: %.0f s audio in %.1f s, %d utterances, logid %s", len(x) / SR, time.perf_counter() - t, len(utts), info.get("logid"))
    return [{"speaker": "SPEAKER_0", **u, "engine": "volc"} for u in utts]


def diarize(x: np.ndarray, model: str = FILE_MODELS[0]) -> list[dict]:
    """File-mode ASR with speakers and word times on a window of audio (via the file-mode ASR service).
    StepFun's risk control refuses some content outright; that window is then sent to Doubao and every
    segment carries `engine` so the rows say which engine wrote them."""
    t_enc = time.perf_counter()
    ogg = subprocess.run(["ffmpeg", "-v", "error", "-f", "s16le", "-ar", str(SR), "-ac", "1", "-i", "pipe:0",
                          "-c:a", "libopus", "-b:a", "16k", "-f", "ogg", "pipe:1"], input=x.tobytes(),
                         capture_output=True, check=True).stdout
    t_up = time.perf_counter()
    up = json.loads(_http(BRIDGE, json.dumps({"filename": "live.ogg", "content_type": "audio/ogg"}).encode(),
                          {"Authorization": f"Bearer {os.environ['ASR_UPLOAD_BRIDGE_TOKEN']}", "Content-Type": "application/json"}))
    _http(up["upload_url"], ogg, {"Content-Type": "audio/ogg"}, "PUT")
    auth = {"Authorization": "Basic " + base64.b64encode(os.environ["ASR_SERVICE_BASIC_AUTH"].encode()).decode()}
    job = json.loads(_http(f"{DIARIZE}/audio/transcriptions", json.dumps({"audio_url": up["download_url"], "model": model}).encode(),
                           {**auth, "Content-Type": "application/json"}))["job_id"]
    t_job = time.perf_counter()
    for _ in range(120):
        r = json.loads(_http(f"{DIARIZE}/jobs/{job}", headers=auth))
        if r["status"] == "COMPLETED":
            log.info("diarize %s: %.0f s audio, encode %.1f s, upload %.1f s, job %.1f s", model, len(x) / SR,
                     t_up - t_enc, t_job - t_up, time.perf_counter() - t_job)
            for g in r["segments"]:
                g["engine"] = model.split("-")[0]
            return r["segments"]
        if r["status"] == "FAILED":
            err = str(r.get("error"))
            if "risk" in err and model == FILE_MODELS[0]:
                log.warning("StepFun refused the window (%s); sending it to Doubao", err[:80])
                return diarize(x, FILE_MODELS[1])
            raise RuntimeError(err)
        time.sleep(1)
    raise RuntimeError(f"diarization job {job} still running after 120 s")


CLIP_JITTER_MS = 300  # a re-cut turn's start moves about this much between passes


def clip_turns(segs: list[dict], since_s: float) -> list[dict]:
    """The previous pass finalized every turn starting before `since` (its `to` is the start of the first
    turn it left out), so those are dropped here, whole: keeping the part after `since` re-emitted the
    sentence of a speaker overlapping the cut turn (phone case 09-29 13:12). Only a turn starting within
    the re-cut jitter of `since` is kept, clipped to it."""
    out = []
    for g in segs:
        if g["start_ms"] < since_s * 1000 - CLIP_JITTER_MS or g["end_ms"] <= since_s * 1000 + 200:
            continue
        if g["start_ms"] < since_s * 1000:
            g = {**g, "start_ms": int(since_s * 1000)}
        out.append(g)
    return out


def label_sentences(dia, embed, segs: list[dict], x: np.ndarray) -> list:
    """Speaker labels from the conversation clustering, in time order; returns the diarist mark after each
    sentence (None when too short to embed) so a pass can take back the sentences it leaves open."""
    marks = []
    for g in segs:
        a, b = g["start_ms"] / 1000, g["end_ms"] / 1000
        g["engine_speaker"] = g.get("speaker")
        if b - a < 0.3:
            g["speaker"], g["confidence"] = "S?", 0.0
            marks.append(None)
            continue
        g["speaker"], g["confidence"] = dia.add(embed(x[int(a * SR):int(min(b, a + 15) * SR)]), b - a)
        marks.append(dia.mark())
    return marks


def final_rows(segs: list[dict], names: dict, t0: dt.datetime, win_s: float, conf: dict | None = None,
               since: float = EDGE_S, at=None) -> tuple[dt.datetime, dt.datetime, list[dict]]:
    """Second pass: the diarized speaker turns of one window become the final rows for [from, to).

    VAD utterances glue fast back-and-forth into one line (no pause to cut at), so the final
    layer is built from the diarized turns instead. Consecutive pieces of one speaker are joined;
    rows near the window edges may be cut mid-turn and are left for the next, later window.
    `since` (seconds into the window) is where the previous pass stopped: rows before it are
    already final and never rewritten. `at` maps seconds into the window to wall time; the default
    is linear from t0, the live server passes the segmenter's clock (a new session's file starting
    inside the window jumps it).
    """
    rows = []
    for g in sorted(segs, key=lambda g: g["start_ms"]):
        a, b, who = g["start_ms"] / 1000, g["end_ms"] / 1000, names[g["speaker"]]
        if rows and rows[-1]["speaker"] == who and a - rows[-1]["b"] < TURN_GAP_S:
            rows[-1]["b"], rows[-1]["text"] = b, rows[-1]["text"] + g["text"]
        else:
            rows.append(dict(a=a, b=b, speaker=who, text=g["text"]))
    lo, hi = since, win_s - EDGE_S
    keep = [r for r in rows if r["a"] >= lo - 0.05]  # turns are clipped to `since` by the caller
    cut = next((i for i, r in enumerate(keep) if r["b"] > hi), len(keep))
    to = keep[cut]["a"] if cut < len(keep) else hi
    at = at or (lambda s: t0 + dt.timedelta(seconds=s))
    out = [dict(id="f" + at(r["a"]).strftime("%Y%m%d%H%M%S%f")[:17], start=at(r["a"]).isoformat(timespec="milliseconds"),
                end=at(r["b"]).isoformat(timespec="milliseconds"), speaker=r["speaker"], text=r["text"].strip(),
                speaker_confidence=(conf or {}).get(r["speaker"]), state="final")
           for r in keep[:cut] if r["text"].strip()]
    return at(lo), at(to), out


app = FastAPI()
clients: set[WebSocket] = set()
state = {"chunks": None, "events": [], "commit": "", "devices": {}, "embed": None, "dias": []}  # devices: one pipeline's state each


async def broadcast(msg: dict):
    for ws in list(clients):
        try:
            await ws.send_json(msg)
        except Exception:
            clients.discard(ws)


def note(text: str, kind: str = "info"):
    """One line of the system log the page shows: what arrived, what each pass did, state changes."""
    e = {"type": "event", "at": dt.datetime.now().isoformat(timespec="seconds"), "kind": kind, "text": text}
    state["events"].append(e)
    del state["events"][:-300]
    asyncio.ensure_future(broadcast(e))


def find_conversation(conv_id: str) -> dict | None:
    return db.conversation(conv_id)


def conversation_rows(c: dict) -> list[dict]:
    """Its device's rows in its time; a conversation from before per-device pipelines (no device) takes every row."""
    return db.sentences(c["start"], c["end"], inclusive=True, source=c.get("device"))


summary_q: asyncio.Queue = asyncio.Queue()


def queue_summary(c: dict, keep: bool = False):
    """keep: the current summary stays on the page until the new one is written (an upgrade to a newer prompt)."""
    if not keep:
        db.put_summary({"conversation": c["id"], "status": "pending", "error": None})
    summary_q.put_nowait(c)


def all_conversations() -> list[dict]:
    """Every closed conversation with its summary, newest first."""
    return db.conversations()


_tags = {"at": 0.0, "counts": {}}


def tag_counts() -> dict[str, int]:
    """Recounted at most once a minute: a backfill summarizes thousands in a row."""
    if time.time() - _tags["at"] < 60:
        return _tags["counts"]
    n = {}
    for c in all_conversations():
        for t in (c["summary"] or {}).get("tags") or []:
            n[t] = n.get(t, 0) + 1
    _tags.update(at=time.time(), counts=dict(sorted(n.items(), key=lambda kv: -kv[1])))
    return _tags["counts"]


SUMMARY_WORKERS = int(os.environ.get("SUMMARY_WORKERS", "4"))  # 6 drew ~100 429s in a full re-run (09-30); 10 got 429s at once


async def summary_worker():
    """Queued first: conversations closed while the service was down, summaries that never finished,
    and summaries from an older prompt (a new field such as tags reaches the old ones too)."""
    for c in reversed(all_conversations()):
        s = c["summary"]
        if not s or s["status"] == "pending":
            queue_summary(c)
        elif s["status"] == "done" and s.get("prompt_version", 0) < summary.PROMPT_VERSION:
            queue_summary(c, keep=True)
    await asyncio.gather(*(summarize_loop() for _ in range(SUMMARY_WORKERS)))


async def summarize_loop():
    while True:
        c = await summary_q.get()
        for delay in (10, 60, 300, None):
            try:
                res = await asyncio.to_thread(summary.summarize, c, conversation_rows(c), list(tag_counts())[:60])
                break
            except Exception as e:  # HTTP errors, timeouts, a reply that is not JSON
                log.warning("summary %s failed: %r", c["id"], e)
                res = {"conversation": c["id"], "status": "failed", "error": repr(e)[:300]}
                if delay:
                    await asyncio.sleep(delay)
        old = db.summary(c["id"])
        if res["status"] == "failed" and old and old["status"] == "done":  # a failed upgrade keeps the old summary
            continue
        db.put_summary(res)
        note(f"对话 {c['start'][11:19]} 的摘要" + (f"：{res['title']}" if res["status"] == "done" else f"生成失败：{res['error']}"),
             "state" if res["status"] == "done" else "error")
        await broadcast({"type": "summary", **res})


def iso(t: dt.datetime) -> str:
    return t.isoformat(timespec="milliseconds")


async def pipelines():
    """One pipeline per device with audio after its finalized time: a phone sending last night's backlog while an
    iPhone records today is transcribed on its own timeline. A device's pipeline lives on once started."""
    from diarist import load_embedder
    state["embed"] = await asyncio.to_thread(load_embedder)
    running: dict[str, asyncio.Task] = {}
    while True:
        devices = [r[0] for r in db.conn().execute("SELECT DISTINCT device FROM pieces")]
        after = {d: dt.datetime.fromisoformat(v) for d in devices if (v := db.kv(f"finalized_to:{d}"))}
        for d in audio.pending(after):
            if d not in running:
                log.info("pipeline for %s from %s", d, after.get(d))
                running[d] = asyncio.create_task(pipeline(d), name=f"pipeline:{d}")
                running[d].add_done_callback(lambda t, d=d: (log.error("pipeline %s ended: %r", d, t.exception()),
                                                             note(f"{d} 的处理任务退出：{t.exception()!r}"[:160], "error")) if not t.cancelled() else None)
        await asyncio.sleep(5)


async def pipeline(device: str):
    seg = Segmenter()
    resume = db.kv(f"finalized_to:{device}")
    st = state["devices"][device] = {"seg": seg, "done_to": dt.datetime.fromisoformat(resume) if resume else None, "audio_end": None,
                                     "last_speech": None, "refine": None, "conversation": None, "latency": []}
    if resume:
        log.info("%s resuming after the last finalized row at %s", device, resume)

    async def feed():
        """The device's audio stream into the segmenter, taking the next stretch only while finalization is less than
        BUFFER_S - 300 s of audio behind: the buffer must not drop audio before it is finalized."""
        async for t, x in audio.stream(device, st["done_to"]):
            while True:
                done = st["done_to"]
                a = max(seg.audio_base, seg.sample_at(done)) if seg.anchors and done else seg.audio_base
                if (seg.samples - a) / SR <= BUFFER_S - 300:
                    break
                await asyncio.sleep(1)
            st["audio_end"] = t + dt.timedelta(seconds=len(x) / SR)
            try:
                utts = seg.push(t, x)
            except Exception:
                log.exception("%s: segmenter failed on %.1f s at %s", device, len(x) / SR, t.time())
                continue
            for u in utts:
                st["last_speech"] = u[1]  # the refiner skips diarizing stretches with no VAD speech

    async def refine_loop():
        from diarist import Diarist
        bank = {k: e for k, (_, e) in db.people().items()}
        embed, dia, grouper = state["embed"], Diarist(dict(np.load(DATA / "owner-redimnet2.npz")), people=bank), Grouper(open_=db.kv(f"open_conversation:{device}"))
        state["dias"].append(dia)  # a rename on the conversation page teaches every diarist a name
        if grouper.open:
            log.info("%s continuing the open conversation from %s", device, grouper.open["start"])
            note(f"{device}：接着上次打开的对话 {grouper.open['start'][11:19]}", "state")
        else:
            note(f"{device}：开始处理", "state")
        done_to = st["done_to"]  # wall time up to which rows are final

        def record(c: dict) -> dict:
            cid = "c" + c["start"].replace("-", "").replace(":", "").replace("T", "")[:14]
            if (other := db.conversation(cid)) and other.get("device") != device:  # two devices starting the same second
                cid += "-" + re.sub(r"[^\w]", "_", device)
            return {"id": cid, "start": c["start"], "end": c["end"], "participants": c["participants"],
                    "sentences": len(c["sentences"]), "speech_s": round(c["speech_s"], 1), "device": device}

        async def commit(closed: list[dict], rows: list[dict] | None = None, lo: str = "", hi: dt.datetime | None = None):
            """One transaction: the pass's rows replace [lo, hi), closed conversations, the finalized time, the open conversation."""
            recs = [record(c) for c in closed]
            with db.tx() as t:
                if rows is not None:
                    db.replace_sentences(t, lo, iso(hi), rows, device)
                for rec in recs:
                    db.insert_conversation(t, rec)
                    db.put_summary({"conversation": rec["id"], "status": "pending", "error": None}, t)
                if hi:
                    db.put_kv(f"finalized_to:{device}", iso(hi), t)
                db.put_kv(f"open_conversation:{device}", grouper.open, t)
            if hi:
                st["done_to"] = hi
            if rows is not None:
                await broadcast({"type": "final", "from": lo, "to": iso(hi), "rows": rows})
            for rec in recs:
                dia.reset()
                summary_q.put_nowait(rec)
                log.info("%s conversation closed %s", device, rec)
                note(f"对话结束：{rec['start'][11:19]} 到 {rec['end'][11:19]}，{rec['sentences']} 句，参与 {'、'.join(rec['participants']) or '—'}", "state")
                await broadcast({"type": "conversation", **db.conversation(rec["id"])})
            if not grouper.open:
                st["conversation"] = None

        async def refine_pass() -> bool:
            """One pass over [done_to - margin, + window]; True when a backlog remains."""
            nonlocal done_to
            if not seg.anchors:
                return False  # no audio yet
            t_run = time.perf_counter()
            # from where the last pass stopped, or from the oldest buffered audio
            t0, x = seg.window(REFINE_WINDOW_S, done_to - dt.timedelta(seconds=REFINE_MARGIN_S) if done_to else seg.time_at(seg.audio_base))
            a0 = seg.sample_at(t0)
            at = lambda s: seg.time_at(a0 + int(s * SR))  # the window may span a session boundary, where the clock jumps
            # a first pass may start mid-sentence: skip its edge; later passes start at a sentence the last one left open
            since = EDGE_S if done_to is None else max(0.0, (seg.sample_at(done_to) - a0) / SR)
            if len(x) / SR - since < EDGE_S + 10:
                return False
            hi = at(len(x) / SR - EDGE_S)
            if not seg.speaking() and (st["last_speech"] is None or st["last_speech"] <= (done_to or t0)):
                # silence: nothing to diarize, but the clock must advance so a conversation can close on time
                # (speech still going on at the end of the fed audio is not silence: a replayed 10-01 14:43 lost 90 s that way)
                done_to = hi
                await commit(grouper.advance(hi), hi=hi)
                return (seg.time_at(seg.samples) - hi).total_seconds() > REFINE_WINDOW_S - REFINE_MARGIN_S
            was_open = bool(grouper.open)
            m0 = dia.mark()
            try:
                segs = clip_turns(await asyncio.to_thread(engine_segs, x), since)
                engine = segs[0]["engine"] if segs else None
                marks = await asyncio.to_thread(label_sentences, dia, embed, segs, x)
            except Exception as e:
                dia.rollback(m0)
                log.warning("%s refine failed: %r", device, e)
                note(f"{device}：这一轮引擎调用失败：{str(e)[:120]}，下一轮重试", "error")
                st["refine"] = {"error": str(e)[:200]}
                return False
            labels = {g["speaker"]: g["speaker"] for g in segs}
            by_label = {}
            for g in segs:  # final_rows keys confidence by label
                by_label[g["speaker"]] = max(by_label.get(g["speaker"], 0.0), g["confidence"])
            lo, hi, rows = final_rows(segs, labels, t0, len(x) / SR, by_label, since, at)
            kept = [m for g, m in zip(segs, marks) if m and at(g["start_ms"] / 1000) < hi]  # sentences left open come back next pass
            dia.rollback(kept[-1] if kept else m0)
            with open(DATA / f"{lo.date()}.passes.jsonl", "a") as f:  # every pass's raw engine output, for tracing lost rows
                f.write(json.dumps({"at": dt.datetime.now().isoformat(timespec="seconds"), "t0": iso(t0), "since": since, "from": iso(lo),
                                    "to": iso(hi), "segs": segs},  # each sentence carries engine_speaker and its cluster's owner score (confidence)
                                   ensure_ascii=False) + "\n")
            shown = dt.datetime.now()
            for r in rows:  # end of speech -> on the page: the latency the person sees
                r["source"], r["engine"], r["shown_at"] = device, engine, iso(shown)
                st["latency"].append(round((shown - dt.datetime.fromisoformat(r["end"])).total_seconds(), 1))
            del st["latency"][:-50]
            done_to = hi
            closed = [c for r in rows for c in grouper.sentence(r)]
            closed += grouper.advance(hi)
            await commit(closed, rows, iso(lo), hi)
            if grouper.open:
                o = grouper.open
                st["conversation"] = {"start": o["start"], "end": o["end"], "sentences": len(o["sentences"]), "device": device}
            st["refine"] = {"at": dt.datetime.now().isoformat(timespec="seconds"), "ms": round((time.perf_counter() - t_run) * 1000),
                               "speakers": by_label, "changed": len(rows),
                               "backlog_s": round((seg.time_at(seg.samples) - hi).total_seconds())}
            log.info("refine %s: %s", device, st["refine"])
            note(f"定稿一轮：{device} 送 {len(x) / SR:.0f} s（含 {since:.0f} s 上下文）给 {engine or ENGINE}，"
                 f"{st['refine']['ms'] / 1000:.1f} s，{len(rows)} 句，定稿到 {hi.strftime('%H:%M:%S')}，积压 {st['refine']['backlog_s']} s")
            if grouper.open and not was_open:
                note(f"{device}：对话开始 {grouper.open['start'][11:19]}", "state")
            return st["refine"]["backlog_s"] > REFINE_WINDOW_S - REFINE_MARGIN_S

        while True:
            await asyncio.sleep(REFINE_EVERY_S)
            while await refine_pass():  # timer-driven: silence also advances, so conversations close on time
                pass
            closed = []
            end_at = db.kv(f"end_at:{device}")
            if end_at and not audio.queued(device):  # the recorder stopped and its last audio is processed
                closed += grouper.end(dt.datetime.fromisoformat(end_at))
                db.put_kv(f"end_at:{device}", None)
            idle = grouper.tick()  # the safety net: nothing at all for IDLE_S (the recorder died without an end)
            if idle:
                note(f"{device}：20 分钟没有任何事件，按兜底规则关闭对话", "warn")
            closed += idle
            if closed:
                await commit(closed)

    def watched(coro, name):
        """A task whose death is logged: an unhandled exception would otherwise stop the pipeline silently."""
        t = asyncio.create_task(coro, name=name)
        t.add_done_callback(lambda t: (log.error("task %s ended: %r", name, t.exception()),
                                       note(f"内部任务 {name} 退出：{t.exception()!r}"[:160], "error")) if not t.cancelled() else None)
        return t

    watched(feed(), f"feed:{device}")
    await refine_loop()


def device_status(device: str, st: dict) -> dict:
    end, done, lat = st["audio_end"], st["done_to"], st["latency"]
    return {"device": device, "audio_end": end.isoformat(timespec="seconds") if end else None,
            "behind_s": round((dt.datetime.now() - end).total_seconds(), 1) if end else None,
            "queued": audio.queued(device), "done_to": done.isoformat(timespec="seconds") if done else None,
            "last_speech": st["last_speech"].isoformat(timespec="seconds") if st["last_speech"] else None,
            "refine": st["refine"], "conversation": st["conversation"],
            "latency_s": sorted(lat)[len(lat) // 2] if lat else None}


def status() -> dict:
    """What the page's status line shows (the device whose audio came in last), every device's pipeline, and the
    pieces still queued over all devices."""
    ds = [device_status(d, st) for d, st in state["devices"].items()]
    top = max(ds, key=lambda d: d["audio_end"] or "", default={"device": None, "audio_end": None, "behind_s": None, "done_to": None,
                                                                 "last_speech": None, "refine": None, "conversation": None, "latency_s": None})
    return {**top, "queued": sum(d["queued"] for d in ds), "devices": ds, "commit": state["commit"], "chunks": state["chunks"], "engine": ENGINE}


async def status_loop():
    while True:
        await broadcast({"type": "status", **status()})
        await asyncio.sleep(2)


def authorized(cookies, headers) -> bool:
    ok = lambda v: bool(v) and hmac.compare_digest(v, AUTH_TOKEN)
    return ok(cookies.get("token")) or ok(headers.get("authorization", "")[7:])


@app.middleware("http")
async def auth(request: Request, call_next):
    """Open any page once with ?token=<AUTH_TOKEN>: a year-long cookie replaces the browser's basic-auth prompt
    that Android Chrome forgets on every reload."""
    if not AUTH_TOKEN:
        return await call_next(request)
    t = request.query_params.get("token")
    if t and hmac.compare_digest(t, AUTH_TOKEN):
        u = request.url.remove_query_params("token")
        resp = RedirectResponse(u.path + (f"?{u.query}" if u.query else ""), status_code=303)  # relative: the proxy owns the scheme
        resp.set_cookie("token", AUTH_TOKEN, max_age=365 * 86400, httponly=True, samesite="lax")
        return resp
    if authorized(request.cookies, request.headers):
        return await call_next(request)
    return Response("token required: open /?token=<AUTH_TOKEN> once", 401)


NO_CACHE = {"Cache-Control": "no-cache"}  # pages change with deploys; revalidate by etag instead of heuristic caching


DIST = HERE / "static/dist"  # the web app (web/, built by web/deploy.sh)


@app.get("/")
@app.get("/conversations")
@app.get("/c/{conv_id}")
@app.get("/record")
@app.get("/speakers")
@app.get("/p/{name}")
async def page(conv_id: str = "", name: str = ""):
    return FileResponse(DIST / "index.html", headers=NO_CACHE)


@app.get("/assets/{name}")
async def asset(name: str):
    """Build output with a content hash in the name: never changes, so cached for good."""
    p = DIST / "assets" / name
    if not p.is_file():
        raise HTTPException(404, "no such asset")
    return FileResponse(p, headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/app.apk")
async def apk():
    """The Android client, for installing from the phone's browser."""
    p = DATA / "live-record.apk"
    if not p.exists():
        raise HTTPException(404, "no apk uploaded")
    return FileResponse(p, media_type="application/vnd.android.package-archive", filename="live-record.apk")


@app.post("/api/chunks")
async def chunk(request: Request, device: str, start: int):
    """One self-contained audio file (the apps' Opus or AAC pieces), `start` its device time in unix ms. Kept on disk (also
    for playback) and queued; the device's pipeline takes its pieces in time order at the pace finalization keeps up
    with, so a client back from an outage sends its whole backlog at once, whatever other devices are doing."""
    seconds = await asyncio.to_thread(audio.save, device, start, await request.body(), request.headers.get("content-type", ""))
    if seconds is None:
        raise HTTPException(400, "could not decode")
    state["chunks"] = {"device": device, "at": time.time()}
    late = "（早于这台设备已处理的位置，只存不转写）" if start <= audio.read_to.get(device, -1) else ""
    note(f"收到 {device} 的块 {seconds:.1f} s，起于 {dt.datetime.fromtimestamp(start / 1000).strftime('%H:%M:%S')}{late}", "warn" if late else "info")
    return {"seconds": round(seconds, 2)}


@app.post("/api/chunks/end")
async def chunk_end(device: str, t: int):
    """The recorder stopped at device time t (unix ms): the open conversation closes once its audio is processed."""
    end = dt.datetime.fromtimestamp(t / 1000)
    db.put_kv(f"end_at:{device}", iso(end))  # kept across a restart: the queue may still hold the audio before it
    log.info("%s stopped recording at %s", device, end.time())
    note(f"{device} 停止录音（{end.strftime('%H:%M:%S')}），处理完剩余音频后关闭对话")
    return {"ok": True}


@app.get("/api/events")
async def events():
    return state["events"]


@app.get("/api/status")
async def status_now():
    return status()


@app.get("/api/sentences")
async def sentences(date: str | None = None):
    day = dt.date.fromisoformat(date) if date else dt.date.today()
    return db.sentences(day.isoformat(), (day + dt.timedelta(days=1)).isoformat())


@app.get("/api/conversations")
async def conversations(date: str | None = None):
    day = dt.date.fromisoformat(date) if date else dt.date.today()
    closed = db.conversations(day.isoformat(), (day + dt.timedelta(days=1)).isoformat())
    return closed + [{**st["conversation"], "state": "open"} for st in state["devices"].values()
                     if st["conversation"] and st["conversation"]["start"][:10] == day.isoformat()]


@app.get("/api/days")
async def days():
    """Days that have sentences, newest first."""
    return [r[0] for r in db.conn().execute("SELECT DISTINCT substr(start, 1, 10) d FROM sentences ORDER BY d DESC")]


@app.get("/api/search")
async def search(q: str = "", tag: str = "", kind: str = "", who: str = "", after: str = "", before: str = "",
                 limit: int = 30):
    """Closed conversations newest first, filtered by a tag and by words in the title, summary, tags or
    todos (and in the transcript when the summary does not match), by a participant, and by start time;
    `before` is the start of the last one already shown."""
    out = []
    for c in all_conversations():
        s = c["summary"] or {}
        if after and c["start"] < after:
            break
        if before and c["start"] >= before or who and who not in (c.get("participants") or []) or tag and tag not in (s.get("tags") or []) or kind and c.get("kind", "对话") != kind:
            continue
        if q:
            meta = json.dumps([s.get(k) for k in ("title", "summary", "tags", "todos", "decisions")], ensure_ascii=False)
            if q.lower() not in meta.lower() and not db.has_text(c["start"], c["end"], q, c.get("device")):
                continue
        out.append(c)
        if len(out) == limit:
            break
    return out


@app.get("/api/tags")
async def tags():
    return tag_counts()


@app.get("/api/conversation/{conv_id}")
async def conversation(conv_id: str):
    c = find_conversation(conv_id)
    if not c:
        raise HTTPException(404, "no such conversation")
    return {**c, "summary": db.summary(conv_id), "rows": conversation_rows(c)}


@app.post("/api/conversation/{conv_id}/summarize")
async def resummarize(conv_id: str):
    c = find_conversation(conv_id)
    if not c:
        raise HTTPException(404, "no such conversation")
    queue_summary(c)
    return {"status": "pending"}


@app.get("/api/names")
async def names():
    """Names already in use, most conversations first: suggestions for a rename."""
    n = {}
    for c in all_conversations():
        for p in c.get("participants") or []:
            if p != "我" and not re.fullmatch(r"S(\d+|\?)", p):
                n[p] = n.get(p, 0) + 1
    for p in db.people():
        n.setdefault(p, 0)
    return sorted(n, key=lambda k: -n[k])


@app.post("/api/conversation/{conv_id}/rename")
async def rename(conv_id: str, body: dict):
    frm, to = body.get("from", ""), (body.get("to") or "").strip()
    return await apply_rename(conv_id, frm, to)


async def apply_rename(conv_id: str, frm: str, to: str) -> dict:
    """One label of one conversation becomes a name: its rows, the participants, its voice and pins change in one
    transaction, the summary is redone, and when the recording is still here up to ENROLL sentences of it teach the
    diarist that voice for later conversations."""
    c = find_conversation(conv_id)
    if not c:
        raise HTTPException(404, "no such conversation")
    if not to or to == "S?" or len(to) > 20 or to == frm:
        raise HTTPException(400, "bad name")
    hit = [r for r in conversation_rows(c) if r.get("speaker") == frm]
    if not hit:
        raise HTTPException(400, f"no sentence of {frm}")
    c["participants"] = list(dict.fromkeys(to if p == frm else p for p in c["participants"]))
    with db.tx() as t:
        t.execute("UPDATE sentences SET speaker = ? WHERE start >= ? AND start <= ? AND speaker = ?", (to, c["start"], c["end"], frm))
        t.execute("UPDATE conversations SET participants = ? WHERE id = ?", (json.dumps(c["participants"], ensure_ascii=False), conv_id))
        t.execute("UPDATE OR REPLACE voices SET label = ? WHERE conv = ? AND label = ?", (to, conv_id, frm))  # voices and pins follow
        t.execute("UPDATE OR REPLACE apart SET voice = ? WHERE voice = ?", (f"{conv_id}|{to}", f"{conv_id}|{frm}"))
    _speakers["at"] = 0.0
    queue_summary(c, keep=True)
    learnt = 0
    if to != "我" and not re.fullmatch(r"S\d+", to) and state.get("embed"):  # S<n> means "not known": nothing to learn
        long = sorted((r for r in hit if (dt.datetime.fromisoformat(r["end"]) - dt.datetime.fromisoformat(r["start"])).total_seconds() >= 1.5),
                      key=lambda r: r["start"])[:ENROLL]
        es = []
        for r in long:
            try:
                x = np.frombuffer(await cut(r["start"], min(r["end"], (dt.datetime.fromisoformat(r["start"]) + dt.timedelta(seconds=15)).isoformat()), "s16le", r.get("source")), np.int16)
            except HTTPException:  # history conversations have no recording here
                continue
            es.append(await asyncio.to_thread(state["embed"], x))
        if es:
            n0, e0 = db.people().get(to, (0, np.zeros(len(es[0]))))
            n = n0 + len(es)
            mean = (e0 * n0 + np.sum(es, 0)) / n
            db.conn().execute("INSERT OR REPLACE INTO people VALUES (?, ?, ?)", (to, n, db.blob(mean)))
            for dia in state["dias"]:
                dia.people[to] = mean / np.linalg.norm(mean)
            learnt = len(es)
    note(f"对话 {c['start'][11:19]}：{frm} 改名为 {to}" + (f"，记住了 {learnt} 句的声纹" if learnt else ""), "state")
    return {"participants": c["participants"], "renamed": len(hit), "voiceprints": learnt}


ENROLL = 20

# speakers page: one voiceprint per conversation x label (history ones computed from the old recorder's audio and
# uploaded with a clip each; live ones computed here), grouped across conversations by speakers.py
VOICES = DATA / "voices"  # clips/: the history voices' samples
SPEAKER_LINK = float(os.environ.get("SPEAKER_LINK", "0.5"))  # cosine (device-centred) to join an anonymous group
SPEAKER_SUGGEST = float(os.environ.get("SPEAKER_SUGGEST", "0.6"))  # cosine to offer an anonymous group to a name
_speakers = {"at": 0.0, "groups": None}


def conv_start(conv_id: str) -> str:
    x = conv_id[1:]
    return f"{x[:4]}-{x[4:6]}-{x[6:8]}T{x[8:10]}:{x[10:12]}:{x[12:14]}"


def speaker_groups() -> list[dict]:
    """Takes seconds (every conversation and voice): kept 10 min, redone after a rename or new voices."""
    if _speakers["groups"] is not None and time.time() - _speakers["at"] < 600:
        return _speakers["groups"]
    convs = {c["id"]: c for c in all_conversations()}
    out = []
    for g in speakers.speakers(db.voices(), SPEAKER_LINK, SPEAKER_SUGGEST, db.apart()):
        out.append({"id": g["id"], "name": g["name"], **describe(g["members"], convs),
                    "suggested": [{"id": speakers.key(s["members"][0]), "score": s["score"], **describe(s["members"], convs)}
                                  for s in g["suggested"]]})
    order = lambda g: (g["name"] is not None, g["name"] == "我", -g["count"])  # unnamed and frequent first, 我 last
    out.sort(key=order)
    _speakers.update(at=time.time(), groups=out)
    return out


def describe(members: list[dict], convs: dict) -> dict:
    ms = []
    for v in members:
        c = convs.get(v["conv"]) or {}
        s = (c.get("summary") or {})
        pts = next((p.get("points") or [] for p in s.get("people") or [] if p.get("who") == v["label"]), [])
        ms.append({"conv": v["conv"], "label": v["label"], "start": conv_start(v["conv"]), "n": v["n"], "speech_s": v["speech_s"],
                   "clip": v.get("clip"), "clip_start": v.get("clip_start"), "clip_end": v.get("clip_end"), "clip_text": v.get("clip_text"),
                   "title": s.get("title"), "points": pts[:2],
                   "with": [p for p in c.get("participants") or [] if p != v["label"]]})
    ms.sort(key=lambda m: m["start"], reverse=True)
    co = {}
    for m in ms:
        for p in m["with"]:
            if not speakers.ANON.fullmatch(p):
                co[p] = co.get(p, 0) + 1
    return {"count": len(ms), "speech_s": round(sum(m["speech_s"] for m in ms), 1), "last": ms[0]["start"] if ms else None,
            "samples": ms[:: max(1, len(ms) // 4)][:4],  # spread over time: a group that mixes people sounds mixed
            "co": sorted(co, key=lambda k: -co[k])[:4], "members": ms}


@app.get("/api/speakers")
async def speaker_list():
    """Without the member lists (most of the size; the home uplink is slow): GET /api/speakers/{id} has them."""
    bare = lambda g: {k: v for k, v in g.items() if k != "members"}
    return [{**bare(g), "suggested": [bare(x) for x in g["suggested"]]} for g in await asyncio.to_thread(speaker_groups)]


def find_group(gid: str, sid: str | None = None) -> dict:
    g = next((g for g in speaker_groups() if g["id"] == gid), None)
    if g and sid:
        g = next((x for x in g["suggested"] if x["id"] == sid), None)
    if not g:
        raise HTTPException(409, "说话人分组已经变了，刷新页面再试")
    return g


@app.get("/api/speakers/{gid}")
async def speaker_one(gid: str):
    return await asyncio.to_thread(find_group, gid)


def check_name(to: str):
    if not to or to == "S?" or len(to) > 20 or speakers.ANON.fullmatch(to):
        raise HTTPException(400, "bad name")


@app.post("/api/speakers/name")
async def speaker_name(body: dict):
    """Every member voice of a group (or of one of its suggestions) takes the name, in all of its conversations."""
    to = (body.get("name") or "").strip()
    check_name(to)
    done = 0
    for m in (await asyncio.to_thread(find_group, body["group"], body.get("suggestion")))["members"]:
        if m["label"] == to:
            continue
        try:
            await apply_rename(m["conv"], m["label"], to)
        except HTTPException as e:  # the conversation changed since the page loaded
            log.warning("speaker name %s %s: %s", m["conv"], m["label"], e.detail)
            continue
        done += 1
    return {"renamed": done}


@app.post("/api/speakers/detach")
async def speaker_detach(body: dict):
    """A voice that is not this speaker: a named one goes back to a fresh S<n> of its conversation; either way it is
    kept apart from the group from now on."""
    conv, label, group = body["conv"], body["label"], body["group"]
    key = f"{conv}|{label}"
    if not speakers.ANON.fullmatch(label):
        c = find_conversation(conv)
        if not c:
            raise HTTPException(404, "no such conversation")
        used = {int(p[1:]) for p in c["participants"] if re.fullmatch(r"S\d+", p)}
        new = f"S{max(used, default=0) + 1}"
        await apply_rename(conv, label, new)
        key = f"{conv}|{new}"
    db.conn().execute("INSERT OR IGNORE INTO apart VALUES (?, ?)", (key, group))
    _speakers["at"] = 0.0
    return {"key": key}


@app.get("/api/person/{name}")
async def person(name: str):
    """Everything about one named speaker: their conversations newest first with what they said there, the todos they
    own, and what I promised them."""
    convs = [c for c in all_conversations() if name in (c.get("participants") or [])]
    out, co, tags, theirs, mine = [], {}, {}, [], []
    for c in convs:
        s = c["summary"] if (c["summary"] or {}).get("status") == "done" else {}
        pts = next((p.get("points") or [] for p in s.get("people") or [] if p.get("who") == name), [])
        out.append({"id": c["id"], "start": c["start"], "end": c["end"], "title": s.get("title"), "summary": s.get("summary"),
                    "points": pts, "participants": c["participants"], "speech_s": c.get("speech_s")})
        for p in c["participants"]:
            if p != name and not speakers.ANON.fullmatch(p):
                co[p] = co.get(p, 0) + 1
        for t in s.get("tags") or []:
            tags[t] = tags.get(t, 0) + 1
        for t in s.get("todos") or []:
            t = {**t, "conversation": c["id"], "title": s.get("title")}
            if t.get("owner") == name:
                theirs.append(t)
            elif t.get("owner") == "我" and t.get("to") == name:  # to: prompt v4 on; older summaries have none
                mine.append(t)
    if not convs:
        raise HTTPException(404, "no conversation with this name")
    top = lambda d, n: sorted(d, key=lambda k: -d[k])[:n]
    return {"name": name, "count": len(convs), "first": convs[-1]["start"], "last": convs[0]["start"],
            "with": top(co, 6), "tags": top(tags, 8), "theirs": theirs, "mine": mine, "conversations": out}


@app.get("/api/voice-clip/{name}")
async def voice_clip(name: str):
    p = VOICES / "clips" / name
    if not re.fullmatch(r"[\w.-]+\.ogg", name) or not p.is_file():
        raise HTTPException(404, "no such clip")
    return FileResponse(p, media_type="audio/ogg", headers={"Cache-Control": "public, max-age=31536000, immutable"})


async def voices_loop():
    """Voiceprints for the labels of closed live conversations (history ones are uploaded): up to 4 sentences of
    >= 1.5 s per label with >= 3 sentences. A conversation without a recording here is marked and not retried."""
    while True:
        await asyncio.to_thread(speaker_groups)  # warm: the page's first load would otherwise wait seconds
        await asyncio.sleep(300)
        if not state.get("embed"):
            continue
        seen = db.voiced_conversations()
        since = (dt.date.today() - dt.timedelta(days=3)).isoformat()
        for c in all_conversations():
            if c["start"] < since or c["id"] in seen:
                continue
            by = {}
            for r in conversation_rows(c):
                if r.get("speaker") and r["speaker"] != "S?":
                    by.setdefault(r["speaker"], []).append(r)
            out = []
            for label, rs in by.items():
                if len(rs) < 3:
                    continue
                dur = lambda r: (dt.datetime.fromisoformat(r["end"]) - dt.datetime.fromisoformat(r["start"])).total_seconds()
                long = sorted((r for r in rs if dur(r) >= 1.5), key=dur, reverse=True)[:4]
                es = []
                for r in long:
                    try:
                        x = np.frombuffer(await cut(r["start"], min(r["end"], (dt.datetime.fromisoformat(r["start"]) + dt.timedelta(seconds=15)).isoformat()), "s16le", r.get("source")), np.int16)
                    except HTTPException:
                        continue
                    es.append(await asyncio.to_thread(state["embed"], x))
                if es:
                    e = np.mean(es, 0)
                    best = min(long, key=lambda r: abs(dur(r) - 6))
                    out.append({"conv": c["id"], "label": label, "device": "live", "n": len(rs), "speech_s": round(sum(map(dur, rs)), 1),
                                "k": len(es), "e": e / np.linalg.norm(e),
                                "clip_start": best["start"], "clip_end": min(best["end"], (dt.datetime.fromisoformat(best["start"]) + dt.timedelta(seconds=10)).isoformat()),
                                "clip_text": best.get("text")})
            with db.tx() as t:
                db.insert_voices(t, out or [{"conv": c["id"]}])
            _speakers["at"] = 0.0


@app.get("/api/audio")
async def audio_wav(start: str, end: str, device: str | None = None):
    """WAV of [start, end] (+0.2 s each side) cut from the uploaded pieces of `device` (any when not given) covering it."""
    wav = await cut(start, end, "wav", device)
    return Response(wav, media_type="audio/wav")


async def cut(start: str, end: str, fmt: str, device: str | None = None) -> bytes:
    a, b = dt.datetime.fromisoformat(start), dt.datetime.fromisoformat(end)
    out = await asyncio.to_thread(audio.cut, a, b, fmt, device) if 0 < (b - a).total_seconds() <= 120 else None
    if out is None:
        raise HTTPException(404, "no recording covers this time")
    return out


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    if AUTH_TOKEN and not authorized(ws.cookies, ws.headers):
        await ws.close(code=4401)
        return
    await ws.accept()
    clients.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        clients.discard(ws)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", type=Path, default=Path(env("RECORDINGS_DIR")))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8770")))
    args = ap.parse_args()
    missing = [k for k in ("ASR_UPLOAD_BRIDGE_TOKEN", "ASR_SERVICE_BASIC_AUTH") if not os.environ.get(k)]
    if ENGINE == "volc" and not os.environ.get("VOLC_ASR_KEY"):
        missing.append("VOLC_ASR_KEY")
    if ENGINE not in ("volc", "stepfun"):
        raise SystemExit(f"ENGINE must be volc or stepfun, not {ENGINE!r}")
    missing += [k for k in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL") if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"not set: {', '.join(missing)} (see .env.example)")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    args.dir.mkdir(parents=True, exist_ok=True)
    audio.DIR = args.dir
    db.open_db(DATA / "live.db")
    state["commit"] = subprocess.run(["git", "-C", str(HERE), "rev-parse", "--short", "HEAD"],
                                     capture_output=True, text=True).stdout.strip()

    @app.on_event("startup")
    async def start():
        for name, coro in (("pipeline", pipelines()), ("status", status_loop()), ("summary", summary_worker()),
                           ("voices", voices_loop())):
            asyncio.create_task(coro, name=name).add_done_callback(
                lambda t, n=name: log.error("task %s ended: %r", n, t.exception()) if not t.cancelled() else None)

    import uvicorn
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
