"""Blocking work runs in threads by kind, each kind under its own cap: anyio.to_thread.run_sync(f, *args, limiter=X).
One kind piling up (a backlog's voiceprints) never makes another wait (an upload's decode): each waits only for its own.

Measured on the deploy host (#200: 6 s voiceprints of ReDimNet2, an upload's 1 s piece decoded every 0.25 s meanwhile):
voiceprints top out at ~1.8/s in one process however many run at once (the GIL); with no cap and torch's own threads
(as before) they took 19-23 cores, an upload's decode waited p95 ~0.5 s and a 1 s VAD push on the event loop ~1 s;
OMP_NUM_THREADS=4 and TORCH 2: the same ~1.7/s on ~7.6 cores, decode p95 76 ms, VAD p95 15 ms, the loop's lag 2 ms."""
import os

os.environ.setdefault("OMP_NUM_THREADS", "4")  # torch's threads in every thread that calls it; read when torch loads (later)

import anyio  # noqa: E402

TORCH = anyio.CapacityLimiter(int(os.environ.get("TORCH_CALLS", "2")))  # ReDimNet2 voiceprints, the diarist's clustering
MEDIA = anyio.CapacityLimiter(int(os.environ.get("MEDIA_CALLS", "16")))  # decoding pieces, encoding windows (ffmpeg, base64)
DB = anyio.CapacityLimiter(int(os.environ.get("DB_CALLS", "8")))  # reads over the whole store: speaker groups, MCP, ask's tools


def first(e: BaseException) -> BaseException:
    """A TaskGroup's error as the first one in it (what the logs and the calls table say; an ExceptionGroup's own text
    says nothing)."""
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    return e
