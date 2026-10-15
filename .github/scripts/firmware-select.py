#!/usr/bin/env python3
"""Select affected firmware families without adding another Actions dependency."""
import json
import os
import subprocess
from pathlib import Path

FORMAL_VARIANTS = ["sdmod", "promicro", "promicro-p2", "promicro-p3"]
FAMILIES = ["devkit", "selftest", "blerelay"]


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def main():
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    tag = os.environ["GITHUB_EVENT_NAME"] == "push" and os.environ["GITHUB_REF"].startswith("refs/tags/firmware-")
    variants = FORMAL_VARIANTS
    if tag:
        subprocess.run(["git", "merge-base", "--is-ancestor", "HEAD", "origin/main"], check=True)
        families = FAMILIES
    elif os.environ["GITHUB_EVENT_NAME"] == "workflow_dispatch":
        target = event.get("inputs", {}).get("target", "all")
        variant = event.get("inputs", {}).get("variant", "all")
        if target not in ["all", *FAMILIES] or variant not in ["all", *FORMAL_VARIANTS, "expansion"]:
            raise SystemExit("Unknown firmware target/variant")
        families = FAMILIES if target == "all" else [target]
        variants = FORMAL_VARIANTS if variant == "all" else [variant]
    else:
        before = event.get("before", "")
        if not before or set(before) == {"0"}:
            families = FAMILIES
        else:
            # Full history includes the previous push; force pushes can remove it.
            if subprocess.run(["git", "cat-file", "-e", before], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
                subprocess.run(["git", "fetch", "--no-tags", "origin", before], check=True)
            paths = git("diff", "--name-only", before, "HEAD").splitlines()
            shared = any(p in (".github/workflows/firmware-build.yml", ".github/scripts/test_firmware_select.py") or p.startswith(".github/scripts/firmware-") for p in paths)
            families = FAMILIES if shared else [f for f in FAMILIES if any(p.startswith(f"firmware/{f}/") for p in paths)]
    include = []
    for family in families:
        for variant in variants if family == "devkit" else [family]:
            include.append({"family": family, "variant": variant, "sdk": "idf" if family == "blerelay" else "ncs"})
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write("matrix=" + json.dumps({"include": include}, separators=(",", ":")) + "\n")
        output.write(f"selected={'true' if include else 'false'}\n")


if __name__ == "__main__":
    main()
