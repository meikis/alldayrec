"""The calls table (#155): every ASR try and LLM call with its time, failure and size, never its text; the summary by
kind and engine; the StepFun balance and how long it lasts."""
import datetime as dt
import io
import json
import tempfile
import urllib.error
from pathlib import Path

import numpy as np
import pytest

import db
import server
import summary


def test_calls(monkeypatch):
    db.open_db(Path(tempfile.mkdtemp()) / "t.db")
    lo = (dt.datetime.now() - dt.timedelta(seconds=1)).isoformat()

    def bad(x, partial=None):
        raise RuntimeError("stepfun sse HTTP 402: balance")
    ok = lambda x, partial=None: [{"text": "秘密", "start_ms": 0, "end_ms": 500}]
    monkeypatch.setattr(server, "PROVIDERS", {**server.PROVIDERS, "a": bad, "b": ok})
    monkeypatch.setattr(server, "SQUEEZE_KEEP_S", 0.0)
    monkeypatch.setattr(server, "ENGINE_FALLBACK", "b")
    server.engine_segs(np.zeros(16000 * 30, np.int16), [], None, "a", "draft", "LR-P3-116C")
    rows = [dict(r) for r in db.conn().execute("SELECT * FROM calls ORDER BY rowid")]
    assert [(r["kind"], r["engine"], r["device"], r["audio_s"], r["sent_s"]) for r in rows] == \
        [("draft", "a", "LR-P3-116C", 30.0, 30.0), ("draft", "b", "LR-P3-116C", 30.0, 30.0)]
    assert "402" in rows[0]["error"] and rows[1]["error"] is None

    answers = [urllib.error.HTTPError("u", 502, "Bad Gateway", {}, io.BytesIO(b"echoes the prompt")),
               io.BytesIO(json.dumps({"choices": [{"message": {"content": "{}"}}], "usage": {"prompt_tokens": 900, "completion_tokens": 40}}).encode())]

    def urlopen(req, timeout):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a
    monkeypatch.setattr(summary.urllib.request, "urlopen", urlopen)
    monkeypatch.setenv("LLM_MODEL", "m")
    monkeypatch.setenv("LLM_BASE_URL", "http://relay")
    monkeypatch.setenv("LLM_API_KEY", "k")
    with pytest.raises(urllib.error.HTTPError):
        summary.complete([{"role": "user", "content": "秘密"}], kind="recap")
    summary.complete([{"role": "user", "content": "秘密"}], kind="recap")
    assert "秘密" not in json.dumps([dict(r) for r in db.conn().execute("SELECT * FROM calls")]), "never what was said"
    assert "echoes" not in json.dumps([dict(r) for r in db.conn().execute("SELECT * FROM calls")]), "nor a response body"

    got = {(c["kind"], c["engine"]): c for c in db.calls(lo)}
    assert got[("draft", "a")]["failed"] == {"402": 1} and got[("draft", "a")]["ms50"] is None
    assert got[("draft", "b")]["n"] == 1 and got[("draft", "b")]["sent_h"] == round(30 / 3600, 2)
    r = got[("recap", "m")]
    assert (r["n"], r["failed"], r["prompt_tokens"], r["completion_tokens"]) == (2, {"502": 1}, 900, 40) and r["ms50"] is not None
    assert db.calls(dt.datetime.now().isoformat()) == []

    monkeypatch.setattr(db, "PATH", None)
    db.add_call("final", "x", None, 1)  # a script on a read-only copy: nothing kept, nothing raised


def test_balance():
    db.open_db(Path(tempfile.mkdtemp()) / "t.db")
    assert db.balance("stepfun") is None
    now = dt.datetime.now()
    for h, cny in ((48, 30.0), (36, 28.0), (24, 26.0), (23, 46.0), (0, 42.0)):  # ¥2 a 12 h, a ¥20 top-up, ¥4 since
        db.conn().execute("INSERT INTO balances VALUES (?, 'stepfun', ?)", ((now - dt.timedelta(hours=h)).isoformat(timespec="seconds"), cny))
    b = db.balance("stepfun")
    assert (b["cny"], b["per_day"], b["days_left"]) == (42.0, 4.0, 10.5), b  # ¥8 in 2 days, the top-up left out
    db.conn().execute("DELETE FROM balances WHERE at < ?", ((now - dt.timedelta(hours=1)).isoformat(),))
    assert db.balance("stepfun")["per_day"] is None, "one reading: no rate yet"
