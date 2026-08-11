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

import dataclasses
import json
import pathlib
import tempfile
import unittest

from openstack_image_builder import build_plan
from openstack_image_builder import images


class BuildPlanTest(unittest.TestCase):
    def setUp(self):
        self.repo_root = pathlib.Path(__file__).resolve().parents[1]
        self.temporary = tempfile.TemporaryDirectory(
            dir=self.repo_root / ".tmp", prefix="native-plan-test."
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.contexts = self.root / "contexts"
        self._create_contexts()
        self.source_plan = {
            "version": 3,
            "stream": "master",
            "images": [
                "base",
                "watcher/watcher-base",
                "cyborg/cyborg",
                "cyborg/cyborg-agent",
            ],
            "target_expression": (
                "watcher/watcher-base,cyborg/cyborg,cyborg/cyborg-agent"
            ),
        }

    def _create_contexts(self):
        base = self.contexts / "base"
        base.mkdir(parents=True)
        (base / "Containerfile").write_text("FROM scratch\n", encoding="utf-8")
        (base / "requirements.lock.master").write_text(
            "base lock\n", encoding="utf-8"
        )
        for project, image_names in {
            "watcher": ("watcher-base",),
            "cyborg": ("cyborg", "cyborg-agent"),
        }.items():
            context = self.contexts / project
            (context / "src" / project).mkdir(parents=True)
            for image in image_names:
                image_dir = context / image
                image_dir.mkdir()
                (image_dir / "Containerfile").write_text(
                    "FROM scratch\n", encoding="utf-8"
                )
                (image_dir / "requirements.lock.effective.master").write_text(
                    "service lock\n", encoding="utf-8"
                )
                (image_dir / "source-package-map.effective.txt").write_text(
                    f"{project} {project}\n", encoding="utf-8"
                )

    def create(self, **overrides):
        values = {
            "contexts_root": self.contexts,
            "registry": "registry.test:5000",
            "namespace": "openstack",
            "tags": "first,second",
            "base_os_image": "registry.test/base-os:latest",
            "platform": "linux/amd64/v3",
            "pip_no_binary": ":all:",
            "parallel": 2,
        }
        values.update(overrides)
        return build_plan.create(
            self.repo_root,
            self.source_plan,
            **values,
        )

    def test_shell_compatible_target_normalization(self):
        containers = self.repo_root / "containers"

        self.assertEqual(
            (
                ["base", "cyborg/cyborg", "cyborg/cyborg-agent"],
                "cyborg/cyborg,cyborg/cyborg-agent",
            ),
            images.target_selection(containers, "cyborg"),
        )
        self.assertEqual(
            [
                "base",
                "cyborg/cyborg",
                "cyborg/cyborg-agent",
                "watcher/watcher-base",
            ],
            images.target_selection(containers, "all")[0],
        )
        self.assertEqual(
            ["base", "watcher/watcher-base", "cyborg/cyborg"],
            images.target_selection(
                containers, "watcher/watcher-base,cyborg/cyborg"
            )[0],
        )
        with self.assertRaisesRegex(ValueError, "empty"):
            images.target_selection(containers, "watcher,")

    def test_complete_base_and_service_argv_and_references(self):
        value = self.create()

        self.assertEqual(
            [
                "base",
                "watcher/watcher-base",
                "cyborg/cyborg",
                "cyborg/cyborg-agent",
            ],
            [image.image for image in value.images],
        )
        self.assertEqual(8, len(value.references))
        self.assertEqual(
            "registry.test:5000/openstack/openstack-base:first",
            value.base_image,
        )
        base = value.images[0]
        self.assertEqual(
            (
                "buildah",
                "bud",
                "--platform",
                "linux/amd64/v3",
                "--tag",
                "registry.test:5000/openstack/openstack-base:first",
                "--tag",
                "registry.test:5000/openstack/openstack-base:second",
                "--build-arg",
                "BASE_IMAGE=registry.test/base-os:latest",
                "--build-arg",
                "CONSTRAINTS_FILE=requirements.lock.master",
                "--build-arg",
                "PIP_NO_BINARY=:all:",
                "-f",
                str(self.contexts / "base" / "Containerfile"),
                f"{self.contexts}/base/",
            ),
            base.argv,
        )
        watcher = value.images[1]
        self.assertIn("--pull-never", watcher.argv)
        self.assertIn(
            "CONSTRAINTS_FILE=watcher-base/requirements.lock.effective.master",
            watcher.argv,
        )
        self.assertIn(
            "SOURCE_PACKAGE_MAP=watcher-base/source-package-map.effective.txt",
            watcher.argv,
        )
        self.assertIn(f"{self.contexts}/watcher/", watcher.argv)

    def test_explicit_service_base_disables_only_default_reference(self):
        value = self.create(base_image="quay.example/custom/base:one")

        self.assertEqual("quay.example/custom/base:one", value.base_image)
        self.assertIn(
            "BASE_IMAGE=quay.example/custom/base:one", value.images[1].argv
        )
        self.assertNotIn("--pull-never", value.images[1].argv)

    def test_atomic_round_trip_rejects_changed_argv(self):
        value = self.create()
        path = self.root / "native-build-plan.json"
        build_plan.write_atomic(path, value)

        self.assertEqual(value, build_plan.load(path))
        changed = json.loads(path.read_text(encoding="utf-8"))
        changed["images"][0]["argv"].append("--quiet")
        path.write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "argv changed"):
            build_plan.load(path)

    def test_loaded_plan_rejects_changed_context_and_containerfile(self):
        value = self.create()
        changed_context = value.to_dict()
        changed_context["images"][1]["context"] = str(self.contexts / "cyborg")
        with self.assertRaisesRegex(ValueError, "context changed"):
            build_plan.from_dict(changed_context)

        alternate = self.contexts / "watcher/alternate/Containerfile"
        alternate.parent.mkdir()
        alternate.write_text("FROM scratch\n", encoding="utf-8")
        changed_file = value.to_dict()
        changed_file["images"][1]["containerfile"] = str(alternate)
        with self.assertRaisesRegex(ValueError, "Containerfile changed"):
            build_plan.from_dict(changed_file)

    def test_rejects_context_outside_repository_tmp(self):
        with tempfile.TemporaryDirectory() as outside:
            with self.assertRaisesRegex(ValueError, "escaped"):
                self.create(contexts_root=pathlib.Path(outside))

    def test_rejects_package_input_symlink_escape(self):
        package_input = (
            self.contexts
            / "watcher/watcher-base/requirements.lock.effective.master"
        )
        outside = self.root / "outside.lock"
        outside.write_text("outside\n", encoding="utf-8")
        package_input.unlink()
        package_input.symlink_to(outside)

        with self.assertRaisesRegex(ValueError, "escaped"):
            self.create()

    def test_rejects_missing_effective_input_and_invalid_runtime_values(self):
        path = (
            self.contexts
            / "watcher/watcher-base/source-package-map.effective.txt"
        )
        path.unlink()
        with self.assertRaisesRegex(ValueError, "package input"):
            self.create()

        path.write_text("watcher watcher\n", encoding="utf-8")
        for overrides, message in (
            ({"tags": "bad tag"}, "tags"),
            ({"tags": "same,same"}, "unique"),
            ({"parallel": 0}, "positive"),
            ({"platform": "linux"}, "platform"),
            ({"registry": "registry.test/path"}, "registry"),
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaisesRegex(ValueError, message):
                    self.create(**overrides)

    def test_loaded_plan_requires_normalized_known_images(self):
        value = self.create()
        changed = value.to_dict()
        changed["images"] = changed["images"][1:]

        with self.assertRaisesRegex(ValueError, "normalized"):
            build_plan.from_dict(changed)

        changed_policy = dataclasses.replace(
            value, source_policy="clone-if-missing"
        )
        with self.assertRaisesRegex(ValueError, "prepared-only"):
            build_plan.from_dict(changed_policy.to_dict())

        changed = dataclasses.replace(
            value,
            images=(
                dataclasses.replace(value.images[0], image="unknown/image"),
                *value.images[1:],
            ),
        )
        with self.assertRaisesRegex(ValueError, "unknown images"):
            build_plan.from_dict(changed.to_dict())


if __name__ == "__main__":
    unittest.main()
