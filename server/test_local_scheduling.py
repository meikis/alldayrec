"""The local engine keeps one whole-window job under the existing ASR cap."""
import anyio
import numpy as np
from alldayrec import local_asr, threads


def test_whole_window_job(monkeypatch):
    jobs = []
    original = anyio.to_thread.run_sync
    async def observed(func, *args, **kw):
        jobs.append((func, len(args[0]), kw.get("limiter")))
        return await original(func, *args, **kw)
    monkeypatch.setattr(anyio.to_thread, "run_sync", observed)
    monkeypatch.setattr(local_asr, "_transcribe", lambda x: [{"text": "whole"}])
    x = np.zeros(240 * local_asr.SR, np.int16)
    assert anyio.run(local_asr.transcribe, x) == [{"text": "whole"}]
    assert jobs == [(local_asr._transcribe, len(x), threads.ASR)]


def test_asr_wait_is_task_local_and_resets(monkeypatch):
    import threading
    monkeypatch.setattr(threads, 'ASR', threads._ASRSlots(1))
    entered, release = threading.Event(), threading.Event()
    waits = {}
    def hold():
        entered.set()
        assert release.wait(5)
    async def first():
        await anyio.to_thread.run_sync(hold, limiter=threads.ASR)
        waits['first'] = threads.asr_wait_s()
    async def second():
        await anyio.to_thread.run_sync(lambda: None, limiter=threads.ASR)
        waits['second'] = threads.asr_wait_s()
        await anyio.to_thread.run_sync(lambda: None, limiter=threads.ASR)
        waits['reset'] = threads.asr_wait_s()
    async def scenario():
        async with anyio.create_task_group() as tg:
            try:
                tg.start_soon(first)
                with anyio.fail_after(3):
                    while not entered.is_set(): await anyio.sleep(.001)
                    tg.start_soon(second)
                    while not threads.ASR.statistics().tasks_waiting: await anyio.sleep(.001)
                await anyio.sleep(.05)
            finally:
                release.set()
        assert threads.asr_wait_s() == 0
        assert threads.ASR.statistics().borrowed_tokens == 0
    anyio.run(scenario)
    assert waits['first'] == waits['reset'] == 0
    assert waits['second'] >= .04


def test_asr_cancellation_and_error_release_slot(monkeypatch):
    import pytest
    monkeypatch.setattr(threads, 'ASR', threads._ASRSlots(1))
    scopes, ran, cancelled_waits = [], [], []
    async def waiting():
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            await anyio.to_thread.run_sync(lambda: ran.append(True), limiter=threads.ASR)
        cancelled_waits.append(threads.asr_wait_s())
    def fail(): raise ValueError('fake decoder failure')
    async def scenario():
        async with threads.ASR:
            async with anyio.create_task_group() as tg:
                tg.start_soon(waiting)
                with anyio.fail_after(3):
                    while not threads.ASR.statistics().tasks_waiting: await anyio.sleep(.001)
                scopes[0].cancel()
        assert not ran and cancelled_waits == [0]
        with pytest.raises(ValueError):
            await anyio.to_thread.run_sync(fail, limiter=threads.ASR)
        assert threads.asr_wait_s() == 0
        await anyio.to_thread.run_sync(lambda: None, limiter=threads.ASR)
        assert threads.asr_wait_s() == 0
        assert threads.ASR.statistics().borrowed_tokens == 0
    anyio.run(scenario)


def test_cancelling_running_asr_does_not_release_native_work_early(monkeypatch):
    import threading
    monkeypatch.setattr(threads, 'ASR', threads._ASRSlots(1))
    entered, release, short_entered = threading.Event(), threading.Event(), threading.Event()
    scopes = []
    def long_work():
        entered.set()
        assert release.wait(5)
    async def long_call():
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            await anyio.to_thread.run_sync(long_work, limiter=threads.ASR)
    async def short_call():
        await anyio.to_thread.run_sync(short_entered.set, limiter=threads.ASR)
    async def scenario():
        async with anyio.create_task_group() as tg:
            try:
                tg.start_soon(long_call)
                with anyio.fail_after(3):
                    while not entered.is_set(): await anyio.sleep(.001)
                    scopes[0].cancel()
                    tg.start_soon(short_call)
                    while not threads.ASR.statistics().tasks_waiting: await anyio.sleep(.001)
                assert not short_entered.is_set()
                assert threads.ASR.statistics().borrowed_tokens == 1
            finally:
                release.set()
        assert short_entered.is_set() and threads.ASR.statistics().borrowed_tokens == 0
    anyio.run(scenario)
