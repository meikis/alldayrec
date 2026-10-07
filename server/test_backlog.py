"""A backlog does not hold back the drafts or the live analysis (10-06: a phone back after 3 h offline sent them at
once and the live page showed nothing new until all of it was final). While the final layer is still busy at the start
of a 3 h backlog and audio of now keeps coming, the drafts follow what is said now and the live analysis takes them as
the conversation going on; the backlog is never drafted (no engine call spent on it) nor analyzed live; once the final
layer goes on, it finalizes every utterance of the backlog in order, none lost, none twice, and the same live analysis
goes on without seeing a sentence twice. Real audio store, streams and pipeline; FireRed's own stream postprocessor on
frame energy for the VAD (no checkpoint in CI), a fixed voiceprint, fake engines that read back which tone they hear
and a fake LLM that adds a topic per turn."""
import asyncio
import datetime as dt
import re
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


def rig(tmp_path, monkeypatch, final_engine):
    """A pipeline's world: the energy VAD, fake engines and LLM, a page watching; returns what the draft engine was
    sent (seconds) and what the live analysis was fed (tone numbers per turn)."""
    monkeypatch.setattr(FireRedStreamVad, "from_pretrained", classmethod(energy_vad))
    monkeypatch.setattr(server, "DATA", tmp_path)
    monkeypatch.setattr(server, "DRAFT_ENGINE", "stepfun-sse")
    monkeypatch.setattr(server, "ENGINE_FALLBACK", "")
    monkeypatch.setattr(server.summary, "MIN_SPEECH_S", 3)
    monkeypatch.setattr(server, "LIVE_EVERY_S", 1)
    sent, fed = {"draft": 0.0}, []

    def llm(messages, timeout=None, kind=None):  # each turn's new sentences (their tone numbers), one topic added
        new = [int(t) for t in re.findall(r"^\[[\d:]{8}\] [^:]+: (\d+)$", messages[-1]["content"], re.M)]
        fed.append(new)
        return {"ops": [{"op": "add", "title": f"t{new[0]}"}]}, {"choices": [{"message": {"content": "{}"}}]}
    monkeypatch.setattr(server.live, "complete", llm)

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
    return sent, fed


def save(t: dt.datetime, body: bytes):
    return audio.save(DEVICE, int(t.timestamp() * 1000), body, "audio/flac")


def st():
    return server.state["devices"][DEVICE]


