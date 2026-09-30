"""Live transcript of the continuous recorder.

    set -a; . .env; set +a; uv run python server.py [--dir <recordings>] [--port 8770] [--from-start]   (variables: .env.example)

Tails the recorder's newest Opus segment, or takes the 20 s pieces the /record page and the app upload, cuts speech with FireRed Stream-VAD,
sends each utterance to StepFun streaming ASR, appends sentences to data/<date>.jsonl and
pushes them to the page at http://127.0.0.1:<port>/.
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
import uuid
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse, Response

import summary
from conversations import Grouper

HERE = Path(__file__).resolve().parent
def env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise SystemExit(f"{name} is not set (see .env.example)")
    return v


VAD_MODEL = Path(env("VAD_MODEL_DIR"))            # FireRedVAD Stream-VAD checkpoint directory
DATA = Path(env("LIVE_DATA"))                     # sentences, final rows, conversations, owner.npz
ASR_URL = os.environ.get("STEPFUN_SSE_URL", "")  # per-utterance SSE endpoint; only needed when FIRST_LAYER=on or TEXT_SOURCE=sse
ASR_MODEL = os.environ.get("STEPFUN_SSE_MODEL", "stepaudio-2.5-asr")
SR = 16000
FRAME_LEN, FRAME_SHIFT = 400, 160          # FireRed fbank: 25 ms window, 10 ms shift
MERGE_GAP_S = 0.6                          # utterances closer than this go to ASR together
MAX_UTTERANCE_S = 15.0
ROTATE_IDLE_S = 5.0                        # file stopped growing and a newer one exists -> switch
REFINE_EVERY_S = int(os.environ.get("REFINE_EVERY_S", "30"))  # finalization cadence
FIRST_LAYER = os.environ.get("FIRST_LAYER", "on") != "off"   # off: no per-utterance ASR; text only comes from the finalization pass
REFINE_WINDOW_S = int(os.environ.get("REFINE_WINDOW_S", "240"))    # most audio one pass takes (catching up a backlog)
REFINE_MARGIN_S = 1.0                      # audio re-read before the last finalized time: the engine's sentence starts come a little late
ENGINE = os.environ.get("ENGINE", "volc")  # second-layer engine: volc (seed-asr-2.0 streaming API) | stepfun (file-mode service)
BUFFER_S = 900                             # audio kept for refinement; a stalled refiner catches up from here
TURN_GAP_S = float(os.environ.get("TURN_GAP_S", "1.0"))  # same speaker, closer than this: one final row; 0 keeps the engine's sentences as rows
EDGE_S = 3.0                               # rows touching the window edges may be cut: left to the next pass
OWNER_MIN = 0.25                           # best cluster must also score this against the owner voiceprint
OWNER_SURE = float(os.environ.get("OWNER_SURE", "0.5"))  # any cluster this close to the owner is me too (clustering often splits me in 2-5)
ASR_PAD_S = 0.4                            # context added around a diarized turn before ASR
MIN_SPEAKER_S = 2.0                        # a sentence shorter than this opens no new speaker id (noise, "嗯")
LINK_MIN = float(os.environ.get("LINK_MIN", "0.55"))   # cosine to link a window's cluster to a known speaker
# Per input device: lapel one-on-one wants coarse clusters and easy linking (0.65 split the same person
# into 3 ids); the far-field MacBook meeting wants finer clusters and stricter linking (0.55 merged
# two colleagues). Case 09-28 vs case 05-26, see the 2026-09-29 iteration record.
DEVICE_PARAMS = {"Wireless Mic Rx": {"diar": 1.0, "link": 0.55}, "MacBook Pro麦克风": {"diar": 0.8, "link": 0.65}}
OWNER_DEFAULT_DEVICE = os.environ.get("OWNER_DEFAULT_DEVICE", "")  # voiceprint used for devices owner.py has not seen (a new phone)
DIARIZER = os.environ.get("DIARIZER", "cloud")   # cloud: speakers from the file-mode ASR result; local: sherpa-onnx on this machine
TEXT_SOURCE = os.environ.get("TEXT_SOURCE", "file")  # local diarizer's turn text: file (one StepFun file call per pass, words split by turn) | sse (one call per turn)
SEG_MODEL = Path(os.environ.get("SEG_MODEL", ""))  # sherpa-onnx pyannote segmentation model; only for DIARIZER=local
EMB_MODEL = Path(env("EMB_MODEL"))                # sherpa-onnx speaker embedding model (CAM++ by default)
BRIDGE = env("ASR_UPLOAD_BRIDGE_URL")             # returns {upload_url, download_url} for a file name
DIARIZE = env("ASR_SERVICE_URL")                   # file-mode ASR service: POST /audio/transcriptions, GET /jobs/{id}
AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "")      # empty: no auth (loopback only); set when a proxy exposes the server

log = logging.getLogger("live")


def file_start(path: Path) -> dt.datetime:
    return dt.datetime.strptime(path.name[:19], "%Y-%m-%d_%H-%M-%S")


def current_device(directory: Path) -> str:
    """The recorder writes the sidecar when a segment closes, so take the latest closed one."""
    side = max(directory.glob("20*.json"), key=lambda p: p.name)
    return json.loads(side.read_text())["device"]


SESSION_STALE_S = 45  # chunks arrive every 20 s; a session without one for this long has ended
FORMATS = {".ogg": "ogg"}   # the recorder's Opus files (tailed while being written)
CHUNK_MAX_S = 30            # an uploaded piece is at most this long (the clients cut every 20 s)


def recordings(directory: Path) -> list[Path]:
    return [p for p in directory.glob("20*") if p.suffix in FORMATS]


def newest(directory: Path) -> Path | None:
    return max(recordings(directory), key=lambda p: p.name, default=None)


class Follower:
    """Yields (absolute start time, int16 samples) from the segment being written, across rotations.

    ffmpeg's ogg demuxer seeks to the end of a regular file for the duration, which never
    returns on a growing file, so the bytes go in through a pipe instead.
    """

    def __init__(self, directory: Path, from_start: bool, resume: dt.datetime | None = None):
        self.dir, self.from_start, self.resume = directory, from_start, resume
        self.path: Path | None = None
        self.audio_end: dt.datetime | None = None

    async def chunks(self):
        skip_until = None if self.from_start else (self.resume or dt.datetime.now())
        while True:
            path = newest(self.dir)
            if path is None or path == self.path:
                await asyncio.sleep(1)  # nothing recorded yet
                continue
            self.path = path
            async for t, x in self._decode(path):
                if skip_until and t + dt.timedelta(seconds=len(x) / SR) <= skip_until:
                    continue
                self.audio_end = t + dt.timedelta(seconds=len(x) / SR)
                yield t, x
            skip_until = None  # only the file found at startup holds already-transcribed audio

    async def _decode(self, path: Path):
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-v", "error", "-f", FORMATS[path.suffix], "-i", "pipe:0", "-f", "s16le", "-ac", "1",
            "-ar", str(SR), "-flush_packets", "1", "pipe:1",
            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        feeder = asyncio.create_task(self._feed(path, proc))
        start, done, rest = file_start(path), 0, b""
        while chunk := await proc.stdout.read(SR):  # up to 0.5 s
            chunk = rest + chunk
            rest = chunk[len(chunk) // 2 * 2:]
            x = np.frombuffer(chunk[:len(chunk) // 2 * 2], np.int16)
            yield start + dt.timedelta(seconds=done / SR), x
            done += len(x)
        await feeder
        await proc.wait()
        log.info("finished %s after %.1f s of audio", path.name, done / SR)

    async def _feed(self, path: Path, proc):
        idle = 0.0
        with open(path, "rb") as f:
            while True:
                b = f.read(1 << 16)
                if b:
                    idle = 0.0
                    proc.stdin.write(b)
                    await proc.stdin.drain()
                    continue
                idle += 0.2
                if idle >= ROTATE_IDLE_S and newest(self.dir) != path:
                    break
                await asyncio.sleep(0.2)
        proc.stdin.close()


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


class Asr:
    """StepFun ASR over Step Plan's SSE endpoint, one request per utterance.

    Stateless, unlike the realtime WebSocket, which acks a no-speech commit and then never
    answers. SSE ends in ~0.3 s with an empty transcript instead; a 12 s utterance takes ~0.45 s.
    """

    def __init__(self, key: str):
        self.key = key

    def _post(self, x: np.ndarray) -> str:
        import urllib.request
        body = json.dumps({"audio": {"data": base64.b64encode(x.tobytes()).decode(), "input": {
            "transcription": {"model": ASR_MODEL, "language": "zh", "enable_itn": True},
            "format": {"type": "pcm", "codec": "pcm_s16le", "rate": SR, "bits": 16, "channel": 1}}}}).encode()
        req = urllib.request.Request(ASR_URL, body, {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json",
                                                     "Accept": "text/event-stream", "User-Agent": "curl/8"})
        text = ""
        with urllib.request.urlopen(req, timeout=30) as r:
            for line in r:
                if line.startswith(b"data:"):
                    m = json.loads(line[5:])
                    if m.get("type") == "transcript.text.done":
                        text = m.get("text", "")
                    elif "error" in m:
                        raise RuntimeError(m)
        return text

    async def transcribe(self, x: np.ndarray) -> str:
        """Transcript, or '' when StepFun heard no speech. Backs off on errors (429 when several
        clients share the key), gives up after 5 tries."""
        for attempt in range(5):
            try:
                return await asyncio.to_thread(self._post, x)
            except Exception as e:
                log.warning("ASR attempt %d failed: %r", attempt + 1, e)
                if attempt == 4:
                    raise
                await asyncio.sleep(2 ** attempt)


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


def words_to_turns(turns: list[dict], file_segs: list[dict], reach_ms: int = 1000) -> int:
    """Give each diarized turn the text of the file-mode ASR words whose midpoint falls in it (or within
    reach_ms of it). One file call per pass replaces one SSE call per turn and reads whole sentences,
    so the text is far better than per-turn ASR; returns the number of words no turn could take."""
    words = sorted((w for g in file_segs for w in (g.get("words") or []) if w.get("text")), key=lambda w: w["start_ms"])
    turns = sorted(turns, key=lambda g: g["start_ms"])
    parts: dict[int, list[str]] = {i: [] for i in range(len(turns))}
    lost = 0
    for w in words:
        mid = (w["start_ms"] + w["end_ms"]) // 2
        best = min(range(len(turns)), key=lambda i: max(0, turns[i]["start_ms"] - mid, mid - turns[i]["end_ms"]), default=None)
        if best is None or max(0, turns[best]["start_ms"] - mid, mid - turns[best]["end_ms"]) > reach_ms:
            lost += 1
            continue
        parts[best].append(w["text"])
    for i, g in enumerate(turns):
        g["text"] = "".join(parts[i])
    return lost


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


class Speakers:
    """Labels each StepFun speaker of a window 我 or 他人: the cluster whose mean voiceprint is closest
    to the owner's. Per-cluster, not per-sentence: on a lapel mic in a one-on-one, single sentences
    of the other person score as high as mine (09-09: 57 % per sentence vs 91 % per cluster)."""

    def __init__(self):
        import sherpa_onnx
        self.ex = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(EMB_MODEL), num_threads=2))
        self.owner = dict(np.load(DATA / "owner.npz"))
        self.warned: set[str] = set()

    def embed(self, x: np.ndarray) -> np.ndarray:
        s = self.ex.create_stream()
        s.accept_waveform(SR, x.astype(np.float32) / 32768)
        s.input_finished()
        e = np.array(self.ex.compute(s))
        return e / np.linalg.norm(e)

    def owner_for(self, device: str) -> np.ndarray:
        owner = self.owner.get(device)
        if owner is None and OWNER_DEFAULT_DEVICE in self.owner:
            owner = self.owner[OWNER_DEFAULT_DEVICE]
            if device not in self.warned:
                self.warned.add(device)
                log.warning("no owner voiceprint for %r, scoring against %r", device, OWNER_DEFAULT_DEVICE)
        if owner is None:
            raise RuntimeError(f"no owner voiceprint for {device!r}; run owner.py")
        return owner

    def label(self, segs: list[dict], x: np.ndarray, device: str) -> tuple[dict, dict, dict, dict]:
        """(cluster -> 我/他人, cluster -> owner score, cluster -> unit mean voiceprint, cluster -> seconds)."""
        owner = self.owner_for(device)
        mean, secs = {}, {}
        for g in segs:
            a, b = g["start_ms"] / 1000, min(g["end_ms"] / 1000, g["start_ms"] / 1000 + 15)
            if b - a >= 1.0:
                mean[g["speaker"]] = mean.get(g["speaker"], 0) + self.embed(x[int(a * SR):int(b * SR)]) * (b - a)
                secs[g["speaker"]] = secs.get(g["speaker"], 0.0) + (b - a)
        mean = {k: v / np.linalg.norm(v) for k, v in mean.items()}
        score = {k: float(v @ owner) for k, v in mean.items()}
        me = {k for k, v in score.items() if v >= OWNER_SURE}
        if not me and score and max(score.values()) >= OWNER_MIN:
            me = {max(score, key=score.get)}
        return {k: "我" if k in me else "他人" for k in {g["speaker"] for g in segs}}, score, mean, secs


class Registry:
    """Conversation-local speaker ids. A window's cluster mean voiceprint is matched to the running
    centroid of each known speaker (cosine >= LINK_MIN, best wins) or opens the next S<n>. Omi's
    sliding-centroid idea; ids reset when a conversation closes."""

    def __init__(self):
        self.c: dict[str, tuple[np.ndarray, float]] = {}

    def reset(self):
        self.c = {}

    def best(self, vec: np.ndarray) -> tuple[str | None, float]:
        return max(((k, float(v @ vec)) for k, (v, _) in self.c.items()), key=lambda t: t[1], default=(None, 0.0))

    def assign(self, vec: np.ndarray, weight: float, link_min: float = LINK_MIN) -> tuple[str, float]:
        best = self.best(vec)
        if best[0] and best[1] >= link_min:
            k, (v, w) = best[0], self.c[best[0]]
            nv = v * w + vec * weight
            self.c[k] = (nv / np.linalg.norm(nv), w + weight)
            return k, round(best[1], 3)
        k = f"S{len(self.c) + 1}"
        self.c[k] = (vec, weight)
        return k, 1.0  # a new id is certain by definition; how sure it is *not* someone known is 1 - best


class LocalDiarizer:
    """sherpa-onnx pyannote-segmentation-3.0 + CAM++ clustering, RTF about 0.06 on the M2 Pro."""

    def __init__(self, threshold=float(os.environ.get("DIAR_THRESHOLD", "1.0"))):  # 0.5 splits a 2-person chat into 20+ clusters
        import sherpa_onnx
        cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(SEG_MODEL)), num_threads=4),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(EMB_MODEL), num_threads=4),
            clustering=sherpa_onnx.FastClusteringConfig(num_clusters=-1, threshold=threshold),
            min_duration_on=float(os.environ.get("DIAR_MIN_ON", "0.3")), min_duration_off=float(os.environ.get("DIAR_MIN_OFF", "0.5")))
        self.sd = sherpa_onnx.OfflineSpeakerDiarization(cfg)

    def __call__(self, x: np.ndarray) -> list[dict]:
        r = self.sd.process(x.astype(np.float32) / 32768).sort_by_start_time()
        return [dict(start_ms=int(s.start * 1000), end_ms=int(s.end * 1000), speaker=f"S{s.speaker}", text="") for s in r]


app = FastAPI()
clients: set[WebSocket] = set()
state = {"follower": None, "chunks": None, "audio_q": None, "end_at": None, "done_to": None, "audio_start": None, "latency": [], "events": [], "asr_ms": None, "errors": 0, "no_speech": 0, "commit": "", "refine": None, "device": None,
         "conversation": None, "last_speech": None}


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


def sentences_path(day: dt.date) -> Path:
    return DATA / f"{day.isoformat()}.jsonl"


def final_path(day: dt.date) -> Path:
    return DATA / f"{day.isoformat()}.final.jsonl"  # each line replaces the final rows in [from, to)


def conversations_path(day: dt.date) -> Path:
    return DATA / f"{day.isoformat()}.conversations.jsonl"


PEOPLE = DATA / "people.json"  # {name: {"n": sentences, "e": mean voiceprint}}, from renames on the conversation page


def people_bank() -> dict[str, np.ndarray]:
    return {k: np.array(v["e"]) for k, v in json.loads(PEOPLE.read_text()).items()} if PEOPLE.exists() else {}


def summary_path(conv_id: str) -> Path:
    return DATA / "summaries" / f"{conv_id}.json"


def find_conversation(conv_id: str) -> dict | None:
    """Ids are "c" + the start time as YYYYMMDDhhmmss, so the day file is known from the id."""
    p = conversations_path(dt.date(int(conv_id[1:5]), int(conv_id[5:7]), int(conv_id[7:9])))
    return next((c for c in map(json.loads, open(p)) if c["id"] == conv_id), None) if p.exists() else None


def conversation_rows(c: dict, cache: dict | None = None) -> list[dict]:
    """`cache` keeps each day's rows across calls (a search over many conversations of the same day)."""
    cache = {} if cache is None else cache
    a, b = dt.date.fromisoformat(c["start"][:10]), dt.date.fromisoformat(c["end"][:10])
    days = [a + dt.timedelta(days=i) for i in range((b - a).days + 1)]
    for d in days:
        if d not in cache:
            cache[d] = day_rows(d)
    return [r for d in days for r in cache[d] if c["start"] <= r["start"] <= c["end"]]


