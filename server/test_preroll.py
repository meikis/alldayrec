"""A recorder's pieces are slices of one running encoder (CELT, 10 ms frames, pre-skip 0, as the app's Store.oggOpus
cuts them). Decoded on its own, a piece began up to 26 dB too loud for 10-40 ms: a click at every join, each second
while the app is in front (10-08, LR-P3-2420). Two back-to-back pieces of such a stream: the second one starts as it
goes on; a piece after a gap and an iPhone microphone piece (SILK, 20 ms) decode as before."""
import datetime as dt
import subprocess

import numpy as np

import audio
import db

T = int(dt.datetime(2026, 10, 8, 1, 13).timestamp() * 1000)
lvl = lambda v: 10 * np.log10(np.mean(v.astype(np.float64) ** 2))


def encode(*opts: str) -> list[bytes]:
    """4 s of quiet pink noise (a room, about -55 dBFS) through one libopus encoder: its audio packets."""
    ogg = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anoisesrc=d=4:c=pink:r=16000:a=0.003", "-c:a", "libopus",
                          *opts, "-f", "ogg", "pipe:1"], capture_output=True, check=True).stdout
    return audio.packets(ogg)[2:]  # OpusHead, OpusTags


def test_join(tmp_path):
    audio.DIR = tmp_path
    db.open_db(tmp_path / "t.db")
    rec = encode("-application", "lowdelay", "-frame_duration", "10", "-b:a", "32k")  # the recorder's codec.c
    a, b = audio.ogg(rec[:200]), audio.ogg(rec[200:400])  # 2 s each, back to back
    assert audio.save("LR-P3-test", T, a, "audio/ogg") == audio.save("LR-P3-test", T + 2000, b, "audio/ogg") == 2.0
    row = lambda t: db.conn().execute("SELECT * FROM pieces WHERE start_ms = ?", (t,)).fetchone()
    x = audio.samples(row(T + 2000))
    assert len(x) == 200 * 160, len(x)
    head, mid = lvl(x[:640]), lvl(x[8000:24000])  # its first 40 ms, its middle second
    assert abs(head - mid) < 2, f"the join: first 40 ms {head - mid:+.1f} dB against the middle"
    assert abs(row(T + 2000)["rms"] - row(T)["rms"]) < 1, "the stored level is the piece's, not its start's"
    cold = audio.decode(b)
    assert lvl(cold[:640]) - mid > 6, "on its own the same piece starts loud (what the check above guards against)"
    # after a gap (a lost frame, another recording): nothing to go on from, decoded as before
    assert audio.save("LR-P3-test", T + 4010, b, "audio/ogg") == 2.0
    assert np.array_equal(audio.samples(row(T + 4010)), cold)
    # the iPhone's own microphone: SILK, 20 ms frames, its own pieces untouched
    mic = encode("-application", "voip", "-frame_duration", "20", "-b:a", "16k")
    c, d = audio.ogg(mic[:50], 20), audio.ogg(mic[50:100], 20)
    assert audio.save("iPhone", T, c, "audio/ogg") == audio.save("iPhone", T + 1000, d, "audio/ogg") == 1.0
    assert np.array_equal(audio.samples(db.conn().execute("SELECT * FROM pieces WHERE device = 'iPhone' AND start_ms = ?", (T + 1000,)).fetchone()),
                          audio.decode(d))
