# live-record

A continuous audio stream in, conversations out, within a minute.

The phone app uploads Opus pieces; the server keeps them on disk as a queue and feeds them in time order, detects speech locally (FireRedVAD), and transcribes in two layers (issue #9). The final layer always runs: pieces of up to 5 min, cut in pauses, go to the ASR engine once each; it decides locally who is the owner and keeps conversation-local speaker ids (ReDimNet2 voiceprints), closes a conversation after a silence and summarizes it. While a page shows today, the draft layer also sends each stretch at its pause, so text shows within seconds; drafts are kept (table `drafts`) and the page shows them only where final rows do not cover the time yet.

Only speech detection and speaker identity run on this machine; sentence splitting, diarization and transcription come from the ASR engine behind `ASR_SERVICE_URL`.

```
cp .env.example .env   # fill in
cd server && uv sync && set -a && . ../.env && set +a && uv run python server.py
```

Page: http://127.0.0.1:8770/ . Data: `LIVE_DATA/live.db` (SQLite: sentences, drafts, conversations, summaries, voices, the upload queue, the recorders' event logs and status reports — page `/device/<name>`; schema in `server/db.py`), `<date>.passes.jsonl` (raw engine output per pass).

Tests and tooling in `server/`: `test_live.py`, `replay_case.py` (replay a case through candidate pipelines), `score_case.py` (score against a case's `gt.jsonl`), `eval_all.sh`.
