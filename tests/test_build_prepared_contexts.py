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
import unittest


class BuildPreparedContextsTest(unittest.TestCase):
    def setUp(self):
        self.repo_root = pathlib.Path(__file__).resolve().parents[1]
        (self.repo_root / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(
            dir=self.repo_root / ".tmp", prefix="prepared-context-test."
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.contexts = self.root / "contexts"
        self.fake_bin = self.root / "bin"
        self.capture = self.root / "buildah.log"
        self._create_contexts()
        self._create_commands()

        self.environment = os.environ.copy()
        self.environment.update(
            {
                "BUILD_CAPTURE": str(self.capture),
                "PATH": f"{self.fake_bin}:{self.environment['PATH']}",
                "STREAM": "master",
                "REGISTRY": "registry.test:5000",
                "NAMESPACE": "openstack",
                "TAG": "prepared",
                "S2I_CONTEXTS_ROOT": str(self.contexts),
                "ERROR_ON_CLONE": "1",
                "BUILD_LOGS_DIR": str(self.root / "logs"),
                "PARALLEL": "1",
            }
        )

    def _create_contexts(self):
        for context in ("base", "watcher", "cyborg"):
            path = self.contexts / context
            path.mkdir(parents=True)
            (path / "requirements.lock.master").write_text(
                "constraints\n", encoding="utf-8"
            )
        (self.contexts / "base" / "Containerfile").write_text(
            "FROM scratch\n", encoding="utf-8"
        )
        for project, images in {
            "watcher": ["watcher-base"],
            "cyborg": ["cyborg", "cyborg-agent"],
        }.items():
            (self.contexts / project / "src" / project).mkdir(parents=True)
            for image in images:
                image_dir = self.contexts / project / image
                image_dir.mkdir()
                (image_dir / "Containerfile").write_text(
                    "FROM scratch\n", encoding="utf-8"
                )

    def _create_commands(self):
        self.fake_bin.mkdir()
        buildah = self.fake_bin / "buildah"
        buildah.write_text(
            "#!/usr/bin/env python3\n"
            "import os\n"
            "import pathlib\n"
            "import sys\n"
            "path = pathlib.Path(os.environ['BUILD_CAPTURE'])\n"
            "with path.open('a', encoding='utf-8') as stream:\n"
            "    stream.write(' '.join(sys.argv[1:]) + '\\n')\n",
            encoding="utf-8",
        )
        git = self.fake_bin / "git"
        git.write_text(
            "#!/bin/sh\necho 'prepared build invoked git' >&2\nexit 97\n",
            encoding="utf-8",
        )
        buildah.chmod(0o755)
        git.chmod(0o755)

    def _run(self, check=True, environment=None):
        self.capture.write_text("", encoding="utf-8")
        command_environment = self.environment.copy()
        command_environment.update(environment or {})
        result = subprocess.run(
            [
                "bash",
                str(self.repo_root / "build.sh"),
                "build-parallel",
                "watcher/watcher-base,cyborg/cyborg,cyborg/cyborg-agent",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=command_environment,
        )
        if check and result.returncode:
            self.fail(f"build failed: {result.stderr}")
        commands = self.capture.read_text(encoding="utf-8").splitlines()
        return result, commands

    def test_strict_prepared_build_uses_all_isolated_contexts(self):
        _result, commands = self._run()

        self.assertEqual(4, len(commands))
        for context in ("base", "watcher", "cyborg"):
            self.assertTrue(
                any(
                    f" {self.contexts}/{context}/" in command
                    for command in commands
                )
            )
        self.assertTrue(
            all(
                "CONSTRAINTS_FILE=requirements.lock.master" in command
                for command in commands
            )
        )

    def test_missing_prepared_source_never_falls_back_to_clone(self):
        (self.contexts / "watcher" / "src" / "watcher").rmdir()

        result, commands = self._run(check=False)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("Main source not found", result.stdout + result.stderr)
        self.assertLess(len(commands), 4)

    def test_prepared_contexts_outside_repository_tmp_are_rejected(self):
        with tempfile.TemporaryDirectory() as outside:
            result, commands = self._run(
                check=False, environment={"S2I_CONTEXTS_ROOT": outside}
            )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("must remain beneath", result.stdout + result.stderr)
        self.assertEqual([], commands)


if __name__ == "__main__":
    unittest.main()
