"""A backlog does not hold back the drafts (10-06: a phone back after 3 h offline sent them at once and the live page
showed nothing new until all of it was final). While the final layer is still busy at the start of a 3 h backlog and
audio of now keeps coming, the drafts follow what is said now; the backlog is never drafted (no engine call spent on
it); once the final layer goes on, it finalizes every utterance of the backlog in order, none lost, none twice.
Real audio store, streams and pipeline; FireRed's own stream postprocessor on frame energy for the VAD (no
checkpoint in CI), a fixed voiceprint and fake engines that read back which tone they hear."""
import asyncio
import datetime as dt
import subprocess
import threading
import time

import numpy as np
from fireredvad import FireRedStreamVad, FireRedStreamVadConfig
from fireredvad.core.stream_vad_postprocessor import StreamVadPostprocessor

import audio
import db
import server

SR, DEVICE = 16000, "LR-TEST"
BACKLOG_S, PIECE_S, LIVE_S = 3 * 3600, 60, 15  # the backlog in pieces of PIECE_S, a tone 10 s into each; then LIVE_S of now
N = BACKLOG_S // PIECE_S


class EnergyVad(FireRedStreamVad):
    def detect_chunk(self, x):
        loud = np.lib.stride_tricks.sliding_window_view(np.abs(x.astype(np.int32)), 400)[::160].mean(1) > 500
        return [self.postprocessor.process_one_frame(float(v)) for v in loud]


def energy_vad(cls, model_dir, c=FireRedStreamVadConfig()):
    return EnergyVad(None, None, StreamVadPostprocessor(c.smooth_window_size, c.speech_threshold, c.pad_start_frame,
                                                         c.min_speech_frame, c.max_speech_frame, c.min_silence_frame), c)


def tone(idx: int, s: float) -> np.ndarray:
    """s seconds of a square wave whose amplitude is idx."""
    return (np.sign(np.sin(2 * np.pi * 500 * np.arange(int(s * SR)) / SR)) * (1000 + 50 * idx)).astype(np.int16)


def flac(x: np.ndarray) -> bytes:  # lossless: the amplitudes come back exact
    return subprocess.run(["ffmpeg", "-v", "error", "-f", "s16le", "-ar", str(SR), "-ac", "1", "-i", "pipe:0", "-f", "flac", "pipe:1"],
                          input=x.tobytes(), capture_output=True, check=True).stdout


def heard(x: np.ndarray, partial=None) -> list[dict]:
    """One sentence per tone, its text the tone's idx."""
    loud = np.abs(x[:len(x) // 160 * 160].astype(np.int32)).reshape(-1, 160).mean(1) > 500
    edges = np.flatnonzero(np.diff(np.r_[0, loud.astype(np.int8), 0]))
    return [{"start_ms": int(a) * 10, "end_ms": int(b) * 10, "speaker": "SPEAKER_0",
             "text": str(round((np.median(np.abs(x[a * 160:b * 160].astype(np.int32))) - 1000) / 50))}
            for a, b in zip(edges[::2], edges[1::2])]


def test_backlog_does_not_hold_drafts(tmp_path, monkeypatch):
    monkeypatch.setattr(FireRedStreamVad, "from_pretrained", classmethod(energy_vad))
    monkeypatch.setattr(server, "DATA", tmp_path)
    monkeypatch.setattr(server, "DRAFT_ENGINE", "stepfun-sse")
    monkeypatch.setattr(server, "ENGINE_FALLBACK", "")
    monkeypatch.setattr(server.summary, "MIN_SPEECH_S", 1e9)  # no live analysis (an LLM call)
    busy, sent = threading.Event(), {"draft": 0.0}  # the final layer's engine answers once busy is set

    def final_engine(x, partial=None):
        busy.wait()
        return heard(x)

    def draft_engine(x, partial=None):
        sent["draft"] += len(x) / SR
        return heard(x)
    monkeypatch.setitem(server.PROVIDERS, server.ENGINE, final_engine)
    monkeypatch.setitem(server.PROVIDERS, "stepfun-sse", draft_engine)
    np.savez(tmp_path / "owner-redimnet2.npz", owner=np.array([1.0, 0, 0, 0]))
    audio.DIR = tmp_path
    db.open_db(tmp_path / "live.db")
    monkeypatch.setitem(server.state, "embed", lambda x: np.array([0.0, 1, 0, 0]))
    monkeypatch.setitem(server.state, "watch_until", time.time() + 3600)  # a page watches

    silence = np.zeros(PIECE_S * SR, np.int16)
    backlog = [flac(np.concatenate([silence[:10 * SR], tone(i, 3), silence[13 * SR:]])) for i in range(N)]
    now = np.concatenate([np.concatenate([tone(N + j, 2), np.zeros(3 * SR, np.int16)]) for j in range(LIVE_S // 5)])
    now = [flac(now[k * SR:(k + 1) * SR]) for k in range(LIVE_S)]  # 1 s pieces, as an app in front sends them
    live0 = dt.datetime.fromtimestamp(round(time.time()) + 2)
    t0 = live0 - dt.timedelta(seconds=PIECE_S + BACKLOG_S)  # the backlog: t0 .. live0 - PIECE_S
    save = lambda t, body: audio.save(DEVICE, int(t.timestamp() * 1000), body, "audio/flac")
    st = lambda: server.state["devices"][DEVICE]

    async def run():
        try:
            save(t0, backlog[0])  # the pipeline has started; the phone goes offline, then comes back with the rest at once
            pipe = asyncio.create_task(server.pipeline(DEVICE))
            await asyncio.sleep(1)
            rest = asyncio.ensure_future(asyncio.to_thread(lambda: [save(t0 + dt.timedelta(seconds=PIECE_S * i), b) for i, b in enumerate(backlog) if i]))
            for k, body in enumerate(now):  # meanwhile what is said now, as it is said
                await asyncio.sleep(live0.timestamp() + k + 1 - time.time())
                await asyncio.to_thread(save, live0 + dt.timedelta(seconds=k), body)
            await rest
            for _ in range(20):  # the last tone's pause has just come in
                if {str(N + j) for j in range(LIVE_S // 5)} <= {r["text"] for r in st()["drafts"]}:
                    break
                await asyncio.sleep(0.5)
            drafts = db.drafts("", None, DEVICE)
            assert [r["text"] for r in drafts if int(r["text"]) >= N] == [str(N + j) for j in range(LIVE_S // 5)], drafts
            assert st()["done_to"] is None or st()["done_to"] < t0 + dt.timedelta(seconds=PIECE_S), "the final layer was to be busy"
            edge = live0 - dt.timedelta(seconds=PIECE_S + server.DRAFT_BACK_S)
            assert all(r["start"] >= server.iso(edge) for r in drafts), "the backlog is not drafted"
            assert sent["draft"] < server.DRAFT_BACK_S + LIVE_S, sent

            busy.set()
            finals = lambda: db.sentences(server.iso(t0), server.iso(live0), source=DEVICE)
            for _ in range(240):
                if len(finals()) >= N:
                    break
                await asyncio.sleep(0.5)
            assert [r["text"] for r in finals()] == [str(i) for i in range(N)], [r["text"] for r in finals()][:20]
            pipe.cancel()
        finally:
            busy.set()  # a failed check leaves the final layer's call waiting, and asyncio.run waits for its thread

    asyncio.run(run())
