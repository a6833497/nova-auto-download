import json
import tempfile
import unittest
from pathlib import Path

from build_release_manifest import atomic_write, build_manifest


class BuildReleaseManifestTest(unittest.TestCase):
    def test_manifest_hashes_current_release_bytes_and_skips_old_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "daily-sync.sh"
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)
            (root / "release-manifest.json").write_text("stale", encoding="utf-8")
            manifest = build_manifest(root, "a" * 40, "20260818T120000Z", "/old")
            self.assertEqual(["daily-sync.sh"], [item["path"] for item in manifest["files"]])
            self.assertEqual("755", manifest["files"][0]["mode"])
            self.assertEqual("a" * 40, manifest["commit"])

    def test_atomic_write_replaces_stale_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "release-manifest.json"
            target.write_text("stale", encoding="utf-8")
            atomic_write(target, {"status": "PASSED"})
            self.assertEqual({"status": "PASSED"}, json.loads(target.read_text(encoding="utf-8")))

    def test_rejects_non_full_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                build_manifest(Path(directory), "abc", "20260818T120000Z", "/old")


if __name__ == "__main__":
    unittest.main()
