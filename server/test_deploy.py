"""deploy.sh installs nothing anywhere while an instance's tenants.json is one its new server would not start with."""
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
GOOD = json.dumps([{"id": "a", "token": "t", "data": "/x", "recordings": "/y"}])


def instance(tmp: Path, name: str, tenants: str | None = None, mode: int = 0o600) -> str:
    d = tmp / name
    (d / "server/.venv/bin").mkdir(parents=True)
    py = d / "server/.venv/bin/python"  # this venv's (a symlink would lose its packages)
    py.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    py.chmod(0o755)
    if tenants is not None:
        (d / "tenants.json").write_text(tenants)
        (d / "tenants.json").chmod(mode)
    return f"{d}:lr-{name}"


def deploy(tmp: Path, *instances: str) -> subprocess.CompletedProcess:
    tgz = tmp / "release.tgz"  # as deploy.sh packs it
    subprocess.run(["tar", "-czf", tgz, "-C", HERE, "./tenant.py"], check=True)
    return subprocess.run(["bash", HERE / "deploy.sh", "remote", "deploy", *instances], env={**os.environ, "LR_TGZ": str(tgz)},
                          capture_output=True, text=True)


def test_stops_before_installing(tmp_path):
    good = instance(tmp_path, "good", GOOD)
    for bad, why in ((instance(tmp_path, "none"), "no tenants file"), (instance(tmp_path, "open", GOOD, 0o644), "chmod 600"),
                     (instance(tmp_path, "broken", "["), "JSONDecodeError")):
        r = deploy(tmp_path, good, bad)  # the good one first: not installed either
        assert r.returncode == 1 and why in r.stderr and "tenants.sh migrate first" in r.stdout and "nothing installed" in r.stdout, r.stdout + r.stderr
        assert not list(tmp_path.glob("*/backup-*"))
    r = deploy(tmp_path, good)  # passes the check, then fails installing: this fake instance has no server
    assert "migrate first" not in r.stdout and list(tmp_path.glob("good/backup-deploy-*")), r.stdout + r.stderr
