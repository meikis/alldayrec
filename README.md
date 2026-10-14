# live-record

A continuous audio stream in, conversations out, within a minute.

The phone app uploads Opus pieces; the server keeps them on disk as a queue and feeds them in time order, detects speech locally (FireRedVAD), and transcribes in two layers (issue #9). The final layer always runs: pieces of up to 5 min, cut in pauses, go to the ASR engine once each; it decides locally who is the owner and keeps conversation-local speaker ids (ReDimNet2 voiceprints), closes a conversation after a silence and summarizes it. While a page shows today, the draft layer also sends each stretch at its pause, so text shows within seconds; drafts are kept (table `drafts`) and the page shows them only where final rows do not cover the time yet.

Only speech detection and speaker identity run on this machine, both on onnxruntime (`server/infer.py`, no torch; the model files in `MODELS_DIR`, default `/opt/lr-models`, pinned by sha256: `uv run --group export python export_onnx.py` makes them); sentence splitting, diarization and transcription come from the ASR engine behind `ASR_SERVICE_URL`.

```
cp .env.example .env   # fill in
echo '[{"id": "main", "token": "", "data": "'$PWD/data'", "recordings": "'$PWD/recordings'"}]' > tenants.json && chmod 600 tenants.json
cd server && uv sync && set -a && . ../.env && set +a && uv run python server.py
```

`tenants.json` (beside `.env`) is who the server serves: one tenant per person, each its own data and recordings dir and its own token; a lone tenant with an empty token runs without auth (`server/tenant.py`).

Page: http://127.0.0.1:8770/ . Data, per tenant: `<data>/live.db` (SQLite: sentences, drafts, conversations, summaries, voices, the upload queue, the recorders' event logs and status reports — page `/device/<name>`; schema in `server/db.py`), `<date>.passes.jsonl` (raw engine output per pass).

Tests and tooling in `server/`: `test_live.py`, `replay_case.py` (replay a case through candidate pipelines), `score_case.py` (score against a case's `gt.jsonl`), `eval_all.sh`.

Android app releases: raise `version` in `app/pubspec.yaml` (the build number after `+` must go up), build in a clean checkout of main (`cd app && ANDROID_HOME=~/Library/Android/sdk flutter build apk --release`, signed with `~/.android/live-record-debug.keystore` like every build before), then `server/apk_release.sh app/build/app/outputs/flutter-apk/app-release.apk`. It puts the APK at the main instance's `/app.apk` by a rename (a download under way keeps the old file), keeps the one it replaces as `data/live-record.apk.prev-<version>`, refuses one that is not newer, and checks `/api/app/latest`. That route reads the version from the APK itself (`server/apk.py`); Android phones ask for it at start and, back in front, once a day, and offer the update at the top of the home page.