summary_q: asyncio.Queue = asyncio.Queue()


def queue_summary(c: dict, keep: bool = False):
    """keep: the current summary stays on the page until the new one is written (an upgrade to a newer prompt)."""
    summary_path(c["id"]).parent.mkdir(exist_ok=True)
    if not keep:
        summary_path(c["id"]).write_text(json.dumps({"conversation": c["id"], "status": "pending", "error": None}))
    summary_q.put_nowait(c)


def all_conversations() -> list[dict]:
    """Every closed conversation with its summary, newest first."""
    # ponytail: reads every file per call; fine for years of conversations, an index when it is not
    cs = [{**c, "summary": load_summary(c["id"])} for p in DATA.glob("*.conversations.jsonl") for c in map(json.loads, open(p))]
    return sorted(cs, key=lambda c: c["start"], reverse=True)


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


SUMMARY_WORKERS = int(os.environ.get("SUMMARY_WORKERS", "6"))  # the gateway answered 8 parallel calls without 429; 10 got 429s


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
        old = load_summary(c["id"])
        if res["status"] == "failed" and old and old["status"] == "done":  # a failed upgrade keeps the old summary
            continue
        summary_path(c["id"]).write_text(json.dumps(res, ensure_ascii=False, indent=1))
        note(f"对话 {c['start'][11:19]} 的摘要" + (f"：{res['title']}" if res["status"] == "done" else f"生成失败：{res['error']}"),
             "state" if res["status"] == "done" else "error")
        await broadcast({"type": "summary", **res})


