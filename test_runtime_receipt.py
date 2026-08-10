import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from write_runtime_receipt import build_receipt


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RuntimeReceiptTest(unittest.TestCase):
    def test_receipt_binds_exact_auto_and_backend_files(self):
        with TemporaryDirectory() as root:
            base = Path(root)
            auto = base / "auto-release"
            backend = base / "backend-release"
            auto.mkdir()
            projection = backend / "api/src/scripts/refresh-operations-projections.ts"
            projection.parent.mkdir(parents=True)
            runner = auto / "linky_sync_runner.py"
            runner.write_text("# runner\n")
            projection.write_text("// projection\n")
            (auto / "release-manifest.json").write_text(json.dumps({
                "commit": "a" * 40,
                "files": [{"path": "linky_sync_runner.py", "sha256": digest(runner), "mode": "444"}],
            }))
            task_manifest = backend / "api/task-chain-manifest.sha256"
            task_manifest.parent.mkdir(exist_ok=True)
            task_manifest.write_text(f"{digest(projection)}  api/src/scripts/refresh-operations-projections.ts\n")
            (backend / "release-metadata.json").write_text(json.dumps({
                "commit": "b" * 40, "taskManifestHash": digest(task_manifest),
            }))

            receipt = build_receipt("batch-1", auto, backend)
            self.assertEqual("PASSED", receipt["status"])
            self.assertEqual("a" * 40, receipt["autoCommit"])
            self.assertEqual("b" * 40, receipt["backendCommit"])

            projection.write_text("// changed\n")
            with self.assertRaisesRegex(ValueError, "projection checksum mismatch"):
                build_receipt("batch-2", auto, backend)


if __name__ == "__main__":
    unittest.main()
