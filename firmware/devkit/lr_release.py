"""Build, pack and upload recorder firmware to the server, where the app's recorder page offers it (server/firmware.py).
Each variant is built by lr.sh and its model read from that build (CONFIG_BT_DIS_MODEL), so a package goes up only
under the model it was built for. Only from a clean tree (the version names one commit) on origin/main.
    LR_TOKEN=<AUTH_TOKEN> LR_FIRMWARE_TOKEN=<FIRMWARE_TOKEN> \\
    uv run --no-project python lr_release.py sdmod promicro --server URL [--notes TEXT] [--unmerged]
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

D = Path(__file__).resolve().parent
MODELS = {"sdmod": "LR-X1", "promicro": "LR-P1"}  # the boards people have (expansion is a test base for the XIAO)


def git(*a: str) -> str:
    return subprocess.run(["git", "-C", str(D), *a], check=True, capture_output=True, text=True).stdout.strip()


def config(v: str, key: str) -> str | None:
    m = re.search(rf'^CONFIG_{key}="(.*)"$', (D / f"build/lr-{v}/zephyr/.config").read_text(), re.M)
    return m and m.group(1)


def main():
    ap = argparse.ArgumentParser(description="build, pack and upload recorder firmware")
    ap.add_argument("variants", nargs="+", choices=MODELS)
    ap.add_argument("--server", required=True, help="e.g. https://lr.example.com")
    ap.add_argument("--notes", default="", help="what changed, shown in the app (default: the commit's subject)")
    ap.add_argument("--unmerged", action="store_true", help="a commit not on origin/main (a test server)")
    a = ap.parse_args()
    fw_token = os.environ.get("LR_FIRMWARE_TOKEN") or sys.exit("LR_FIRMWARE_TOKEN (the server's FIRMWARE_TOKEN) is not set")
    if subprocess.run(["git", "-C", str(D), "diff", "--quiet", "HEAD", "--", "."]).returncode:
        sys.exit("firmware/devkit has uncommitted changes: a version must name one commit")
    commit, short = git("rev-parse", "HEAD"), git("rev-parse", "--short", "HEAD")
    if not a.unmerged:
        git("fetch", "-q", "origin", "main")
        if subprocess.run(["git", "-C", str(D), "merge-base", "--is-ancestor", commit, "origin/main"]).returncode:
            sys.exit(f"{short} is not on origin/main (--unmerged for a test server)")
    notes = a.notes or git("log", "-1", "--format=%s")
    env = {k: v for k, v in os.environ.items() if k != "LR_LOG"}  # the log build is not for people's boards
    for v in a.variants:
        subprocess.run([str(D / "lr.sh"), "build", v], check=True, env=env)
        subprocess.run([str(D / "lr.sh"), "pkg", v], check=True, env=env)
        model, version = config(v, "BT_DIS_MODEL"), config(v, "BT_DIS_FW_REV_STR") or ""
        if model != MODELS[v]:
            sys.exit(f"{v} built as model {model}, expected {MODELS[v]}")
        if not version.endswith(f"-{short}"):
            sys.exit(f"{v} build carries revision {version}, not HEAD {short}")
        hexfile, pkg = D / f"build/lr-{v}/zephyr/zephyr.hex", D / f"build/lr-{v}/lr-{v}.zip"
        built = dt.datetime.fromtimestamp(hexfile.stat().st_mtime).astimezone().isoformat(timespec="seconds")
        q = urllib.parse.urlencode({"model": model, "version": version, "commit": commit, "built": built, "notes": notes})
        req = urllib.request.Request(f"{a.server.rstrip('/')}/api/firmware?{q}", pkg.read_bytes(), {
            "Content-Type": "application/zip", "Authorization": f"Bearer {os.environ.get('LR_TOKEN', '')}",
            "X-Firmware-Token": fw_token})
        try:
            meta = json.loads(urllib.request.urlopen(req, timeout=60).read())
        except urllib.error.HTTPError as e:
            sys.exit(f"{v}: the server said {e.code} {e.read().decode()}")
        print(f"uploaded {meta['model']} {meta['version']} ({meta['size']} B, sha256 {meta['sha256'][:12]}…)")


if __name__ == "__main__":
    main()