def load_summary(conv_id: str) -> dict | None:
    p = summary_path(conv_id)
    return json.loads(p.read_text()) if p.exists() else None


def apply_final(records) -> list[dict]:
    rows = []
    for rec in records:
        rows = [r for r in rows if not rec["from"] <= r["start"] < rec["to"]] + rec["rows"]
    return sorted(rows, key=lambda r: r["start"])


def last_transcribed(day: dt.date) -> dt.datetime | None:
    """End of the last first-layer row written today: where a restarted service resumes."""
    ends = []
    if sentences_path(day).exists():
        ends += [json.loads(l)["end"] for l in open(sentences_path(day))]
    if final_path(day).exists():  # final-only mode writes no first-layer rows
        ends += [json.loads(l)["to"] for l in open(final_path(day))]
    return dt.datetime.fromisoformat(max(ends)) if ends else None


async def pipeline(directory: Path, from_start: bool, key: str):
    resume = last_transcribed(dt.date.today())
    if resume:
        log.info("resuming after the last transcribed row at %s", resume.time())
    follower, seg, asr = Follower(directory, from_start, resume), Segmenter(), Asr(key)
    state["follower"] = follower
    queue: asyncio.Queue = asyncio.Queue()
    recent: list[dict] = []

    async def transcribe_worker():
        while True:
            a, b, x = await queue.get()
            t0 = time.perf_counter()
            try:
                text, words, error = await asr.transcribe(x), [], None
                if not text:
                    state["no_speech"] += 1
                    log.info("no speech in %s-%s (%.2f s)", a.time(), b.time(), len(x) / SR)
                    continue
            except Exception as e:
                text, words, error = "", [], str(e)
                state["errors"] += 1
            state["asr_ms"] = round((time.perf_counter() - t0) * 1000)
            row = dict(id=a.strftime("%Y%m%d%H%M%S%f")[:17], start=a.isoformat(timespec="milliseconds"),
                       end=b.isoformat(timespec="milliseconds"), text=text, speaker=None, state="pending",
                       source=state["device"], words=words,
                       asr_ms=state["asr_ms"], shown_at=dt.datetime.now().isoformat(timespec="milliseconds"),
                       error=error)
            DATA.mkdir(exist_ok=True)
            with open(sentences_path(a.date()), "a") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            await broadcast({"type": "sentence", **row})
            recent.append(row)
            del recent[:-300]

    async def refine_loop():
        from diarist import Diarist, load_embedder
        embed, dia, grouper = load_embedder(), Diarist(dict(np.load(DATA / "owner-redimnet2.npz")), people=people_bank()), Grouper(path=DATA / "grouper.json")
        state["embed"], state["dia"] = embed, dia  # a rename on the conversation page teaches the diarist a name
        if grouper.open:
            log.info("continuing the open conversation from %s", grouper.open["start"])
            note(f"服务启动，接着上次打开的对话 {grouper.open['start'][11:19]}", "state")
        else:
            note("服务启动", "state")
        locals_: dict[float, LocalDiarizer] = {}  # one per clustering threshold, built on first use

        def local_for(device: str) -> LocalDiarizer:
            th = DEVICE_PARAMS.get(device, {}).get("diar", float(os.environ.get("DIAR_THRESHOLD", "1.0")))
            if th not in locals_:
                locals_[th] = LocalDiarizer(th)
            return locals_[th]
        local = DIARIZER == "local"
        sem = asyncio.Semaphore(4)
        done_to = None  # wall time up to which rows are final

        async def transcribe_turn(g, x):
            # Diarizer boundaries clip the first and last syllable; without this margin StepFun hears
            # no speech in many short turns (case 09-28: coverage 84 % -> 97 %, speaker accuracy 76 % -> 89 %)
            a, b = int((g["start_ms"] / 1000 - ASR_PAD_S) * SR), int((g["end_ms"] / 1000 + ASR_PAD_S) * SR)
            async with sem:
                g["text"] = await asr.transcribe(x[max(0, a):b])

        def close(c: dict):
            dia.reset()
            rec = {"id": "c" + c["start"].replace("-", "").replace(":", "").replace("T", "")[:14], "start": c["start"], "end": c["end"],
                   "state": "closed", "participants": c["participants"], "sentences": [r["id"] for r in c["sentences"]],
                   "speech_s": round(c["speech_s"], 1)}
            with open(conversations_path(dt.datetime.fromisoformat(c["start"]).date()), "a") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            state["conversation"] = None
            queue_summary(rec)
            log.info("conversation closed %s", {k: v for k, v in rec.items() if k != "sentences"})
            note(f"对话结束：{rec['start'][11:19]} 到 {rec['end'][11:19]}，{len(rec['sentences'])} 句，参与 {'、'.join(rec['participants']) or '—'}", "state")
            return {"type": "conversation", **rec}

        async def refine_pass() -> bool:
            """One pass over [done_to - margin, + window]; True when a backlog remains."""
            nonlocal done_to
            if not seg.anchors:
                return False  # no audio yet
            t_run = time.perf_counter()
            # from where the last pass stopped, or from the oldest buffered audio (a client may burst a backlog at startup)
            t0, x = seg.window(REFINE_WINDOW_S, done_to - dt.timedelta(seconds=REFINE_MARGIN_S) if done_to else seg.time_at(seg.audio_base))
            a0 = seg.sample_at(t0)
            at = lambda s: seg.time_at(a0 + int(s * SR))  # the window may span a session boundary, where the clock jumps
            # a first pass may start mid-sentence: skip its edge; later passes start at a sentence the last one left open
            since = EDGE_S if done_to is None else max(0.0, (seg.sample_at(done_to) - a0) / SR)
            if len(x) / SR - since < EDGE_S + 10:
                return False
            hi = at(len(x) / SR - EDGE_S)
            if state["last_speech"] is None or state["last_speech"] <= (done_to or t0):
                # silence: nothing to diarize, but the clock must advance so a conversation can close on time
                done_to = hi
                for c in grouper.advance(hi):
                    await broadcast(close(c))
                state["done_to"] = hi
                return (seg.time_at(seg.samples) - hi).total_seconds() > REFINE_WINDOW_S - REFINE_MARGIN_S
            was_open = bool(grouper.open)
            m0 = dia.mark()
            try:
                device = state["device"] or current_device(follower.dir)
                segs = await asyncio.to_thread(local_for(device), x) if local else await asyncio.to_thread(engine_segs, x)
                segs = clip_turns(segs, since)
                engine = None
                if local and TEXT_SOURCE == "file":  # one file-mode call for the window; its words go to the local turns
                    file_segs = await asyncio.to_thread(diarize, x)
                    engine = file_segs[0]["engine"] if file_segs else None
                    lost = words_to_turns(segs, file_segs)
                    if lost:
                        log.info("%d words fell outside every diarized turn", lost)
                elif not local:
                    engine = segs[0]["engine"] if segs else None
                elif local:  # turns are only cut, not transcribed: ASR the ones this pass will finalize
                    await asyncio.gather(*[transcribe_turn(g, x) for g in segs if g["end_ms"] / 1000 > since])
                marks = await asyncio.to_thread(label_sentences, dia, embed, segs, x)
            except Exception as e:
                dia.rollback(m0)
                log.warning("refine failed: %r", e)
                note(f"这一轮引擎调用失败：{str(e)[:120]}，下一轮重试", "error")
                state["refine"] = {"error": str(e)[:200]}
                return False
            labels = {g["speaker"]: g["speaker"] for g in segs}
            by_label = {}
            for g in segs:  # final_rows keys confidence by label
                by_label[g["speaker"]] = max(by_label.get(g["speaker"], 0.0), g["confidence"])
            lo, hi, rows = final_rows(segs, labels, t0, len(x) / SR, by_label, since, at)
            kept = [m for g, m in zip(segs, marks) if m and at(g["start_ms"] / 1000) < hi]  # sentences left open come back next pass
            dia.rollback(kept[-1] if kept else m0)
            with open(DATA / f"{lo.date()}.passes.jsonl", "a") as f:  # every pass's raw engine output, for tracing lost rows
                f.write(json.dumps({"at": dt.datetime.now().isoformat(timespec="seconds"), "t0": t0.isoformat(timespec="milliseconds"),
                                    "since": since, "from": lo.isoformat(timespec="milliseconds"), "to": hi.isoformat(timespec="milliseconds"),
                                    "segs": segs},  # each sentence carries engine_speaker and its cluster's owner score (confidence)
                                   ensure_ascii=False) + "\n")
            for r in rows:
                r["source"], r["engine"] = device, engine or "stepfun-sse"
            lo_s, hi_s = lo.isoformat(timespec="milliseconds"), hi.isoformat(timespec="milliseconds")
            for p in recent:  # first-layer text the second pass produced nothing for: keep it, speaker unknown
                if lo_s <= p["start"] < hi_s and p.get("text") and not any(r["start"] < p["end"] and p["start"] < r["end"] for r in rows):
                    rows.append({**p, "speaker": None, "speaker_confidence": None, "state": "final", "words": None})
                    log.info("kept first-layer row %s without a diarized turn", p["start"][11:19])
            rows.sort(key=lambda r: r["start"])
            shown = dt.datetime.now()
            for r in rows:  # end of speech -> on the page: the latency the person sees
                r["shown_at"] = shown.isoformat(timespec="milliseconds")
                state["latency"].append(round((shown - dt.datetime.fromisoformat(r["end"])).total_seconds(), 1))
            del state["latency"][:-50]
            rec = {"from": lo.isoformat(timespec="milliseconds"), "to": hi.isoformat(timespec="milliseconds"), "rows": rows}
            with open(final_path(lo.date()), "a") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            await broadcast({"type": "final", **rec})
            done_to = hi
            closed = []
            for r in rows:
                closed += grouper.sentence(r)
            closed += grouper.advance(hi)
            state["done_to"] = hi
            for c in closed:
                await broadcast(close(c))
            if grouper.open:
                o = grouper.open
                state["conversation"] = {"start": o["start"], "end": o["end"], "sentences": len(o["sentences"])}
            state["refine"] = {"at": dt.datetime.now().isoformat(timespec="seconds"), "ms": round((time.perf_counter() - t_run) * 1000),
                               "speakers": by_label, "changed": len(rows),
                               "backlog_s": round((seg.time_at(seg.samples) - hi).total_seconds())}
            log.info("refine %s: %s", device, state["refine"])
            note(f"定稿一轮：{device} 送 {len(x) / SR:.0f} s（含 {since:.0f} s 上下文）给 {engine or ENGINE}，"
                 f"{state['refine']['ms'] / 1000:.1f} s，{len(rows)} 句，定稿到 {hi.strftime('%H:%M:%S')}，积压 {state['refine']['backlog_s']} s")
            if grouper.open and not was_open:
                note(f"对话开始：{grouper.open['start'][11:19]}", "state")
            return state["refine"]["backlog_s"] > REFINE_WINDOW_S - REFINE_MARGIN_S

        last_end = None
        while True:
            await asyncio.sleep(REFINE_EVERY_S)
            c = state["chunks"]
            if c and time.time() - c["at"] < SESSION_STALE_S:
                state["device"] = c["device"]
            else:
                try:
                    state["device"] = current_device(follower.dir)
                except Exception:
                    pass
            while await refine_pass():  # timer-driven: silence also advances, so conversations close on time
                pass
            closed = []
            if state["end_at"] and follower.audio_end == last_end:  # the recorder stopped and its last audio is processed
                closed += grouper.end(state["end_at"])
                state["end_at"] = None
            last_end = follower.audio_end
            idle = grouper.tick()  # the safety net: nothing at all for IDLE_S (the recorder died without an end)
            if idle:
                note("20 分钟没有任何事件，按兜底规则关闭对话", "warn")
            closed += idle
            for c in closed:
                await broadcast(close(c))
            if not grouper.open:
                state["conversation"] = None

    def watched(coro, name):
        """A task whose death is logged: an unhandled exception would otherwise stop the pipeline silently."""
        t = asyncio.create_task(coro, name=name)
        t.add_done_callback(lambda t: (log.error("task %s ended: %r", name, t.exception()),
                                       note(f"内部任务 {name} 退出：{t.exception()!r}"[:160], "error")) if not t.cancelled() else None)
        return t

    watched(transcribe_worker(), "transcribe")
    watched(refine_loop(), "refine")
    audio_q: asyncio.Queue = asyncio.Queue()
    state["audio_q"] = audio_q
    state["seg"] = seg

    async def follow():
        async for t, x in follower.chunks():
            await audio_q.put((t, x))

    watched(follow(), "follow")
    while True:
        t, x = await audio_q.get()  # from the followed file, or /api/chunks
        try:
            utts = seg.push(t, x)
        except Exception:
            log.exception("segmenter failed on %.1f s at %s", len(x) / SR, t.time())
            continue
        for u in utts:
            state["last_speech"] = u[1]  # the refiner skips diarizing stretches with no VAD speech
            if FIRST_LAYER:
                await queue.put(u)


