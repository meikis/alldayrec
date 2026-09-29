# live-record

A continuous audio stream in, conversations out, within a minute.

The recorder writes 30-minute Opus files; the server follows the file being written, detects speech locally (FireRedVAD), sends each new stretch to a file-mode ASR engine every 20 s to get sentences with word times and anonymous speaker labels, decides locally who is the owner and keeps conversation-local speaker ids (CAM++ voiceprints), finalizes sentences once, and closes a conversation after 120 s of silence.

Only speech detection and speaker identity run on this machine; sentence splitting, diarization and transcription come from the ASR engine behind `ASR_SERVICE_URL`.

```
cp .env.example .env   # fill in
cd server && uv sync && set -a && . ../.env && set +a && uv run python server.py
```

Page: http://127.0.0.1:8770/ . Data: `LIVE_DATA/<date>.final.jsonl` (sentences), `<date>.conversations.jsonl`.

Tests and tooling in `server/`: `test_live.py`, `replay_case.py` (replay a case through candidate pipelines), `score_case.py` (score against a case's `gt.jsonl`), `feed.py` (replay recordings at a chosen speed), `eval_all.sh`.
