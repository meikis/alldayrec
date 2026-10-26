"""Uncontended local drafts cover screen-off pieces and page-open backfill."""
import datetime as dt

import anyio
import numpy as np
import pytest

from alldayrec import asr, audio, device_pipeline
from test_backlog import flac, heard, rig, tone

SR, DEV = 16000, "test-cadence"


@pytest.mark.parametrize("first_s", [55, 60])
def test_sixty_second_pieces_get_whole_drafts(tmp_path, monkeypatch, first_s):
    async def final_engine(x, partial=None):
        await anyio.sleep_forever()
    rig(tmp_path, monkeypatch, final_engine)
    monkeypatch.setattr(asr, "DRAFT_ENGINE", "local")
    windows = []

    async def draft_engine(x, partial=None):
        h = heard(x)
        windows.append((round(len(x) / SR, 1), [g["text"] for g in h]))
        return h
    monkeypatch.setitem(asr.PROVIDERS, "local", draft_engine)
    z = lambda s: np.zeros(int(s * SR), np.int16)
    p1 = np.concatenate([tone(1, first_s - 33), z(5), tone(2, 23), z(5)])
    p2 = np.concatenate([tone(3, 25), z(5), tone(4, 25), z(5)])   # 60 s, starts where p1 ends
    now = dt.datetime.now()
    t1 = now - dt.timedelta(seconds=first_s + 60)
    t2 = t1 + dt.timedelta(seconds=first_s)
    texts = lambda: {t for _, ts in windows for t in ts}

    async def scenario():
        audio.save(DEV, round(t1.timestamp() * 1000), flac(p1), "audio/flac")
        async with anyio.create_task_group() as tg:
            tg.start_soon(device_pipeline.pipeline, DEV)
            with anyio.fail_after(30):
                while "2" not in texts():
                    await anyio.sleep(.05)
            await anyio.sleep(2)
            audio.save(DEV, round(t2.timestamp() * 1000), flac(p2), "audio/flac")
            with anyio.fail_after(30):
                while "4" not in texts():
                    await anyio.sleep(.05)
            await anyio.sleep(2)
            tg.cancel_scope.cancel()
    anyio.run(scenario)
    assert texts() == {"1", "2", "3", "4"}, f"a piece was not fully drafted: {windows}"


def test_open_page_drafts_all_120_seconds_unfinal(tmp_path, monkeypatch):
    """Opening a page is backfill, even when a final is still running."""
    async def final_engine(x, partial=None):
        await anyio.sleep_forever()
    rig(tmp_path, monkeypatch, final_engine)
    monkeypatch.setattr(asr, "DRAFT_ENGINE", "local")
    windows = []
    async def draft_engine(x, partial=None):
        windows.append((len(x) / SR, heard(x)))
        return heard(x)
    monkeypatch.setitem(asr.PROVIDERS, "local", draft_engine)
    quiet = np.zeros(3 * SR, np.int16)
    x = np.concatenate([tone(1, 87), quiet, tone(2, 27), quiet])
    start = dt.datetime.now() - dt.timedelta(seconds=120)
    async def scenario():
        audio.save(DEV, round(start.timestamp() * 1000), flac(x), "audio/flac")
        async with anyio.create_task_group() as tg:
            tg.start_soon(device_pipeline.pipeline, DEV)
            with anyio.fail_after(15):
                while not windows:
                    await anyio.sleep(.01)
            tg.cancel_scope.cancel()
    anyio.run(scenario)
    assert windows[0][0] > 115, windows
    assert {g["text"] for g in windows[0][1]} == {"1", "2"}


