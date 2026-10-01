# live-record

A continuous audio stream in, conversations out, within a minute.

The phone app uploads Opus pieces; the server keeps them on disk as a queue and feeds them in time order, detects speech locally (FireRedVAD), sends each new stretch to the ASR engine every 20-30 s to get sentences with anonymous speaker labels, decides locally who is the owner and keeps conversation-local speaker ids (ReDimNet2 voiceprints), finalizes sentences once, closes a conversation after a silence and summarizes it.

Only speech detection and speaker identity run on this machine; sentence splitting, diarization and transcription come from the ASR engine behind `ASR_SERVICE_URL`.

```
cp .env.example .env   # fill in
cd server && uv sync && set -a && . ../.env && set +a && uv run python server.py
```

Page: http://127.0.0.1:8770/ . Data: `LIVE_DATA/live.db` (SQLite: sentences, conversations, summaries, voices, the upload queue; schema in `server/db.py`), `<date>.passes.jsonl` (raw engine output per pass).

Tests and tooling in `server/`: `test_live.py`, `replay_case.py` (replay a case through candidate pipelines), `score_case.py` (score against a case's `gt.jsonl`), `eval_all.sh`.
