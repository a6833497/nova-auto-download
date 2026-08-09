import os
import pathlib
import shutil
import stat
import subprocess
import tempfile
import unittest

import verify_runtime_closure


ROOT = pathlib.Path(__file__).resolve().parent


class RuntimeClosureTest(unittest.TestCase):
    def test_policy_closure_passes_from_repository(self):
        self.assertEqual(verify_runtime_closure.main([]), 0)

    def test_gate_distinguishes_executable_code_from_mutable_output(self):
        old = "/home/ubuntu/nova-auto-download"
        self.assertTrue(verify_runtime_closure.is_code_reference(f"exec {old}/runner.sh", old))
        self.assertTrue(verify_runtime_closure.is_code_reference(f"cd {old} && python3 runner.py", old))
        self.assertFalse(verify_runtime_closure.is_code_reference(f"STATE_ROOT={old}/state", old))
        self.assertFalse(verify_runtime_closure.is_code_reference(f"command >> {old}/logs/run.log", old))

    def test_timo_daily_resolves_same_release_and_preserves_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            release = pathlib.Path(tmp) / "release"
            release.mkdir()
            wrapper = release / "sync-timo-external-daily.sh"
            shutil.copy2(ROOT / wrapper.name, wrapper)
            runner = release / "sync-timo-external.sh"
            runner.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$TIMO_SYNC_WINDOW\" \"$0\" \"$@\"\n"
                "exit 37\n",
                encoding="utf-8",
            )
            runner.chmod(runner.stat().st_mode | stat.S_IXUSR)
            current = pathlib.Path(tmp) / "current"
            current.symlink_to(release, target_is_directory=True)
            result = subprocess.run(
                [str(current / wrapper.name), "alpha", "beta"],
                text=True,
                capture_output=True,
                env={**os.environ, "TIMO_SYNC_WINDOW": "rolling"},
                check=False,
            )
            lines = result.stdout.splitlines()
            self.assertEqual(result.returncode, 37)
            self.assertEqual(lines[0], "daily")
            self.assertEqual(pathlib.Path(lines[1]).resolve(), runner.resolve())
            self.assertEqual(lines[2:], ["alpha", "beta"])


if __name__ == "__main__":
    unittest.main()
