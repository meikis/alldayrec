"""The server's store: one SQLite file in LIVE_DATA. Audio (uploaded pieces, voice clips) stays on disk; the passes
trace stays a jsonl log. One connection per thread (the summary and speaker work runs in threads), WAL so reads
never wait for a write; every multi-step change is one `tx()`.
"""
import datetime as dt
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS sentences (       -- final rows; a finalization pass replaces the ones starting in [from, to)
    id TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL, speaker TEXT, speaker_confidence REAL, text TEXT,
    source TEXT, engine TEXT, shown_at TEXT, dictation TEXT);  -- dictation: the app dictated into (history rows)
CREATE INDEX IF NOT EXISTS sentences_start ON sentences(start);
CREATE TABLE IF NOT EXISTS drafts (          -- the draft layer's rows (only while a page watches); kept after final rows cover their time
    id TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL, speaker TEXT, speaker_confidence REAL, text TEXT,
    source TEXT, engine TEXT, shown_at TEXT);
CREATE INDEX IF NOT EXISTS drafts_start ON drafts(start);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY, start TEXT NOT NULL, end TEXT NOT NULL, participants TEXT NOT NULL, speech_s REAL,
    sentences INTEGER NOT NULL, kind TEXT, source TEXT, dictation_share REAL, device TEXT);  -- device: its sentences' source
