"""Blocking work runs in threads by kind, each kind under its own cap: anyio.to_thread.run_sync(f, *args, limiter=X).
One kind piling up (a backlog's voiceprints) never makes another wait (an upload's decode): each waits only for its own.

Measured on a 16-core desktop (6 s voiceprints of ReDimNet2, an upload's 1 s piece decoded every 0.25 s meanwhile):
voiceprints top out at ~1.8/s in one process however many run at once (the GIL); with no cap and torch's own threads
(as before) they took 19-23 cores, an upload's decode waited p95 ~0.5 s and a 1 s VAD push on the event loop ~1 s;
torch at 4 threads and 2 calls: the same ~1.7/s on ~7.6 cores, decode p95 76 ms, VAD p95 15 ms, the loop's lag 2 ms.
onnxruntime, 2 calls of infer.THREADS (INFER_THREADS, 4) threads each: 200 voiceprints of 0.3-8 s in 60 s on
301 CPU s (torch 4 x 2: 80 s, 601 CPU s); 32 calls at once took 7x the CPU for no more of them, and 3.8 GB."""
import os
import time
from contextvars import ContextVar

import anyio

_asr_wait_s = ContextVar("asr_wait_s", default=0.0)


class _ASRSlots:
    """The same capacity cap, with admission delay local to the caller, not thread startup or decode time."""
    def __init__(self, tokens: int):
        self._limiter = anyio.CapacityLimiter(tokens)

    async def __aenter__(self):
        _asr_wait_s.set(0.0)
        busy = self._limiter.available_tokens <= 0
        started = time.perf_counter()
        await self._limiter.acquire()
        _asr_wait_s.set(time.perf_counter() - started if busy else 0.0)
        return self

    async def __aexit__(self, *exc):
        self._limiter.release()
        if exc[0] is not None:
            _asr_wait_s.set(0.0)

    def __getattr__(self, name):
        return getattr(self._limiter, name)

    @property
    def total_tokens(self):
        return self._limiter.total_tokens

    @total_tokens.setter
    def total_tokens(self, value):
        self._limiter.total_tokens = value


def asr_wait_s() -> float:
    return _asr_wait_s.get()


def clear_asr_wait():
    _asr_wait_s.set(0.0)


INFER = anyio.CapacityLimiter(int(os.environ.get("INFER_CALLS", "2")))  # ReDimNet2 voiceprints (infer.py), the diarist's clustering
ASR = _ASRSlots(int(os.environ.get("ASR_CALLS", "1")))  # local_asr decodes, at the same cap as before
MEDIA = anyio.CapacityLimiter(int(os.environ.get("MEDIA_CALLS", "16")))  # decoding pieces, encoding windows (ffmpeg, base64)
DB = anyio.CapacityLimiter(int(os.environ.get("DB_CALLS", "8")))  # reads over the whole store: speaker groups, MCP, ask's tools
VAD = anyio.CapacityLimiter(2)  # the segmenters' VAD (Segmenter.apush), one call per segmenter at a time: ~1.6 ms of a core per s of audio


def first(e: BaseException) -> BaseException:
    """A TaskGroup's error as the first one in it (what the logs and the calls table say; an ExceptionGroup's own text
    says nothing)."""
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    return e
