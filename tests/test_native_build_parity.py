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

import json
import os
import pathlib
import subprocess
import tempfile
import unittest

from openstack_image_builder import build_plan
from openstack_image_builder import images as image_config


class NativeBuildParityTest(unittest.TestCase):
    def setUp(self):
        self.repo_root = pathlib.Path(__file__).resolve().parents[1]
        self.temporary = tempfile.TemporaryDirectory(
            dir=self.repo_root / ".tmp", prefix="native-parity-test."
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.contexts = self.root / "contexts"
        self.bin = self.root / "bin"
        self._create_contexts()
        self._create_buildah()

    def _create_contexts(self):
        base = self.contexts / "base"
        base.mkdir(parents=True)
        (base / "Containerfile").write_text("FROM scratch\n", encoding="utf-8")
        (base / "requirements.lock.master").write_text(
            "base\n", encoding="utf-8"
        )
        for project, images in {
            "watcher": ("watcher-base",),
            "cyborg": ("cyborg", "cyborg-agent"),
        }.items():
            context = self.contexts / project
            (context / "src" / project).mkdir(parents=True)
            for image in images:
                image_dir = context / image
                image_dir.mkdir()
                (image_dir / "Containerfile").write_text(
                    "FROM scratch\n", encoding="utf-8"
                )
                (image_dir / "requirements.lock.effective.master").write_text(
                    "lock\n", encoding="utf-8"
                )
                (image_dir / "source-package-map.effective.txt").write_text(
                    f"{project} {project}\n", encoding="utf-8"
                )

    def _create_buildah(self):
        self.bin.mkdir()
        command = self.bin / "buildah"
        command.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            "import os\n"
            "import pathlib\n"
            "import sys\n"
            "path = pathlib.Path(os.environ['BUILD_CAPTURE'])\n"
            "with path.open('a', encoding='utf-8') as stream:\n"
            "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n",
            encoding="utf-8",
        )
        command.chmod(0o755)

    def _assert_case(self, name, target, images, base_image=None):
        capture = self.root / f"{name}.jsonl"
        normalized, _expression = image_config.target_selection(
            self.repo_root / "containers", target
        )
        self.assertEqual(images, normalized)
        shell_target = ",".join(images)
        source_plan = {
            "version": 3,
            "stream": "master",
            "images": images,
            "target_expression": (
                "base" if len(images) == 1 else ",".join(images[1:])
            ),
        }
        native = build_plan.create(
            self.repo_root,
            source_plan,
            contexts_root=self.contexts,
            registry="registry.test:5000",
            namespace="testing",
            tags="one,two",
            image_prefix="custom",
            base_image=base_image,
            base_os_image="quay.example/base-os:test",
            platform="linux/amd64/v3",
            pip_no_binary=":all:",
            parallel=2,
        )
        environment = os.environ.copy()
        environment.update(
            {
                "BASE_IMAGE": base_image or "",
                "BASE_OS_IMAGE": "quay.example/base-os:test",
                "BUILD_CAPTURE": str(capture),
                "BUILD_LOGS_DIR": str(self.root / f"{name}-logs"),
                "BUILD_PLATFORM": "linux/amd64/v3",
                "ERROR_ON_CLONE": "1",
                "IMAGE_PREFIX": "custom",
                "NAMESPACE": "testing",
                "PARALLEL": "2",
                "PATH": f"{self.bin}:{environment['PATH']}",
                "PIP_NO_BINARY": ":all:",
                "REGISTRY": "registry.test:5000",
                "S2I_CONTEXTS_ROOT": str(self.contexts),
                "STREAM": "master",
                "TAG": "one,two",
            }
        )
        build_result = subprocess.run(
            [
                "bash",
                str(self.repo_root / "build.sh"),
                "build-parallel",
                shell_target,
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertEqual(
            0,
            build_result.returncode,
            build_result.stdout + build_result.stderr,
        )
        refs_result = subprocess.run(
            [
                "bash",
                str(self.repo_root / "build.sh"),
                "refs",
                shell_target,
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertEqual(0, refs_result.returncode, refs_result.stderr)
        shell_commands = [
            json.loads(line)
            for line in capture.read_text(encoding="utf-8").splitlines()
        ]
        shell_by_file = {
            command[command.index("-f") + 1]: command
            for command in shell_commands
        }
        native_by_file = {
            image.containerfile: list(image.argv[1:])
            for image in native.images
        }
        self.assertEqual(native_by_file, shell_by_file)
        self.assertEqual(
            native.references, tuple(refs_result.stdout.splitlines())
        )
        return native

    def test_empty_image_prefix_reference_parity(self):
        source_plan = {
            "version": 3,
            "stream": "master",
            "images": ["base"],
            "target_expression": "base",
        }
        native = build_plan.create(
            self.repo_root,
            source_plan,
            contexts_root=self.contexts,
            registry="registry.test:5000",
            namespace="testing",
            tags="one",
            image_prefix="",
        )
        environment = os.environ.copy()
        environment.update(
            {
                "IMAGE_PREFIX": "",
                "NAMESPACE": "testing",
                "REGISTRY": "registry.test:5000",
                "STREAM": "master",
                "TAG": "one",
            }
        )
        result = subprocess.run(
            ["bash", str(self.repo_root / "build.sh"), "refs", "base"],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            ["registry.test:5000/testing/base:one"],
            result.stdout.splitlines(),
        )
        self.assertEqual(tuple(result.stdout.splitlines()), native.references)

    def test_shell_and_native_complete_buildah_argv_matrix(self):
        cases = (
            ("base", "base", ["base"]),
            (
                "one-service",
                "watcher/watcher-base",
                ["base", "watcher/watcher-base"],
            ),
            (
                "project",
                "cyborg",
                ["base", "cyborg/cyborg", "cyborg/cyborg-agent"],
            ),
            (
                "ordered-union",
                "watcher/watcher-base,cyborg/cyborg",
                ["base", "watcher/watcher-base", "cyborg/cyborg"],
            ),
            (
                "all",
                "all",
                [
                    "base",
                    "cyborg/cyborg",
                    "cyborg/cyborg-agent",
                    "watcher/watcher-base",
                ],
            ),
        )
        for name, target, images in cases:
            with self.subTest(name=name):
                self._assert_case(name, target, images)

    def test_custom_service_base_omits_local_pull_policy(self):
        value = self._assert_case(
            "custom-base",
            "watcher/watcher-base",
            ["base", "watcher/watcher-base"],
            base_image="quay.example/custom/base:test",
        )

        self.assertNotIn("--pull-never", value.images[1].argv)


if __name__ == "__main__":
    unittest.main()
