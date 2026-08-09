# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import os
import pathlib
import subprocess
import tempfile
import time
import unittest


class ProviderShellTest(unittest.TestCase):
    def setUp(self):
        self.repo_root = pathlib.Path(__file__).resolve().parents[1]
        temporary_root = self.repo_root / ".tmp"
        temporary_root.mkdir(exist_ok=True)
        self.temporary_directory = tempfile.TemporaryDirectory(
            dir=temporary_root, prefix="provider-shell."
        )
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = pathlib.Path(self.temporary_directory.name)
        (self.root / "build.sh").symlink_to(self.repo_root / "build.sh")
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.logs_dir = self.root / "logs"
        self._create_images()
        self._create_fake_buildah()

    def _create_images(self):
        for target in ("base", "alpha/one", "beta/two"):
            image_root = self.root / "containers" / target
            image_root.mkdir(parents=True)
            (image_root / "Containerfile").write_text(
                "FROM scratch\n", encoding="utf-8"
            )
            project = target.split("/", maxsplit=1)[0]
            project_root = self.root / "containers" / project
            (project_root / "requirements.lock.master").touch()
            if "/" in target:
                (project_root / "src" / project).mkdir(
                    parents=True, exist_ok=True
                )

    def _create_fake_buildah(self):
        fake = self.bin_dir / "buildah"
        fake.write_text(
            """#!/usr/bin/env python3
import os
import pathlib
import sys
import time

args = sys.argv[1:]
if args[0] == "bud":
    containerfile = pathlib.Path(args[args.index("-f") + 1])
    image = containerfile.parent.name
    if containerfile.parent.parent.name != "containers":
        image = f"{containerfile.parent.parent.name}/{image}"
    if image != "base":
        print(f"LIVE {image}", flush=True)
        time.sleep(0.75)
    if os.environ.get("FAIL_IMAGE") and image.endswith(os.environ["FAIL_IMAGE"]):
        print(f"FAIL {image}", flush=True)
        sys.exit(9)
    print(f"DONE {image}", flush=True)
    sys.exit(0)
if args[0] == "inspect":
    sys.exit(0)
if args[0] == "push":
    sys.exit(0)
sys.exit(2)
""",
            encoding="utf-8",
        )
        fake.chmod(0o755)

    def _environment(self):
        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{self.bin_dir}:{environment['PATH']}",
                "STREAM": "master",
                "REGISTRY": "registry.test:5000",
                "NAMESPACE": "openstack",
                "TAG": "test",
                "PARALLEL": "2",
                "BUILD_LOGS_DIR": str(self.logs_dir),
            }
        )
        return environment

    def _run(self, *arguments, environment=None):
        return subprocess.run(
            [str(self.root / "build.sh"), *arguments],
            cwd=self.root,
            env=environment or self._environment(),
            check=False,
            capture_output=True,
            text=True,
        )

    def test_explicit_union_is_ordered_and_includes_base(self):
        result = self._run("refs", "beta/two,alpha/one,beta/two")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            [
                "registry.test:5000/openstack/openstack-base:test",
                "registry.test:5000/openstack/openstack-two:test",
                "registry.test:5000/openstack/openstack-one:test",
            ],
            result.stdout.splitlines(),
        )

    def test_single_target_semantics_are_unchanged(self):
        result = self._run("refs", "alpha/one")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            ["registry.test:5000/openstack/openstack-one:test"],
            result.stdout.splitlines(),
        )

    def test_resolve_all_returns_machine_readable_targets(self):
        result = self._run("resolve", "all")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            ["base", "alpha/one", "beta/two"], result.stdout.splitlines()
        )

    def test_explicit_union_rejects_unknown_target(self):
        result = self._run("refs", "alpha/one,unknown/image")

        self.assertNotEqual(0, result.returncode)
        self.assertIn("Unknown image or project", result.stderr)

    def test_parallel_output_is_live_and_logs_are_retained(self):
        process = subprocess.Popen(
            [
                str(self.root / "build.sh"),
                "build-parallel",
                "alpha/one,beta/two",
            ],
            cwd=self.root,
            env=self._environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.addCleanup(
            lambda: process.kill() if process.poll() is None else None
        )
        self.addCleanup(process.stdout.close)
        output = []
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            line = process.stdout.readline()
            output.append(line)
            if "LIVE" in line:
                break

        self.assertIn("LIVE", "".join(output))
        self.assertIsNone(
            process.poll(), "build exited before live output arrived"
        )
        output.append(process.stdout.read())
        process.wait(timeout=10)
        self.assertEqual(0, process.returncode, "".join(output))
        self.assertIn("[alpha/one] LIVE alpha/one", "".join(output))
        self.assertIn("[beta/two] LIVE beta/two", "".join(output))
        self.assertTrue((self.logs_dir / "base.log").is_file())
        self.assertTrue((self.logs_dir / "alpha_one.log").is_file())
        self.assertTrue((self.logs_dir / "beta_two.log").is_file())
        self.assertEqual(1, "".join(output).count("LIVE alpha/one"))

    def test_parallel_failure_propagates(self):
        environment = self._environment()
        environment["FAIL_IMAGE"] = "two"

        result = self._run(
            "build-parallel", "alpha/one,beta/two", environment=environment
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("FAIL beta/two", result.stdout + result.stderr)
        self.assertIn("stopping remaining builds", result.stderr)


if __name__ == "__main__":
    unittest.main()
