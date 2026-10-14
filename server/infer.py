"""Speech detection and voiceprints on onnxruntime, in this process, without torch (#219): FireRedVAD Stream-VAD and
ReDimNet2 B6, the same models as before (on the main instance's audio: the same utterances and squeezed seconds, cosine
>= 0.9999998, the same diarist labels). The files live in MODELS_DIR, each pinned by its sha256 below; check() stops
the server at start when one is missing or different, there is no fallback. Where they come from and how to make
them: export_onnx.py.
"""
import hashlib
import os
import struct
from functools import cache
from pathlib import Path

import kaldi_native_fbank as knf
import numpy as np
import onnxruntime as ort

from vad_postprocessor import StreamVadPostprocessor

DIR = Path(os.environ.get("MODELS_DIR") or "/opt/lr-models")
THREADS = int(os.environ.get("INFER_THREADS", "4"))  # onnxruntime threads per voiceprint call (threads.INFER: calls at once)
VAD, CMVN, EMB = "fireredvad-stream-vad.onnx", "fireredvad-cmvn.ark", "redimnet2-b6.onnx"
MODELS = {  # file: sha256 (sources and licenses: export_onnx.py)
    VAD: "b3c97836130dc34fc32d56fab551e88cf9454511de2b3c250a5e6578dee74b93",
    CMVN: "c87f6f13edf0f0ec7535ddfc9cc3387d9268cb234b70182d566c5e2edf3ca473",
    EMB: "0a17d6472e4e7a31ffe254858574f6f1203acd5a5fa9a153a12556f4aaa72d81",
}


def check(d: Path = DIR):
    """SystemExit naming every model file that is missing in d or not the pinned one."""
    bad = [f"{d / n} {'is missing' if not (d / n).is_file() else 'has another sha256'}" for n, h in MODELS.items()
           if not (d / n).is_file() or hashlib.sha256((d / n).read_bytes()).hexdigest() != h]
    if bad:
        raise SystemExit(f"models: {'; '.join(bad)} (make them: uv run --group export python export_onnx.py {d})")


def session(name: str, threads: int) -> ort.InferenceSession:
    o = ort.SessionOptions()
    o.intra_op_num_threads, o.inter_op_num_threads = threads, 1
    return ort.InferenceSession(str(DIR / name), o, providers=["CPUExecutionProvider"])


def cmvn(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """Means and inverse deviations from a Kaldi binary stats matrix (2 x dim+1, doubles), as FireRedVAD computes them."""
    b = path.read_bytes()
    if b[:6] != b"\0BDM \4" or b[10:11] != b"\4":
        raise ValueError(f"{path}: not a Kaldi binary double matrix")
    rows, cols = struct.unpack_from("<i", b, 6)[0], struct.unpack_from("<i", b, 11)[0]
    s = np.frombuffer(b, "<f8", rows * cols, 15).reshape(rows, cols)
    mean = s[0, :-1] / s[0, -1]
    return mean, 1.0 / np.sqrt(np.maximum(s[1, :-1] / s[0, -1] - mean * mean, 1e-20))


@cache
def _vad() -> tuple[ort.InferenceSession, np.ndarray, np.ndarray]:
    return session(VAD, 1), *cmvn(DIR / CMVN)  # one session for every stream: run() is thread-safe, a stream's state is its caches


def fbank(x: np.ndarray) -> np.ndarray:
    """80 log-mel frames (25 ms / 10 ms, snip edges, no dither) of int16 samples, CMVN applied: FireRedVAD's features."""
    o = knf.FbankOptions()
    o.frame_opts.samp_freq, o.frame_opts.dither, o.frame_opts.snip_edges, o.mel_opts.num_bins = 16000, 0, True, 80
    f = knf.OnlineFbank(o)
    f.accept_waveform(16000, x.tolist())
    _, mean, istd = _vad()
    return ((np.vstack([f.get_frame(i) for i in range(f.num_frames_ready)]) - mean) * istd).astype(np.float32)


class StreamVad:
    """FireRedStreamVad as the Segmenter uses it (detect_chunk, postprocessor) on onnxruntime: the 8 FSMN layers' caches
    carry the stream from one chunk to the next (zeros at the start, the same as none)."""

    def __init__(self):
        self.postprocessor = StreamVadPostprocessor(5, 0.5, 5, 8, 2000, 20)  # FireRedStreamVadConfig()'s defaults
        self.caches = np.zeros((8, 1, 128, 19), np.float32)

    def detect_chunk(self, x: np.ndarray) -> list:
        probs, self.caches = _vad()[0].run(None, {"feat": fbank(x)[None], "caches_in": self.caches})
        return [self.postprocessor.process_one_frame(p) for p in probs.reshape(-1).tolist()]


def embedder():
    """ReDimNet2 B6: int16 audio -> unit voiceprint (192)."""
    s = session(EMB, THREADS)

    def embed(x: np.ndarray) -> np.ndarray:
        e = s.run(None, {"wav": (x.astype(np.float32) / 32768)[None]})[0][0]
        return e / np.linalg.norm(e)
    return embed
