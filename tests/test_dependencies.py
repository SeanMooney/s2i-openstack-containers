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

import pathlib
import shutil
import tempfile
import unittest

from openstack_image_builder import dependencies


class DependenciesTest(unittest.TestCase):
    def setUp(self):
        self.repo_root = pathlib.Path(__file__).resolve().parents[1]
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = pathlib.Path(self.temporary.name) / "workspace"
        self.projects = {}

    def _project(self, canonical_name, metadata=None, src_dir=None):
        relative = src_dir or f"src/{canonical_name}"
        repository = self.workspace / relative
        (repository / ".git").mkdir(parents=True)
        if metadata is not None:
            (repository / "setup.cfg").write_text(
                f"[metadata]\nname = {metadata}\n", encoding="utf-8"
            )
        self.projects[canonical_name] = {
            "src_dir": relative,
            "commit": f"{canonical_name}-head",
        }
        return repository

    def test_transitive_distribution_matches_all_effective_images(self):
        project = "opendev.org/openstack/os-resource-classes"
        self._project(project, "OS_Resource.Classes")

        affected, resolved = dependencies.resolve_transitive(
            self.repo_root,
            self.workspace,
            self.projects,
            [project],
            "master",
        )

        self.assertEqual(
            [
                "cyborg/cyborg",
                "cyborg/cyborg-agent",
                "watcher/watcher-base",
            ],
            affected[project],
        )
        self.assertEqual(["os-resource-classes"], resolved[0]["distributions"])

    def test_missing_metadata_and_unused_distribution_are_rejected(self):
        no_metadata = "opendev.org/openstack/no-metadata"
        self._project(no_metadata)
        with self.assertRaisesRegex(ValueError, "no static Python"):
            dependencies.resolve_transitive(
                self.repo_root,
                self.workspace,
                self.projects,
                [no_metadata],
                "master",
            )

        unused = "opendev.org/openstack/unused"
        self._project(unused, "definitely-unused")
        with self.assertRaisesRegex(ValueError, "not used by any image"):
            dependencies.resolve_transitive(
                self.repo_root,
                self.workspace,
                self.projects,
                [unused],
                "master",
            )

    def test_dependency_used_by_base_is_rejected(self):
        project = "opendev.org/openstack/pbr"
        self._project(project, "pbr")
        with self.assertRaisesRegex(ValueError, "used by base"):
            dependencies.resolve_transitive(
                self.repo_root,
                self.workspace,
                self.projects,
                [project],
                "master",
            )

    def test_missing_and_escaping_checkout_are_rejected(self):
        missing = "opendev.org/openstack/missing"
        self.projects[missing] = {"src_dir": "src/missing", "commit": "head"}
        with self.assertRaisesRegex(ValueError, "does not exist"):
            dependencies.resolve_transitive(
                self.repo_root,
                self.workspace,
                self.projects,
                [missing],
                "master",
            )

        escaping = "opendev.org/openstack/escaping"
        outside = pathlib.Path(self.temporary.name) / "outside"
        (outside / ".git").mkdir(parents=True)
        (outside / "setup.cfg").write_text(
            "[metadata]\nname = openstacksdk\n", encoding="utf-8"
        )
        self.workspace.mkdir(exist_ok=True)
        (self.workspace / "escape").symlink_to(
            outside, target_is_directory=True
        )
        self.projects[escaping] = {"src_dir": "escape", "commit": "head"}
        with self.assertRaisesRegex(ValueError, "beneath workspace_root"):
            dependencies.resolve_transitive(
                self.repo_root,
                self.workspace,
                self.projects,
                [escaping],
                "master",
            )

    def test_malformed_effective_dependency_input_is_rejected(self):
        project = "opendev.org/openstack/openstacksdk"
        self._project(project, "openstacksdk")
        repository = pathlib.Path(self.temporary.name) / "container-repository"
        shutil.copytree(
            self.repo_root / "containers", repository / "containers"
        )
        (
            repository / "containers/watcher/watcher-base/pythondeps.txt"
        ).write_text("not a valid requirement ???\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "unsupported requirement"):
            dependencies.resolve_transitive(
                repository,
                self.workspace,
                self.projects,
                [project],
                "master",
            )

    def test_same_destination_name_from_two_projects_is_rejected(self):
        first = "one.example/openstack/shared"
        second = "two.example/openstack/shared"
        self._project(first, "openstacksdk")
        self._project(second, "openstacksdk")
        with self.assertRaisesRegex(ValueError, "destination collision"):
            dependencies.resolve_transitive(
                self.repo_root,
                self.workspace,
                self.projects,
                [first, second],
                "master",
            )


if __name__ == "__main__":
    unittest.main()
