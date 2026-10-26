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
