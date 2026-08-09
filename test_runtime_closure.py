import hashlib
import json
import os
import pathlib
import shutil
import stat
import subprocess
import tempfile
import unittest

import verify_runtime_closure


ROOT = pathlib.Path(__file__).resolve().parent


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Fixture:
    def __init__(self, base: pathlib.Path, script: str = "#!/usr/bin/env bash\nprintf ok\n"):
        self.root = base / "release"
        self.root.mkdir()
        shutil.copy2(ROOT / "verify_runtime_closure.py", self.root)
        (self.root / "entry.sh").write_text(script)
        (self.root / "entry.sh").chmod(0o755)
        (self.root / "verify_runtime_closure.py").chmod(0o755)
        self.mutable = base / "state"
        self.mutable.mkdir()
        self.policy = {
            "schemaVersion": 2,
            "immutableCodeRoots": ["$PHYSICAL_RELEASE_ROOT"],
            "externalCodeDependencies": [],
            "mutableDataRoots": [str(self.mutable), "/tmp"],
            "forbiddenCodeRoots": ["/home/ubuntu/nova-auto-download", "/home/ubuntu/nova-dashboard-deploy-final"],
            "entrypoints": ["entry.sh"],
            "environmentExecutableOverrides": {
                "TIMO_DISPLAY_TIME_REBUILDER": {"scope": "sameReleaseManifest"},
                "NOVA_NOTIFY_SCRIPT": {"scope": "sameReleaseManifest"},
                "NOVA_DAILY_SYNC_COMMAND": {"scope": "sameReleaseManifest", "singleExecutable": True},
            },
            "runtimeRequirements": {"entry.sh": {"nodePackages": [], "playwrightBrowser": False}},
            "executableReleaseFiles": ["entry.sh", "verify_runtime_closure.py"],
        }
        self.write_policy()
        self.write_manifest()

    def write_policy(self):
        (self.root / "runtime-closure-policy.json").write_text(json.dumps(self.policy))

    def write_manifest(self, omit=()):
        files = []
        for path in sorted(self.root.iterdir()):
            if path.is_file() and path.name != "release-manifest.json" and path.name not in omit:
                files.append({"path": path.name, "sha256": digest(path), "mode": f"{stat.S_IMODE(path.stat().st_mode):o}"})
        (self.root / "release-manifest.json").write_text(json.dumps({"commit": "a" * 40, "files": files}))

    def run(self, *, preflight=False, env=None, via=None):
        command = ["python3", str((via or self.root) / "verify_runtime_closure.py")]
        if preflight:
            command += ["--preflight-entry", "entry.sh"]
        else:
            command += ["--manifest", str(self.root / "release-manifest.json")]
        return subprocess.run(command, cwd=via or self.root, env={**os.environ, **(env or {})}, text=True, capture_output=True)