async def status_loop():
    while True:
        f = state["follower"]
        end = f.audio_end if f else None
        await broadcast({"type": "status", "file": f.path.name if f and f.path else None,
                         "audio_end": end.isoformat(timespec="seconds") if end else None,
                         "behind_s": round((dt.datetime.now() - end).total_seconds(), 1) if end else None,
                         "asr_ms": state["asr_ms"], "errors": state["errors"],
                         "no_speech": state["no_speech"], "commit": state["commit"], "refine": state["refine"],
                         "conversation": state["conversation"], "diarizer": DIARIZER,
                         "latency_s": sorted(state["latency"])[len(state["latency"]) // 2] if state["latency"] else None,
                         "mode": f"{'two-layer' if FIRST_LAYER else 'final-only'}/{ENGINE}/{REFINE_EVERY_S}s/per-sentence"})
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


@app.get("/")
async def index():
    return FileResponse(HERE / "static/index.html", headers=NO_CACHE)


@app.get("/conversations")
@app.get("/c/{conv_id}")
async def conversations_page(conv_id: str = ""):
    return FileResponse(HERE / "static/conversations.html", headers=NO_CACHE)


@app.get("/app.apk")
async def apk():
    """The Android client, for installing from the phone's browser."""
    p = DATA / "live-record.apk"
    if not p.exists():
        raise HTTPException(404, "no apk uploaded")
    return FileResponse(p, media_type="application/vnd.android.package-archive", filename="live-record.apk")


@app.get("/record")
async def record_page():
    return FileResponse(HERE / "static/record.html", headers=NO_CACHE)


def busy(device: str):
    """One stream at a time: a second device would silently take the pipeline over."""
    c = state["chunks"]
    if c and time.time() - c["at"] < SESSION_STALE_S and c["device"] != device:
        note(f"拒绝 {device} 开录：{c['device']} 正在录音", "warn")
        raise HTTPException(409, f"{c['device']} 正在录音，服务端同一时刻只收一路")


@app.post("/api/chunks")
async def chunk(request: Request, device: str, start: int):
    """One self-contained audio file (the app's 20 s Opus pieces), `start` its device time in unix ms;
    it goes straight into the pipeline, anchored at that time, so gaps between pieces do not drift."""
    busy(device)
    f = state["follower"]
    state["audio_start"] = state["audio_start"] or dt.datetime.fromtimestamp(start / 1000)
    behind = state["done_to"] or state["audio_start"]
    if f and f.audio_end and (f.audio_end - behind).total_seconds() > BUFFER_S - 300:
        # a client catching up after an outage would push audio out of the buffer before it is finalized
        note(f"积压 {(f.audio_end - behind).total_seconds():.0f} s，让 {device} 等 20 s 再传", "warn")
        raise HTTPException(429, "服务端积压中，稍后再传", headers={"Retry-After": "20"})
    body = await request.body()
    pcm = await asyncio.to_thread(subprocess.run, ["ffmpeg", "-v", "error", "-i", "pipe:0", "-f", "s16le", "-ac", "1",
                                                   "-ar", str(SR), "pipe:1"], input=body, capture_output=True)
    if pcm.returncode or len(pcm.stdout) < 2 * SR // 10:
        raise HTTPException(400, f"could not decode: {pcm.stderr.decode()[-200:]}")
    x = np.frombuffer(pcm.stdout[:len(pcm.stdout) // 2 * 2], np.int16)
    t = dt.datetime.fromtimestamp(start / 1000)
    ext = {"audio/ogg": "ogg", "audio/webm": "webm"}.get(request.headers.get("content-type", "").split(";")[0], "bin")
    d = state["dir"] / "chunks" / re.sub(r"[^\w.-]", "_", device)  # kept for playback (/api/audio)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{start}.{ext}").write_bytes(body)
    state["chunks"] = {"device": device, "at": time.time()}
    note(f"收到 {device} 的块 {len(x) / SR:.1f} s，起于 {t.strftime('%H:%M:%S')}")
    if state["follower"]:
        state["follower"].audio_end = t + dt.timedelta(seconds=len(x) / SR)
    await state["audio_q"].put((t, x))
    return {"seconds": round(len(x) / SR, 2)}


@app.post("/api/chunks/end")
async def chunk_end(device: str, t: int):
    """The recorder stopped at device time t (unix ms): the open conversation closes once its audio is processed."""
    state["end_at"] = dt.datetime.fromtimestamp(t / 1000)
    log.info("%s stopped recording at %s", device, state["end_at"].time())
    note(f"{device} 停止录音（{state['end_at'].strftime('%H:%M:%S')}），处理完剩余音频后关闭对话")
    return {"ok": True}


@app.get("/api/events")
async def events():
    return state["events"]


@app.get("/api/status")
async def status():
    """What the page's status line shows, plus the pipeline's clocks, for diagnosis."""
    f, seg = state["follower"], state.get("seg")
    return {"audio_end": f.audio_end.isoformat(timespec="seconds") if f and f.audio_end else None,
            "audio_seconds": round(seg.samples / SR, 1) if seg else None,
            "last_speech": state["last_speech"].isoformat(timespec="seconds") if state["last_speech"] else None,
            "done_to": state["done_to"].isoformat(timespec="seconds") if state["done_to"] else None,
            "chunks": state["chunks"], "refine": state["refine"], "conversation": state["conversation"],
            "latency_s": sorted(state["latency"])[len(state["latency"]) // 2] if state["latency"] else None,
            "errors": state["errors"], "engine": ENGINE, "commit": state["commit"]}


@app.get("/api/sentences")
async def sentences(date: str | None = None):
    return day_rows(dt.date.fromisoformat(date) if date else dt.date.today())


def day_rows(day: dt.date) -> list[dict]:
    p, fp = sentences_path(day), final_path(day)
    recs = [json.loads(l) for l in open(fp)] if fp.exists() else []
    final = apply_final(recs)
    # a pending row stays until a finalized stretch covers it; "everything before the last final row"
    # hid sentences from stretches that were never finalized (restarts, skipped windows)
    live = [r for r in map(json.loads, open(p)) if not any(rec["from"] <= r["start"] < rec["to"] for rec in recs)] if p.exists() else []
    return sorted(final + live, key=lambda r: r["start"])


@app.get("/api/conversations")
async def conversations(date: str | None = None):
    day = dt.date.fromisoformat(date) if date else dt.date.today()
    p = conversations_path(day)
    closed = [{**c, "summary": load_summary(c["id"])} for c in map(json.loads, open(p))] if p.exists() else []
    return closed + ([{**state["conversation"], "state": "open"}] if state["conversation"] else [])


@app.get("/api/days")
async def days():
    """Days that have sentences, newest first."""
    return sorted({p.name[:10] for p in DATA.glob("????-??-??.*jsonl")}, reverse=True)


@app.get("/api/search")
async def search(q: str = "", tag: str = "", kind: str = "", before: str = "", limit: int = 30):
    """Closed conversations newest first, filtered by a tag and by words in the title, summary, tags or
    todos (and in the transcript when the summary does not match); `before` is the start of the last one
    already shown."""
    out, cache = [], {}
    for c in all_conversations():
        s = c["summary"] or {}
        if before and c["start"] >= before or tag and tag not in (s.get("tags") or []) or kind and c.get("kind", "对话") != kind:
            continue
        if q:
            meta = json.dumps([s.get(k) for k in ("title", "summary", "tags", "todos", "decisions")], ensure_ascii=False)
            if q.lower() not in meta.lower() and not any(q in (r.get("text") or "") for r in conversation_rows(c, cache)):
                continue
        out.append({k: v for k, v in c.items() if k != "sentences"} | {"sentences": len(c["sentences"])})
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
    return {**c, "summary": load_summary(conv_id), "rows": conversation_rows(c)}


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
    for p in people_bank():
        n.setdefault(p, 0)
    return sorted(n, key=lambda k: -n[k])


@app.post("/api/conversation/{conv_id}/rename")
async def rename(conv_id: str, body: dict):
    """One label of one conversation becomes a name: its rows are rewritten (a final record over the
    conversation, so nothing is edited in place), the summary is redone, and when the recording is still
    here up to ENROLL sentences of it teach the diarist that voice for later conversations."""
    frm, to = body.get("from", ""), (body.get("to") or "").strip()
    c = find_conversation(conv_id)
    if not c:
        raise HTTPException(404, "no such conversation")
    if not to or to == "S?" or len(to) > 20 or to == frm:
        raise HTTPException(400, "bad name")
    rows = conversation_rows(c)
    hit = [r for r in rows if r.get("speaker") == frm]
    if not hit:
        raise HTTPException(400, f"no sentence of {frm}")
    for r in hit:
        r["speaker"] = to
    end = (dt.datetime.fromisoformat(c["end"]) + dt.timedelta(milliseconds=1)).isoformat(timespec="milliseconds")
    for day in sorted({r["start"][:10] for r in rows}):
        with open(final_path(dt.date.fromisoformat(day)), "a") as f:
            f.write(json.dumps({"from": max(c["start"], day + "T00:00:00.000"), "to": min(end, (dt.date.fromisoformat(day) + dt.timedelta(days=1)).isoformat() + "T00:00:00.000"),
                                "rows": [r for r in rows if r["start"][:10] == day], "renamed": {frm: to}}, ensure_ascii=False) + "\n")
    c["participants"] = list(dict.fromkeys(to if p == frm else p for p in c["participants"]))
    p = conversations_path(dt.date.fromisoformat(c["start"][:10]))
    p.write_text("".join(json.dumps(c if x["id"] == conv_id else x, ensure_ascii=False) + "\n" for x in map(json.loads, open(p))))
    queue_summary(c, keep=True)
    learnt = 0
    if to != "我" and not re.fullmatch(r"S\d+", to) and state.get("embed"):  # S<n> means "not known": nothing to learn
        long = sorted((r for r in hit if (dt.datetime.fromisoformat(r["end"]) - dt.datetime.fromisoformat(r["start"])).total_seconds() >= 1.5),
                      key=lambda r: r["start"])[:ENROLL]
        es = []
        for r in long:
            try:
                x = np.frombuffer(await cut(r["start"], min(r["end"], (dt.datetime.fromisoformat(r["start"]) + dt.timedelta(seconds=15)).isoformat()), "s16le"), np.int16)
            except HTTPException:  # history conversations have no recording here
                continue
            es.append(await asyncio.to_thread(state["embed"], x))
        if es:
            bank = json.loads(PEOPLE.read_text()) if PEOPLE.exists() else {}
            old = bank.get(to, {"n": 0, "e": np.zeros(len(es[0])).tolist()})
            n = old["n"] + len(es)
            mean = (np.array(old["e"]) * old["n"] + np.sum(es, 0)) / n
            bank[to] = {"n": n, "e": [round(float(v), 6) for v in mean]}
            PEOPLE.write_text(json.dumps(bank, ensure_ascii=False))
            state["dia"].people[to] = mean / np.linalg.norm(mean)
            learnt = len(es)
    note(f"对话 {c['start'][11:19]}：{frm} 改名为 {to}" + (f"，记住了 {learnt} 句的声纹" if learnt else ""), "state")
    return {"participants": c["participants"], "renamed": len(hit), "voiceprints": learnt}


ENROLL = 20


@app.get("/api/audio")
async def audio(start: str, end: str):
    """WAV of [start, end] (+0.2 s each side) cut from the uploaded pieces, or the recorder's file, covering it."""
    wav = await cut(start, end, "wav")
    return Response(wav, media_type="audio/wav")


async def cut(start: str, end: str, fmt: str) -> bytes:
    a, b = dt.datetime.fromisoformat(start), dt.datetime.fromisoformat(end)
    if b <= a or (b - a).total_seconds() > 120:
        raise HTTPException(404, "no recording covers this time")
    a_ms, b_ms = int(a.timestamp() * 1000), int(b.timestamp() * 1000)
    pieces = sorted(p for p in (state["dir"] / "chunks").glob("*/*.*")
                    if p.stem.isdigit() and int(p.stem) <= b_ms and int(p.stem) + CHUNK_MAX_S * 1000 >= a_ms)
    if pieces:  # uploaded pieces: concatenated back to back (the small gaps between them are ignored)
        path, off = pieces[0], max(0.0, (a_ms - int(pieces[0].stem)) / 1000 - 0.2)
        ins = [x for p in pieces for x in ("-i", str(p))]
        cmd = ["ffmpeg", "-v", "error", *ins, "-filter_complex", "".join(f"[{i}:a]" for i in range(len(pieces))) +
               f"concat=n={len(pieces)}:v=0:a=1[a]", "-map", "[a]", "-ss", f"{off:.3f}", "-t", f"{(b - a).total_seconds() + 0.4:.3f}",
               "-ac", "1", "-ar", str(SR), "-f", fmt, "pipe:1"]
    else:
        files = [p for p in recordings(state["dir"]) if file_start(p) <= a]
        if not files:
            raise HTTPException(404, "no recording covers this time")
        path = max(files, key=file_start)
        off = max(0.0, (a - file_start(path)).total_seconds() - 0.2)
        cmd = ["ffmpeg", "-v", "error", "-ss", f"{off:.3f}", "-t", f"{(b - a).total_seconds() + 0.4:.3f}", "-i", str(path),
               "-ac", "1", "-ar", str(SR), "-f", fmt, "pipe:1"]
    out = await asyncio.to_thread(subprocess.run, cmd, capture_output=True)
    if out.returncode or len(out.stdout) <= 44:
        raise HTTPException(404, f"could not cut {path.name}: {out.stderr.decode()[-200:]}")
    return out.stdout


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
    ap.add_argument("--from-start", action="store_true", help="transcribe the current segment from its beginning")
    args = ap.parse_args()
    if DIARIZER not in ("cloud", "local"):
        raise SystemExit(f"DIARIZER must be cloud or local, not {DIARIZER!r}")
    key = os.environ.get("STEPFUN_API_KEY", "")
    missing = [k for k in ("ASR_UPLOAD_BRIDGE_TOKEN", "ASR_SERVICE_BASIC_AUTH") if not os.environ.get(k)]
    if ENGINE == "volc" and not os.environ.get("VOLC_ASR_KEY"):
        missing.append("VOLC_ASR_KEY")
    if ENGINE not in ("volc", "stepfun"):
        raise SystemExit(f"ENGINE must be volc or stepfun, not {ENGINE!r}")
    if (FIRST_LAYER or TEXT_SOURCE == "sse") and not (key and ASR_URL):
        missing += ["STEPFUN_API_KEY / STEPFUN_SSE_URL"]
    missing += [k for k in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL") if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"not set: {', '.join(missing)} (see .env.example)")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    state["dir"] = args.dir
    args.dir.mkdir(parents=True, exist_ok=True)
    state["commit"] = subprocess.run(["git", "-C", str(HERE), "rev-parse", "--short", "HEAD"],
                                     capture_output=True, text=True).stdout.strip()

    @app.on_event("startup")
    async def start():
        for name, coro in (("pipeline", pipeline(args.dir, args.from_start, key)), ("status", status_loop()), ("summary", summary_worker())):
            asyncio.create_task(coro, name=name).add_done_callback(
                lambda t, n=name: log.error("task %s ended: %r", n, t.exception()) if not t.cancelled() else None)

    import uvicorn
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