CREATE INDEX IF NOT EXISTS conversations_start ON conversations(start);
CREATE TABLE IF NOT EXISTS summaries (        -- body: the summary as the API returns it
    conversation TEXT PRIMARY KEY, status TEXT NOT NULL, prompt_version INTEGER, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS voices (           -- one label of one conversation; label and e NULL: no recording, not retried
    conv TEXT NOT NULL, label TEXT, device TEXT, n INTEGER, speech_s REAL, k INTEGER, e BLOB,
    clip TEXT, clip_start TEXT, clip_end TEXT, clip_text TEXT, UNIQUE (conv, label));
CREATE TABLE IF NOT EXISTS apart (voice TEXT NOT NULL, grp TEXT NOT NULL, PRIMARY KEY (voice, grp));
CREATE TABLE IF NOT EXISTS people (name TEXT PRIMARY KEY, n INTEGER NOT NULL, e BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS pieces (           -- uploaded audio pieces, the ingest queue; size bytes at pos of path (the
    device TEXT NOT NULL, start_ms INTEGER NOT NULL, seconds REAL NOT NULL, path TEXT NOT NULL,  -- hour file, relative
    pos INTEGER, size INTEGER, n INTEGER, PRIMARY KEY (start_ms, device));                      -- to the recordings dir;
                                                                                                -- n: samples the client wrote)
CREATE INDEX IF NOT EXISTS pieces_device ON pieces(device, start_ms);  -- each device's stream and backlog
CREATE TABLE IF NOT EXISTS timelines (conversation TEXT PRIMARY KEY, body TEXT NOT NULL);  -- the live analysis as it stood at the close
CREATE TABLE IF NOT EXISTS feedback (conv TEXT NOT NULL, at TEXT NOT NULL, text TEXT NOT NULL);  -- what the owner says a summary got wrong, read back in batches
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);  -- per device: finalized_to:<d>, draft_to:<d>, open_conversation:<d>, end_at:<d>, live:<d>
"""

PATH: Path | None = None
_local = threading.local()


def open_db(path: Path):
    global PATH
    PATH = path
    _local.c = None  # this thread's connection was to the old path
    conn().executescript(SCHEMA)
    if "device" not in {r[1] for r in conn().execute("PRAGMA table_info(conversations)")}:  # 10-02: one pipeline per device
        conn().execute("ALTER TABLE conversations ADD COLUMN device TEXT")
    if "pos" not in {r[1] for r in conn().execute("PRAGMA table_info(pieces)")}:  # 10-04: pieces in hour files (pack_pieces.py)
        conn().execute("ALTER TABLE pieces ADD COLUMN pos INTEGER")
        conn().execute("ALTER TABLE pieces ADD COLUMN size INTEGER")
    if "n" not in {r[1] for r in conn().execute("PRAGMA table_info(pieces)")}:  # 10-04: iPhone pieces say how many samples they hold
        conn().execute("ALTER TABLE pieces ADD COLUMN n INTEGER")


def conn() -> sqlite3.Connection:
    c = getattr(_local, "c", None)
    if c is None:
        c = _local.c = sqlite3.connect(PATH, isolation_level=None, timeout=30)  # autocommit outside tx()
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
    return c


@contextmanager
def tx():
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
    except BaseException:
        c.execute("ROLLBACK")
        raise
    c.execute("COMMIT")


def vec(b: bytes | None) -> np.ndarray | None:
    return None if b is None else np.frombuffer(b, np.float32)


def blob(e) -> bytes:
    return np.asarray(e, np.float32).tobytes()


# sentences

S_COLS = ("id", "start", "end", "speaker", "speaker_confidence", "text", "source", "engine", "shown_at", "dictation")


def _sentence(r: sqlite3.Row) -> dict:
    return {k: r[k] for k in S_COLS if r[k] is not None} | {"state": "final"}


def sentences(lo: str, hi: str, inclusive: bool = False, source: str | None = None) -> list[dict]:
    """Rows starting in [lo, hi), or [lo, hi] with inclusive (a conversation's own rows); of one source when given."""
    op = "<=" if inclusive else "<"
    return [_sentence(r) for r in conn().execute(f"SELECT * FROM sentences WHERE start >= ? AND start {op} ? AND (? IS NULL OR source = ?) ORDER BY start",
                                                 (lo, hi, source, source))]


def replace_sentences(c: sqlite3.Connection, lo: str, hi: str, rows: list[dict], source: str):
    """A device's pass replaces only that device's rows: another device may have recorded the same minutes."""
    c.execute("DELETE FROM sentences WHERE start >= ? AND start < ? AND source = ?", (lo, hi, source))
    insert_sentences(c, rows)


def insert_sentences(c: sqlite3.Connection, rows: list[dict]):
    c.executemany(f"INSERT INTO sentences ({','.join(S_COLS)}) VALUES ({','.join('?' * len(S_COLS))})",
                  [tuple(r.get(k) for k in S_COLS) for r in rows])


D_COLS = S_COLS[:-1]  # a draft has no dictation


def drafts(lo: str, hi: str | None = None, source: str | None = None) -> list[dict]:
    """Draft rows starting in [lo, hi) (no upper bound when hi is None), of one source when given."""
    return [{k: r[k] for k in D_COLS if r[k] is not None} | {"state": "draft"}
            for r in conn().execute("SELECT * FROM drafts WHERE start >= ? AND (? IS NULL OR start < ?) AND (? IS NULL OR source = ?) ORDER BY start",
                                    (lo, hi, hi, source, source))]


def insert_drafts(c: sqlite3.Connection, rows: list[dict]):
    c.executemany(f"INSERT INTO drafts ({','.join(D_COLS)}) VALUES ({','.join('?' * len(D_COLS))})",
                  [tuple(r.get(k) for k in D_COLS) for r in rows])


def has_text(lo: str, hi: str, q: str, source: str | None = None) -> bool:
    return conn().execute("SELECT 1 FROM sentences WHERE start >= ? AND start <= ? AND (? IS NULL OR source = ?) AND instr(text, ?) LIMIT 1",
                          (lo, hi, source, source, q)).fetchone() is not None


# conversations and summaries

C_COLS = ("id", "start", "end", "participants", "speech_s", "sentences", "kind", "source", "dictation_share", "device")


def _conversation(r: sqlite3.Row) -> dict:
    d = {k: r[k] for k in C_COLS if r[k] is not None}
    return {**d, "participants": json.loads(d["participants"]), "state": "closed"}


def insert_conversation(c: sqlite3.Connection, conv: dict):
    # OR REPLACE: a same-second id would otherwise abort the finalization transaction and stop the pipeline
    c.execute(f"INSERT OR REPLACE INTO conversations ({','.join(C_COLS)}) VALUES ({','.join('?' * len(C_COLS))})",
              tuple(json.dumps(conv[k], ensure_ascii=False) if k == "participants" else conv.get(k) for k in C_COLS))


def conversation(conv_id: str) -> dict | None:
    r = conn().execute("SELECT * FROM conversations WHERE id = ?", (conv_id,)).fetchone()
    return _conversation(r) if r else None


def conversations(lo: str | None = None, hi: str | None = None) -> list[dict]:
    """Closed conversations with their summary (None when there is none): all newest first, or [lo, hi) by start oldest first."""
    q, args = "SELECT c.*, s.body FROM conversations c LEFT JOIN summaries s ON s.conversation = c.id", ()
    if lo:
        q, args = q + " WHERE c.start >= ? AND c.start < ?", (lo, hi)
    return [{**_conversation(r), "summary": json.loads(r["body"]) if r["body"] else None}
            for r in conn().execute(q + (" ORDER BY c.start" if lo else " ORDER BY c.start DESC"), args)]


def summary(conv_id: str) -> dict | None:
    r = conn().execute("SELECT body FROM summaries WHERE conversation = ?", (conv_id,)).fetchone()
    return json.loads(r["body"]) if r else None


def put_summary(s: dict, c: sqlite3.Connection | None = None):
    (c or conn()).execute("INSERT OR REPLACE INTO summaries VALUES (?, ?, ?, ?)",
                          (s["conversation"], s["status"], s.get("prompt_version"), json.dumps(s, ensure_ascii=False)))


def timeline(conv_id: str) -> dict | None:
    r = conn().execute("SELECT body FROM timelines WHERE conversation = ?", (conv_id,)).fetchone()
    return json.loads(r["body"]) if r else None


def put_timeline(conv_id: str, body: dict, c: sqlite3.Connection | None = None):
    (c or conn()).execute("INSERT OR REPLACE INTO timelines VALUES (?, ?)", (conv_id, json.dumps(body, ensure_ascii=False)))


def feedback(conv_id: str | None = None) -> list[dict]:
    """Newest first; every conversation's when none is given."""
    q = "SELECT * FROM feedback" + (" WHERE conv = ?" if conv_id else "") + " ORDER BY at DESC"
    return [dict(r) for r in conn().execute(q, (conv_id,) if conv_id else ())]


def add_feedback(conv_id: str, text: str):
    conn().execute("INSERT INTO feedback VALUES (?, ?, ?)", (conv_id, dt.datetime.now().isoformat(timespec="seconds"), text))


# voices, apart pins, people bank

V_COLS = ("conv", "label", "device", "n", "speech_s", "k", "e", "clip", "clip_start", "clip_end", "clip_text")


def voices() -> list[dict]:
    return [{k: r[k] for k in V_COLS if r[k] is not None} | {"e": vec(r["e"])}
            for r in conn().execute("SELECT * FROM voices WHERE e IS NOT NULL")]


def voiced_conversations() -> set[str]:
    return {r[0] for r in conn().execute("SELECT DISTINCT conv FROM voices")}


def insert_voices(c: sqlite3.Connection, vs: list[dict]):
    c.executemany(f"INSERT OR REPLACE INTO voices ({','.join(V_COLS)}) VALUES ({','.join('?' * len(V_COLS))})",
                  [tuple(blob(v[k]) if k == "e" and v.get(k) is not None else v.get(k) for k in V_COLS) for v in vs])


def apart() -> set[tuple[str, str]]:
    return {(r[0], r[1]) for r in conn().execute("SELECT voice, grp FROM apart")}


def people() -> dict[str, tuple[int, np.ndarray]]:
    return {r["name"]: (r["n"], vec(r["e"])) for r in conn().execute("SELECT * FROM people")}


# pieces and state

def kv(key: str):
    r = conn().execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return json.loads(r[0]) if r else None


def put_kv(key: str, value, c: sqlite3.Connection | None = None):
    (c or conn()).execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, json.dumps(value, ensure_ascii=False)))