class RuntimeClosureTest(unittest.TestCase):
    def test_policy_closure_passes_from_repository(self):
        self.assertEqual(verify_runtime_closure.main([]), 0)

    def test_runtime_requirements_are_entry_specific(self):
        policy = json.loads((ROOT / "runtime-closure-policy.json").read_text())
        daily = policy["runtimeRequirements"]["daily-sync.sh"]
        heal = policy["runtimeRequirements"]["bi-data-heal.sh"]
        timo = policy["runtimeRequirements"]["sync-timo-external-daily.sh"]
        self.assertTrue(daily["playwrightBrowser"])
        self.assertTrue(heal["playwrightBrowser"])
        self.assertFalse(timo["playwrightBrowser"])
        self.assertEqual(timo["nodePackages"], [])

    def test_browser_requirement_is_not_silently_skipped_for_daily_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Fixture(pathlib.Path(tmp))
            f.policy["runtimeRequirements"]["entry.sh"] = {
                "nodePackages": ["definitely-not-installed-runtime-package"],
                "playwrightBrowser": True,
            }
            f.write_policy()
            f.write_manifest()
            result = f.run(preflight=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("NODE_PACKAGE_UNRESOLVED", result.stderr)
            self.assertIn("PLAYWRIGHT_BROWSER_UNRESOLVED", result.stderr)

    def test_env_override_old_checkout_tmp_and_state_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Fixture(pathlib.Path(tmp))
            state_script = f.mutable / "daily-sync.sh"
            state_script.write_text("#!/bin/sh\n")
            state_script.chmod(0o755)
            attacks = {
                "TIMO_DISPLAY_TIME_REBUILDER": "/home/ubuntu/nova-auto-download/old.py",
                "NOVA_NOTIFY_SCRIPT": "/tmp/example.sh",
                "NOVA_DAILY_SYNC_COMMAND": str(state_script),
            }
            for variable, value in attacks.items():
                result = f.run(preflight=True, env={variable: value})
                self.assertNotEqual(result.returncode, 0, (variable, result.stderr))

    def test_prefix_collision_traversal_and_symlink_escape_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Fixture(pathlib.Path(tmp))
            evil = pathlib.Path(tmp) / "release-evil" / "evil.sh"
            evil.parent.mkdir()
            evil.write_text("#!/bin/sh\n")
            traversal = f.root / ".." / "release-evil" / "evil.sh"
            link = f.root / "allowed.sh"
            link.symlink_to(evil)
            for value in (str(evil), str(traversal), str(link)):
                result = f.run(preflight=True, env={"NOVA_NOTIFY_SCRIPT": value})
                self.assertNotEqual(result.returncode, 0, result.stderr)

    def test_mutable_data_read_passes_but_execution_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            f = Fixture(base, f"#!/bin/sh\nprintf data < {base}/state/value.json\n")
            f.write_manifest()
            self.assertEqual(f.run().returncode, 0)
            for command in (f"exec {f.mutable}/x.sh", f"source {f.mutable}/x.sh", "python3 /tmp/x.py"):
                (f.root / "entry.sh").write_text(f"#!/bin/sh\n{command}\n")
                f.write_manifest()
                result = f.run()
                self.assertNotEqual(result.returncode, 0, (command, result.stderr))

    def test_automatic_edge_discovery_requires_manifest_membership(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Fixture(pathlib.Path(tmp), '#!/bin/sh\nSCRIPT_DIR="$(dirname "$0")"\nexec "$SCRIPT_DIR/new-script.sh"\n')
            child = f.root / "new-script.sh"
            child.write_text("#!/bin/sh\nexit 0\n")
            child.chmod(0o755)
            f.write_manifest(omit={"new-script.sh"})
            self.assertIn("NOT_IN_RELEASE_MANIFEST", f.run().stderr)
            f.write_manifest()
            self.assertEqual(f.run().returncode, 0)

    def test_unknown_eval_and_shell_command_strings_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Fixture(pathlib.Path(tmp))
            for command in ('exec "$UNKNOWN_COMMAND"', 'eval "$COMMAND"', 'bash -c "$COMMAND"', 'sh -c "$COMMAND"'):
                (f.root / "entry.sh").write_text(f"#!/bin/sh\n{command}\n")
                f.write_manifest()
                self.assertIn("UNRESOLVED_DYNAMIC_EXEC", f.run().stderr, command)

    def test_whole_shell_command_override_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Fixture(pathlib.Path(tmp))
            result = f.run(preflight=True, env={"NOVA_DAILY_SYNC_COMMAND": f"{f.root}/entry.sh --unsafe"})
            self.assertIn("ENV_COMMAND_STRING_FORBIDDEN", result.stderr)

    def test_current_symlink_resolves_to_physical_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Fixture(pathlib.Path(tmp))
            current = pathlib.Path(tmp) / "current"
            current.symlink_to(f.root, target_is_directory=True)
            self.assertEqual(f.run(preflight=True, via=current).returncode, 0)

    def test_manifest_checksum_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Fixture(pathlib.Path(tmp))
            (f.root / "entry.sh").write_text("#!/bin/sh\nprintf changed\n")
            self.assertIn("CHECKSUM_MISMATCH", f.run().stderr)

    def test_wrapped_commands_are_discovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            commands = "\n".join((
                'env FLAG=1 timeout 2 flock -n /tmp/x.lock nohup bash "$SCRIPT_DIR/a.sh"',
                'sh "$SCRIPT_DIR/b.sh"',
                'python3 "$SCRIPT_DIR/c.py"',
                'node "$SCRIPT_DIR/d.mjs"',
            ))
            f = Fixture(pathlib.Path(tmp), f'#!/bin/sh\nSCRIPT_DIR="$(dirname "$0")"\n{commands}\n')
            for name in ("a.sh", "b.sh", "c.py", "d.mjs"):
                child = f.root / name
                child.write_text("#!/bin/sh\n")
                child.chmod(0o755)
            f.write_manifest()
            graph, failures = verify_runtime_closure.discover(f.root, ["entry.sh"], set(f.policy["environmentExecutableOverrides"]))
            self.assertFalse(failures)
            self.assertEqual(set(graph["entry.sh"]), {"a.sh", "b.sh", "c.py", "d.mjs"})

    def test_external_backend_requires_exact_release_manifest_and_checksum(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            f = Fixture(base)
            releases = base / "backend-releases"
            release = releases / "build-good"
            target = release / "api/src/task.ts"
            target.parent.mkdir(parents=True)
            target.write_text("export {}\n")
            manifest = release / "api/task-chain-manifest.sha256"
            manifest.write_text(f"{digest(target)}  api/src/task.ts\n")
            (release / "release-metadata.json").write_text(json.dumps({"commit": "b" * 40, "taskManifestHash": digest(manifest)}))
            current = base / "backend-current"
            current.symlink_to(release, target_is_directory=True)
            dependency = {
                "name": "backend",
                "entryRoot": str(current / "api"),
                "releaseRootPattern": str(releases),
                "metadata": "release-metadata.json",
                "manifest": "api/task-chain-manifest.sha256",
                "files": ["api/src/task.ts"],
            }
            f.policy["externalCodeDependencies"] = [dependency]
            f.write_policy()
            f.write_manifest()
            self.assertEqual(f.run(preflight=True).returncode, 0)

            manifest.write_text("")
            (release / "release-metadata.json").write_text(json.dumps({"taskManifestHash": digest(manifest)}))
            self.assertIn("EXTERNAL_NOT_IN_MANIFEST", f.run(preflight=True).stderr)

            manifest.write_text(f"{'0' * 64}  api/src/task.ts\n")
            (release / "release-metadata.json").write_text(json.dumps({"taskManifestHash": digest(manifest)}))
            self.assertIn("EXTERNAL_CHECKSUM_MISMATCH", f.run(preflight=True).stderr)

            evil_parent = base / "backend-releases-evil"
            evil_release = evil_parent / "build-evil"
            shutil.copytree(release, evil_release)
            current.unlink()
            current.symlink_to(evil_release, target_is_directory=True)
            self.assertIn("EXTERNAL_PATH_BOUNDARY", f.run(preflight=True).stderr)

    def test_timo_daily_resolves_same_release_and_preserves_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            release = pathlib.Path(tmp) / "release"
            release.mkdir()
            for name in ("sync-timo-external-daily.sh", "verify_runtime_closure.py", "runtime-closure-policy.json"):
                shutil.copy2(ROOT / name, release / name)
            policy = json.loads((release / "runtime-closure-policy.json").read_text())
            policy["entrypoints"] = ["sync-timo-external-daily.sh"]
            policy["externalCodeDependencies"] = []
            policy["runtimeRequirements"] = {"sync-timo-external-daily.sh": {"nodePackages": [], "playwrightBrowser": False}}
            policy["executableReleaseFiles"] = ["sync-timo-external-daily.sh", "sync-timo-external.sh", "verify_runtime_closure.py"]
            (release / "runtime-closure-policy.json").write_text(json.dumps(policy))
            runner = release / "sync-timo-external.sh"
            runner.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$TIMO_SYNC_WINDOW\" \"$0\" \"$@\"\nexit 37\n")
            runner.chmod(0o755)
            manifest = []
            for path in release.iterdir():
                if path.is_file():
                    manifest.append({"path": path.name, "sha256": digest(path), "mode": f"{stat.S_IMODE(path.stat().st_mode):o}"})
            (release / "release-manifest.json").write_text(json.dumps({"commit": "a" * 40, "files": manifest}))
            current = pathlib.Path(tmp) / "current"
            current.symlink_to(release, target_is_directory=True)
            result = subprocess.run([str(current / "sync-timo-external-daily.sh"), "alpha", "beta"], text=True, capture_output=True, env={**os.environ, "TIMO_SYNC_WINDOW": "rolling"})
            self.assertEqual(result.returncode, 37)
            lines = result.stdout.splitlines()
            self.assertIn("runtime closure: PASSED", lines[0])
            self.assertEqual(lines[1], "daily")
            self.assertEqual(pathlib.Path(lines[2]).resolve(), runner.resolve())
            self.assertEqual(lines[3:], ["alpha", "beta"])


if __name__ == "__main__":
    unittest.main()
