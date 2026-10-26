"""The local decoder yields its one ASR slot between existing acoustic pieces."""
import threading
import anyio
import numpy as np
import pytest
from alldayrec import local_asr, threads


@pytest.mark.parametrize("long_kind", ["final", "draft"])
def test_local_short_call_runs_between_long_call_pieces(monkeypatch, long_kind):
    short_kind = "draft" if long_kind == "final" else "final"
    entered, release = threading.Event(), threading.Event()
    order = []
    running = 0
    peak = 0
    class Stream:
        def accept_waveform(self, sr, x):
            self.seconds = len(x) / sr
    class Recognizer:
        def create_stream(self):
            return Stream()
        def decode_stream(self, st):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            kind = short_kind if st.seconds == 3 else long_kind
            order.append(kind)
            if len(order) == 1:
                entered.set()
                assert release.wait(5), 'test did not release first acoustic piece'
            st.result = type('Result', (), {'tokens': [' 测', '试', '。'], 'timestamps': [0.1, 0.4, 0.7]})
            running -= 1
    rec = Recognizer()
    monkeypatch.setattr(local_asr, 'recognizer', lambda: rec)
    monkeypatch.setattr(threads, 'ASR', anyio.CapacityLimiter(1))
    audio = np.zeros(45 * local_asr.SR, np.int16)
    got = {}
    async def call(kind, x):
        got[kind] = await local_asr.transcribe(x)
    async def scenario():
        async with anyio.create_task_group() as tg:
            tg.start_soon(call, long_kind, audio)
            with anyio.fail_after(5):
                while not entered.is_set():
                    await anyio.sleep(0.001)
                tg.start_soon(call, short_kind, np.zeros(3 * local_asr.SR, np.int16))
                while not threads.ASR.statistics().tasks_waiting:
                    await anyio.sleep(0.001)
            release.set()
    anyio.run(scenario)
    assert order[:2] == [long_kind, short_kind], order
    assert order.count(long_kind) == len(local_asr.pieces(audio))
    assert peak == 1, 'yielding must not add parallel ASR'
    assert got[long_kind] == local_asr._transcribe(audio), 'same text, sentence boundaries and absolute token times'
    assert got[short_kind] == [{'start_ms': 100, 'end_ms': 700, 'text': '测试。'}]


@pytest.mark.parametrize("engine,limit,heard", [("local", 30, 2), ("stepfun-sse", 240, 1)])
def test_local_drafts_skip_old_audio_but_cloud_keeps_window(tmp_path, monkeypatch, engine, limit, heard):
    """A queued 60 s recording drafts its newest 30 s locally; cloud keeps the existing full window."""
    import datetime as dt
    from alldayrec import asr, audio, device_pipeline
    from test_backlog import rig, flac, tone

    calls = []
    async def final_engine(x, partial=None):
        await anyio.sleep_forever()
    rig(tmp_path, monkeypatch, final_engine)
    monkeypatch.setattr(asr, 'DRAFT_ENGINE', engine)
    async def draft_engine(x, partial=None):
        voiced = np.abs(x.astype(np.int32))
        calls.append((len(x) / local_asr.SR, round((np.median(voiced[voiced > 500]) - 1000) / 50)))
        return []
    monkeypatch.setitem(asr.PROVIDERS, engine, draft_engine)
    now = dt.datetime.now()
    body = flac(np.concatenate([tone(1, 30), tone(2, 27), np.zeros(3 * local_asr.SR, np.int16)]))
    async def scenario():
        audio.save('test-local-window', round((now - dt.timedelta(seconds=60)).timestamp() * 1000), body, 'audio/flac')
        async with anyio.create_task_group() as tg:
            tg.start_soon(device_pipeline.pipeline, 'test-local-window')
            with anyio.fail_after(10):
                while not calls:
                    await anyio.sleep(.01)
            tg.cancel_scope.cancel()
    anyio.run(scenario)
    assert max(seconds for seconds, _ in calls) <= limit
    assert calls[0][1] == heard, calls


def test_local_piece_yield_preserves_cross_piece_sentences(monkeypatch):
    from test_local_asr import Rec

    audio = np.full(45 * local_asr.SR, 1000, np.int16)
    audio[15 * local_asr.SR:int(15.3 * local_asr.SR)] = 0
    audio[35 * local_asr.SR:int(35.3 * local_asr.SR)] = 0
    rec = Rec({15: ([" 你"], [14.95]), 20: ([" 好"], [.05]), 10: ([" 。"], [.02])})
    monkeypatch.setattr(local_asr, "recognizer", lambda: rec)
    expected = local_asr._transcribe(audio)
    assert expected == [{"start_ms": 14950, "end_ms": 15400, "text": "你好。"}]
    assert anyio.run(local_asr.transcribe, audio) == expected
