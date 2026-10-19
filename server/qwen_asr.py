"""Alibaba Bailian (DashScope) qwen-audio-3.1-asr-flash-filetrans for engine_segs: text, sentences and speakers from one
async file job. On the five GT cases (2026-10-19, the same ≤ 5 min blocks) its text beat StepFun's SSE on four (home
11.6% CER vs 13.6%), its speakers about as StepFun's file job; billed by tokens, ~¥0.05 an hour of audio.

The window goes up lossless (16 kHz wav: StepFun's file job lost ~3 points CER on 16 kbps Opus) through the upload
bridge, Bailian fetches it by URL; the job takes ~150 s for 5 min. The built-in sensitive-word masking (words turned
into ***) is off.
"""
import io
import json
import os
import time
import wave

import anyio
import numpy as np

import net

MODEL = os.environ.get("QWEN_ASR_MODEL") or "qwen-audio-3.1-asr-flash-filetrans"
BASE = "https://dashscope.aliyuncs.com/api/v1"
POLL_S = 2
WAIT_S = 900


def wav(x: np.ndarray) -> bytes:
    """16 kHz int16 mono PCM as a wav file."""
    f = io.BytesIO()
    with wave.open(f, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(x.astype(np.int16).tobytes())
    return f.getvalue()


def sentences(transcripts: list[dict]) -> list[dict]:
    """A transcription's sentences as engine_segs takes them, speaker ids as SPEAKER_<n> (None without one)."""
    return [{"start_ms": s["begin_time"], "end_ms": s["end_time"], "text": s["text"],
             "speaker": None if s.get("speaker_id") is None else f"SPEAKER_{s['speaker_id']}"}
            for t in transcripts[:1] for s in t.get("sentences", []) if s.get("text", "").strip()]


async def transcribe(x: np.ndarray, upload) -> list[dict]:
    """x: 16 kHz int16 PCM; upload(data, content_type) -> a URL Bailian can fetch. Raises on an HTTP error, a failed
    task, or one still running after WAIT_S."""
    url = await upload(wav(x), "audio/wav")
    auth = {"Authorization": f"Bearer {os.environ['DASHSCOPE_API_KEY']}", "Content-Type": "application/json"}
    body = {"model": MODEL, "input": {"file_urls": [url]}, "parameters": {
        "channel_id": [0], "language_hints": ["zh", "en"], "diarization_enabled": True,
        "special_word_filter": json.dumps({"system_reserved_filter": False})}}
    r = await net.request("POST", f"{BASE}/services/audio/asr/transcription", 60, content=json.dumps(body).encode(),
                          headers={**auth, "X-DashScope-Async": "enable"})
    task = json.loads(r.content)["output"]["task_id"]
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        await anyio.sleep(POLL_S)
        o = json.loads((await net.request("GET", f"{BASE}/tasks/{task}", 60, headers=auth)).content)["output"]
        if o["task_status"] in ("PENDING", "RUNNING"):
            continue
        res = (o.get("results") or [{}])[0]
        if o["task_status"] != "SUCCEEDED" or res.get("subtask_status", "SUCCEEDED") != "SUCCEEDED":
            raise RuntimeError(f"qwen asr {o['task_status']}: {res.get('code') or o.get('code')} {res.get('message') or o.get('message')}"[:200])
        return sentences(json.loads((await net.request("GET", res["transcription_url"], 60)).content).get("transcripts", []))
    raise RuntimeError(f"qwen asr task {task} still running after {WAIT_S} s")