def test_backlog_does_not_hold_drafts(tmp_path, monkeypatch):
    busy = threading.Event()  # the final layer's engine answers once busy is set

    def final_engine(x, partial=None):
        busy.wait()
        return heard(x)
    sent, fed = rig(tmp_path, monkeypatch, final_engine)
    silence = np.zeros(PIECE_S * SR, np.int16)
    backlog = [flac(np.concatenate([silence[:10 * SR], tone(i, 3), silence[13 * SR:]])) for i in range(N)]
    now = np.concatenate([np.concatenate([tone(N + j, 2), np.zeros(3 * SR, np.int16)]) for j in range(LIVE_S // 5)])
    now = [flac(now[k * SR:(k + 1) * SR]) for k in range(LIVE_S)]  # 1 s pieces, as an app in front sends them
    live0 = dt.datetime.fromtimestamp(round(time.time()) + 2)
    t0 = live0 - dt.timedelta(seconds=PIECE_S + BACKLOG_S)  # the backlog: t0 .. live0 - PIECE_S

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
            for _ in range(20):  # the analysis of the last pause's drafts
                if fed and fed[-1] and fed[-1][-1] == N + LIVE_S // 5 - 1:
                    break
                await asyncio.sleep(0.5)
            lv = st()["live"]
            assert lv and lv["start"] >= server.iso(edge) and lv["topics"], lv and server.live.public(lv)
            assert fed and fed[-1][-1] == N + LIVE_S // 5 - 1 and min(min(f) for f in fed) >= N - server.DRAFT_BACK_S // PIECE_S - 1, fed

            busy.set()
            finals = lambda: db.sentences(server.iso(t0), server.iso(live0), source=DEVICE)
            for _ in range(240):
                if len(finals()) >= N:
                    break
                await asyncio.sleep(0.5)
            assert [r["text"] for r in finals()] == [str(i) for i in range(N)], [r["text"] for r in finals()][:20]
            await asyncio.sleep(3)  # the final layer's turn of the live analysis, now that the conversation is final
            assert st()["live"]["start"] == lv["start"], "the same live analysis goes on"
            assert len(sum(fed, [])) == len(set(sum(fed, []))), fed
            pipe.cancel()
        finally:
            busy.set()  # a failed check leaves the final layer's call waiting, and asyncio.run waits for its thread

    asyncio.run(run())


def test_hold_keeps_backlog_final(tmp_path, monkeypatch):
    """The phone sends what is said now first and the backlog it reads off the card after it, each upload with hold =
    where now began (#130): drafts follow now at once, finalization takes the whole backlog in order (none arrives
    behind it) and waits at the hold; once the phone lets go, now is finalized after it."""
    sent, fed = rig(tmp_path, monkeypatch, lambda x, partial=None: heard(x))
    n = 20  # 20 min of backlog
    silence = np.zeros(PIECE_S * SR, np.int16)
    backlog = [flac(np.concatenate([silence[:10 * SR], tone(i, 3), silence[13 * SR:]])) for i in range(n)]
    now = np.concatenate([np.concatenate([tone(n + j, 2), np.zeros(3 * SR, np.int16)]) for j in range(LIVE_S // 5)])
    now = [flac(now[k * SR:(k + 1) * SR]) for k in range(LIVE_S)]
    live0 = dt.datetime.fromtimestamp(round(time.time()) + 2)
    t0 = live0 - dt.timedelta(seconds=PIECE_S * (n + 1))
    h = int(live0.timestamp() * 1000)
    finals = lambda: [r["text"] for r in db.sentences(server.iso(t0), server.iso(live0 + dt.timedelta(seconds=LIVE_S)), source=DEVICE)]

    async def run():
        save(t0, backlog[0])  # final up to here before the phone went away
        pipe = asyncio.create_task(server.pipeline(DEVICE))
        await asyncio.sleep(1)
        late = []

        def card():  # the rest of the backlog, as the phone reads it off the card meanwhile
            for i in range(1, n):
                time.sleep(0.2)
                audio.hold(DEVICE, h)
                t = t0 + dt.timedelta(seconds=PIECE_S * i)
                if int(t.timestamp() * 1000) <= audio.read_to.get(DEVICE, -1):
                    late.append(i)
                save(t, backlog[i])
        rest = None
        for k, body in enumerate(now):
            await asyncio.sleep(live0.timestamp() + k + 1 - time.time())
            audio.hold(DEVICE, h)
            await asyncio.to_thread(save, live0 + dt.timedelta(seconds=k), body)
            if k == 4:
                rest = asyncio.ensure_future(asyncio.to_thread(card))
        await rest
        for _ in range(60):
            if len(finals()) >= n and {str(n + j) for j in range(LIVE_S // 5)} <= {r["text"] for r in st()["drafts"]}:
                break
            await asyncio.sleep(0.5)
        assert not late, f"backlog pieces behind the queue: {late}"
        assert finals() == [str(i) for i in range(n)], finals()
        assert {str(n + j) for j in range(LIVE_S // 5)} <= {r["text"] for r in st()["drafts"]}, "drafts follow now meanwhile"
        assert sent["draft"] < server.DRAFT_BACK_S + LIVE_S, sent
        assert server.device_status(DEVICE, st())["hold"] == live0.isoformat(timespec="seconds")
        await asyncio.sleep(3)
        assert finals() == [str(i) for i in range(n)], "now waits at the hold"

        audio.hold(DEVICE, None)  # the next upload says nothing older is coming
        db.put_kv(f"end_at:{DEVICE}", server.iso(live0 + dt.timedelta(seconds=LIVE_S)))  # and the recorder stops
        want = [str(i) for i in range(n + LIVE_S // 5)]
        for _ in range(60):
            if finals() == want:
                break
            await asyncio.sleep(0.5)
        assert finals() == want, finals()
        pipe.cancel()

    asyncio.run(run())


def test_hold_expires(tmp_path, monkeypatch):
    """A phone that never comes back: a hold not said again for HOLD_MAX_S is let go and the queue goes on; older audio
    arriving after that is kept, not finalized (as before #130)."""
    monkeypatch.setattr(audio, "HOLD_MAX_S", 2)
    audio.DIR = tmp_path
    db.open_db(tmp_path / "live.db")
    t = lambda m: dt.datetime(2026, 10, 6, 10, m)
    piece = flac(tone(0, 3))
    save(t(0), piece)
    audio.hold(DEVICE, int(t(5).timestamp() * 1000))
    save(t(5), piece)

    async def run():
        s, got = audio.stream(DEVICE, None), []
        got.append((await anext(s))[0])
        nxt = asyncio.create_task(anext(s))
        await asyncio.sleep(1)
        assert not nxt.done(), "held"
        got.append((await asyncio.wait_for(nxt, 5))[0])
        assert got == [t(0), t(5)]
        nxt = asyncio.create_task(anext(s))  # the reader back for more: 10:05 is done
        await asyncio.sleep(0.5)
        assert audio.held(DEVICE) is None and int(t(3).timestamp() * 1000) <= audio.read_to[DEVICE], "10:03 now comes too late"
        nxt.cancel()

    asyncio.run(run())



def test_final_goes_on_after_a_gap(tmp_path, monkeypatch):
    """A recording stopped and finalized to its very end, the next one after a pause (10-07: the iPhone's microphone up
    to 17:58:55, then from 19:09): the first cut after the finalized time fell in the pause, a piece of no samples, and
    the final layer waited on it for good (370 pieces queued; restarts did not help). It goes on over the pause, in the
    same pipeline and in one started after it."""
    rig(tmp_path, monkeypatch, lambda x, partial=None: heard(x))
    t0 = dt.datetime.fromtimestamp(round(time.time()) - 900)
    at = lambda k: t0 + dt.timedelta(seconds=180 * k)  # a recording every 3 min
    finals = lambda: [r["text"] for r in db.sentences(server.iso(t0), server.iso(at(5)), source=DEVICE)]
    stop = lambda t: db.put_kv(f"end_at:{DEVICE}", server.iso(t))

    async def until(want):
        for _ in range(60):
            if finals() == want:
                return
            await asyncio.sleep(0.5)
        assert finals() == want, (finals(), st()["done_to"])

    async def run():
        save(at(0), flac(np.concatenate([tone(0, 2), np.zeros(3 * SR, np.int16)])))  # 5 s, then stopped
        stop(at(0) + dt.timedelta(seconds=5))
        pipe = asyncio.create_task(server.pipeline(DEVICE))
        await until(["0"])
        assert st()["done_to"] == at(0) + dt.timedelta(seconds=5), "finalized to the very end of the recording"
        # 45 s in 1 s pieces, quiet at first (the VAD pads an utterance's start back: across the pause into the
        # recording before, it bridged the pause), a pause after the tone ends the final piece
        x = np.concatenate([np.zeros(5 * SR, np.int16), tone(1, 2), np.zeros(38 * SR, np.int16)])
        for k in range(45):
            save(at(1) + dt.timedelta(seconds=k), flac(x[k * SR:(k + 1) * SR]))
        await until(["0", "1"])
        stop(at(1) + dt.timedelta(seconds=45))
        for _ in range(20):
            if st()["done_to"] == at(1) + dt.timedelta(seconds=45):
                break
            await asyncio.sleep(0.5)
        assert st()["done_to"] == at(1) + dt.timedelta(seconds=45), st()["done_to"]
        pipe.cancel()
        save(at(2), flac(np.concatenate([tone(2, 2), np.zeros(3 * SR, np.int16)])))  # while the server restarts
        stop(at(2) + dt.timedelta(seconds=5))
        pipe = asyncio.create_task(server.pipeline(DEVICE))  # resumes at the end of a recording, the next after a pause
        await until(["0", "1", "2"])
        pipe.cancel()

    asyncio.run(run())


def test_hold_release(tmp_path, monkeypatch):
    """The phone no longer has the recorder (unbound, or another bound): the hold goes at once, not HOLD_MAX_S after
    the phone last said it (10-07: LR-P3-116C's 261 pieces waited from 17:47 to 05:47). Meanwhile the status says when
    the phone last said it and until when the queue waits without it, and whether the device is recording now."""
    audio.DIR = tmp_path
    db.open_db(tmp_path / "live.db")
    monkeypatch.setattr(server, "note", lambda *a, **k: None)
    t = dt.datetime(2026, 10, 7, 17, 36)
    save(t, flac(tone(0, 3)))  # it came just now: the device records
    audio.hold(DEVICE, int(t.timestamp() * 1000))
    st = {"audio_end": None, "done_to": None, "progress": [], "latency": [], "final_latency": [], "drafting": False,
          "last_speech": None, "refine": None, "conversation": None, "draft": None, "draft_to": None}
    d = server.device_status(DEVICE, st)
    assert d["hold"] == "2026-10-07T17:36:00" and d["recording"], d
    seen, until = dt.datetime.fromisoformat(d["hold_seen"]), dt.datetime.fromisoformat(d["hold_until"])
    assert abs((seen - dt.datetime.now()).total_seconds()) < 5 and (until - seen).total_seconds() == audio.HOLD_MAX_S
    monkeypatch.setattr(server, "LIVE_AUDIO_S", 0)
    assert not server.device_status(DEVICE, st)["recording"], "no audio came in LIVE_AUDIO_S: not recording"

    asyncio.run(server.hold_release(DEVICE))
    d = server.device_status(DEVICE, st)
    assert d["hold"] is None and d["hold_seen"] is None and audio.held(DEVICE) is None
