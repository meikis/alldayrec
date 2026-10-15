"""Behavioral regressions for changed-family and formal/manual trigger boundaries."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).with_name("firmware-select.py")


class FirmwareSelection(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "-q")
        self.git("config", "user.email", "ci@example.invalid")
        self.git("config", "user.name", "CI fixture")
        (self.root / "README").write_text("base\n")
        self.commit()
        self.base = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/main", self.base)

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, text=True, stderr=subprocess.PIPE).strip()

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")

    def select(self, event_name, ref, event):
        event_path = self.root / "event.json"
        output_path = self.root / "output"
        event_path.write_text(json.dumps(event))
        env = dict(os.environ, GITHUB_EVENT_NAME=event_name, GITHUB_REF=ref,
                   GITHUB_EVENT_PATH=str(event_path), GITHUB_OUTPUT=str(output_path))
        result = subprocess.run([sys.executable, str(SCRIPT)], cwd=self.root, env=env, capture_output=True, text=True)
        if result.returncode:
            return result, None
        outputs = dict(line.split("=", 1) for line in output_path.read_text().splitlines())
        return result, json.loads(outputs["matrix"])["include"]

    def test_main_devkit_change_builds_all_recorder_models_only(self):
        source = self.root / "firmware/devkit/src/main.c"
        source.parent.mkdir(parents=True)
        source.write_text("int value;\n")
        self.commit()
        result, jobs = self.select("push", "refs/heads/main", {"before": self.base})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({(j["family"], j["variant"]) for j in jobs}, {
            ("devkit", "sdmod"), ("devkit", "promicro"), ("devkit", "promicro-p2"), ("devkit", "promicro-p3")})

    def test_manual_dispatch_at_tag_stays_requested_test_family(self):
        # Dispatching a tag is not a formal tag-push event.
        result, jobs = self.select("workflow_dispatch", "refs/tags/firmware-test", {
            "inputs": {"target": "devkit", "variant": "expansion"}})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(jobs, [{"family": "devkit", "variant": "expansion", "sdk": "ncs"}])

    def test_formal_tag_builds_exact_six_targets(self):
        result, jobs = self.select("push", "refs/tags/firmware-test", {})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({(j["family"], j["variant"]) for j in jobs}, {
            ("devkit", "sdmod"), ("devkit", "promicro"), ("devkit", "promicro-p2"),
            ("devkit", "promicro-p3"), ("selftest", "selftest"), ("blerelay", "blerelay")})

    def test_formal_tag_outside_main_is_rejected(self):
        (self.root / "README").write_text("unmerged\n")
        self.commit()
        result, jobs = self.select("push", "refs/tags/firmware-test", {})
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(jobs)

    def test_unrelated_main_change_selects_no_firmware(self):
        (self.root / "README").write_text("documentation\n")
        self.commit()
        result, jobs = self.select("push", "refs/heads/main", {"before": self.base})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(jobs, [])


if __name__ == "__main__":
    unittest.main()
