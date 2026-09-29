"""Volcengine (Ark Agent Plan) doubao-seed-asr-2.0 over its WebSocket binary protocol.

    set -a; . .env; set +a; uv run python volc_asr.py <audio file> [--mode nostream|async] [--pace 0]

transcribe(x, mode) sends 16 kHz int16 PCM in 200 ms packets and returns the utterances of the final
response: [{text, start_ms, end_ms, speaker?}]. nostream answers once per ~15 s of audio with the
high-accuracy result; async answers after every packet. VOLC_ASR_KEY is the Agent Plan API key.
"""
import argparse
import asyncio
import gzip
import json
import os
import subprocess
import sys
import time
import uuid

import numpy as np

URL = "wss://openspeech.bytedance.com/api/v3/plan/sauc/bigmodel_{mode}"
RESOURCE = "volc.seedasr.sauc.duration"
SR = 16000
PACKET_MS = 200

FULL_CLIENT, AUDIO_ONLY, FULL_SERVER, SERVER_ERROR = 0b0001, 0b0010, 0b1001, 0b1111
POS_SEQ, NEG_WITH_SEQ = 0b0001, 0b0011
JSON_, GZIP_ = 0b0001, 0b0001


def frame(msg_type: int, flags: int, seq: int, payload: bytes) -> bytes:
    head = bytes([(0b0001 << 4) | 0b0001, (msg_type << 4) | flags, (JSON_ << 4) | GZIP_, 0])
    body = gzip.compress(payload)
    return head + seq.to_bytes(4, "big", signed=True) + len(body).to_bytes(4, "big") + body


def parse(data: bytes) -> dict:
    msg_type, flags, comp = data[1] >> 4, data[1] & 0x0F, data[2] & 0x0F
    p = 4
    out = {"type": msg_type}
    if msg_type == SERVER_ERROR:
        out["code"] = int.from_bytes(data[4:8], "big")
        p = 8
    elif flags & 0x01:
        out["seq"] = int.from_bytes(data[4:8], "big", signed=True)
        p = 8
    size = int.from_bytes(data[p:p + 4], "big")
    payload = data[p + 4:p + 4 + size]
    if comp == GZIP_:
        payload = gzip.decompress(payload)
    try:
        out["payload"] = json.loads(payload)
    except ValueError:
        out["payload"] = payload.decode(errors="replace")
    return out


async def _run(x: np.ndarray, mode: str, pace_s: float, speakers: bool, log=None) -> tuple[list[dict], dict]:
    import websockets
    headers = {"X-Api-Key": os.environ["VOLC_ASR_KEY"], "X-Api-Resource-Id": RESOURCE,
               "X-Api-Connect-Id": str(uuid.uuid4()), "X-Api-Request-Id": str(uuid.uuid4())}
    req = {"user": {"uid": "live-record"},
           "audio": {"format": "pcm", "codec": "raw", "rate": SR, "bits": 16, "channel": 1},
           "request": {"model_name": "bigmodel", "enable_itn": True, "enable_punc": True, "show_utterances": True,
                       "result_type": "full", **({"enable_speaker_info": True} if speakers else {})}}
    step = SR * PACKET_MS // 1000
    packets = [x[i:i + step].tobytes() for i in range(0, len(x), step)]
    last, responses, t0 = None, 0, time.perf_counter()
    # ping_interval=None: while packets pour in faster than real time the server does not answer pings in time
    async with websockets.connect(URL.format(mode=mode), additional_headers=headers, max_size=None, ping_interval=None) as ws:
        await ws.send(frame(FULL_CLIENT, POS_SEQ, 1, json.dumps(req).encode()))
        first = parse(await ws.recv())
        if first["type"] == SERVER_ERROR:
            raise RuntimeError(f"{first['code']}: {first['payload']}")
        logid = ws.response.headers.get("X-Tt-Logid") if hasattr(ws, "response") else None

        async def reader():
            nonlocal last, responses
            async for data in ws:
                r = parse(data)
                responses += 1
                if r["type"] == SERVER_ERROR:
                    raise RuntimeError(f"{r['code']}: {r['payload']}")
                if isinstance(r.get("payload"), dict) and "result" in r["payload"]:
                    last = r["payload"]
                    if log:
                        log(r)
                if r.get("seq", 0) < 0:  # the answer to the last packet
                    return

        task = asyncio.create_task(reader())
        for i, pk in enumerate(packets, start=2):
            end = i == len(packets) + 1
            await ws.send(frame(AUDIO_ONLY, NEG_WITH_SEQ if end else POS_SEQ, -i if end else i, pk))
            if pace_s:
                await asyncio.sleep(pace_s)
        await asyncio.wait_for(task, timeout=120)
    utts = [{"text": u["text"], "start_ms": u["start_time"], "end_ms": u["end_time"],
             **({"speaker": "SPEAKER_" + str(u["additions"]["speaker_id"])} if u.get("additions", {}).get("speaker_id") is not None else {})}
            for u in (last or {}).get("result", {}).get("utterances", []) if u.get("definite", True)]
    return utts, {"seconds": round(time.perf_counter() - t0, 1), "responses": responses, "logid": logid,
                  "text": (last or {}).get("result", {}).get("text", "")}


def transcribe(x: np.ndarray, mode: str = "nostream", pace_s: float = 0.02, speakers: bool = True):  # 0.02: 10x real time
    return asyncio.run(_run(x, mode, pace_s, speakers))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("--mode", default="nostream", choices=["nostream", "async"])
    ap.add_argument("--pace", type=float, default=0.0, help="seconds between 200 ms packets (0.2 = real time)")
    ap.add_argument("--no-speakers", action="store_true")
    a = ap.parse_args()
    x = np.frombuffer(subprocess.run(["ffmpeg", "-v", "error", "-i", a.audio, "-f", "s16le", "-ac", "1", "-ar", str(SR), "-"],
                                     capture_output=True, check=True).stdout, np.int16)
    utts, info = asyncio.run(_run(x, a.mode, a.pace, not a.no_speakers, log=lambda r: print(json.dumps(r["payload"], ensure_ascii=False)[:200], file=sys.stderr)))
    print(json.dumps(info, ensure_ascii=False))
    for u in utts:
        print(f"{u['start_ms'] / 1000:7.1f} {u['end_ms'] / 1000:7.1f} {u.get('speaker', '-'):>3} {u['text']}")


if __name__ == "__main__":
    main()
