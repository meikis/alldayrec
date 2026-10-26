"""Uncontended local drafts cover screen-off pieces and page-open backfill."""
import datetime as dt

import anyio
import numpy as np

from alldayrec import asr, audio, device_pipeline
from test_backlog import flac, heard, rig, tone

SR, DEV = 16000, "test-cadence"


def test_sixty_second_pieces_get_whole_drafts(tmp_path, monkeypatch):
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
    p1 = np.concatenate([tone(1, 22), z(5), tone(2, 23), z(5)])   # 55 s
    p2 = np.concatenate([tone(3, 25), z(5), tone(4, 25), z(5)])   # 60 s, starts where p1 ends
    now = dt.datetime.now()
    t1 = now - dt.timedelta(seconds=115)
    t2 = t1 + dt.timedelta(seconds=55)
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
    assert "3" in texts(), f"tone 3 (first half of the new 60 s piece) never drafted: {windows}"


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