@pytest.mark.parametrize('arrival,engine,first', [
    ('during', 'local', False), ('after', 'local', False),
    ('during', 'stepfun-sse', False), ('during', 'mixed', False),
    ('during', 'fallback', False), ('during', 'local', True),
])
def test_real_wait_skips_existing_pending_audio_only(tmp_path, monkeypatch, arrival, engine, first):
    import threading
    from alldayrec import threads
    async def final_engine(x, partial=None): await anyio.sleep_forever()
    rig(tmp_path, monkeypatch, final_engine)
    monkeypatch.setattr(asr, 'ENGINE', 'qwen' if engine == 'mixed' else 'local')
    provider = 'local' if engine in ('mixed', 'fallback') else engine
    monkeypatch.setattr(asr, 'DRAFT_ENGINE', provider)
    if engine == 'fallback': monkeypatch.setattr(asr, 'ENGINE_FALLBACK', 'qwen')
    monkeypatch.setattr(threads, 'ASR', threads._ASRSlots(1))
    monkeypatch.setattr(device_pipeline, 'LOCAL_DRAFT_WAIT_S', .03)
    monkeypatch.setattr(device_pipeline, 'REFINE_EVERY_S', 1)
    windows, spans = [], []
    entered, release = threading.Event(), threading.Event()
    def hold():
        entered.set()
        assert release.wait(15)
    async def occupy(): await anyio.to_thread.run_sync(hold, limiter=threads.ASR)
    async def draft_engine(x, partial=None):
        rows = await anyio.to_thread.run_sync(heard, x, limiter=threads.ASR)
        windows.append((len(x) / SR, {g['text'] for g in rows}))
        spans.append({g['text']: g['end_ms'] - g['start_ms'] for g in rows})
        return rows
    monkeypatch.setitem(asr.PROVIDERS, provider, draft_engine)
    z = lambda s: np.zeros(int(s * SR), np.int16)
    p1 = flac(np.concatenate([tone(1, 22), z(5), tone(2, 23), z(5)]))
    small = flac(np.concatenate([tone(3, 20), z(5)]))
    more = flac(np.concatenate([tone(4, 25), z(10)]))
    later = flac(np.concatenate([tone(4, 25), z(5), tone(5, 25), z(5)]))
    initial = flac(np.concatenate([tone(1, 27), z(5), tone(2, 23), z(5), tone(3, 25), z(5), tone(4, 25), z(5)]))
    start = dt.datetime.now() - dt.timedelta(seconds=140)
    def save(offset, body): audio.save(DEV, round((start+dt.timedelta(seconds=offset)).timestamp()*1000), body, 'audio/flac')
    async def scenario():
        save(0, initial if first else p1)
        async with anyio.create_task_group() as tg:
            try:
                if not first:
                    tg.start_soon(device_pipeline.pipeline, DEV)
                    with anyio.fail_after(10):
                        while not windows: await anyio.sleep(.01)
                    await anyio.sleep(.2)
                tg.start_soon(occupy)
                with anyio.fail_after(3):
                    while not entered.is_set(): await anyio.sleep(.001)
                if first: tg.start_soon(device_pipeline.pipeline, DEV)
                else: save(55, small)
                with anyio.fail_after(10):
                    while not threads.ASR.statistics().tasks_waiting: await anyio.sleep(.01)
                await anyio.sleep(.05)
                before = len(windows)
                if arrival == 'during' and not first:
                    save(80, more)
                    await anyio.sleep(1.2)
                release.set()
                with anyio.fail_after(10):
                    while len(windows) <= before: await anyio.sleep(.01)
                await anyio.sleep(.2)
                if arrival == 'after': save(80, later)
                target = '5' if arrival == 'after' else '4'
                with anyio.fail_after(10):
                    while not any(target in ts for _, ts in windows): await anyio.sleep(.02)
                await anyio.sleep(.1)
            finally:
                release.set()
                tg.cancel_scope.cancel()
    anyio.run(scenario)
    if first:
        assert windows[0][0] > 110 and windows[0][1] == {'1', '2', '3', '4'}, windows
    elif arrival == 'after':
        n, ts = next((n, ts) for n, ts in windows if '5' in ts)
        assert n > 45 and {'4', '5'} <= ts, windows
    else:
        n, ts = next((n, ts) for n, ts in windows if '4' in ts)
        index = next(i for i, (_, words) in enumerate(windows) if '4' in words)
        if engine == 'local':
            assert n <= 30 and spans[index]['4'] < 22000, (windows, spans)
        else:
            assert spans[index]['4'] >= 24000, (windows, spans)
