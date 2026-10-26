"""Invalid tenant configuration fails at startup without exposing configuration values."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from alldayrec import tenant


SECRET = "synthetic-secret-never-log"
ONE = {"id": "sample", "token": SECRET, "data": "data", "recordings": "recordings"}


@pytest.mark.parametrize("content", [
    b"\xff",
    '[{"token": "' + SECRET + '",',
    json.dumps({"tenants": [ONE]}),
    *[json.dumps(x) for x in (None, True, 12, SECRET)],
    *[json.dumps([x]) for x in (None, True, 12, SECRET, [])],
    *[json.dumps([{k: v for k, v in ONE.items() if k != missing}]) for missing in ONE],
    *[json.dumps([{**ONE, field: value}]) for field in ONE for value in (None, True, 12, [], {})],
    *[json.dumps([{**ONE, "lang": value}]) for value in (True, 12, [], {}, "fr", SECRET)],
    json.dumps([{**ONE, "id": SECRET, "lang": "fr"}]),
])
def test_invalid_config_exits_cleanly(tmp_path, content):
    path = tmp_path / "tenants.json"
    original = content if isinstance(content, bytes) else content.encode()
    path.write_bytes(original)
    path.chmod(0o600)
    result = subprocess.run(
        [sys.executable, "-c", "from pathlib import Path; from alldayrec.tenant import load; load(Path(__import__('sys').argv[1]))", str(path)],
        capture_output=True, text=True, timeout=10,
        env={**os.environ, "PYTHONPATH": str(Path(tenant.__file__).parent.parent)},
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert str(path) in result.stderr
    assert "expected a JSON array of objects" in result.stderr
    assert all(field in result.stderr for field in ONE)
    assert "Traceback" not in output and SECRET not in output
    assert result.stdout == ""
    assert path.read_bytes() == original and path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("lang", [None, "", "zh", "en"])
def test_valid_config_keeps_language_fallback(tmp_path, monkeypatch, lang):
    monkeypatch.setenv("UI_LANG", "en")
    path = tmp_path / "tenants.json"
    path.write_text(json.dumps([{**ONE, "lang": lang, "extra": "ignored"}]))
    path.chmod(0o600)
    loaded, = tenant.load(path)
    assert (loaded.id, loaded.token, loaded.data, loaded.recordings, loaded.lang) == (
        ONE["id"], SECRET, Path("data"), Path("recordings"), lang or "en")
