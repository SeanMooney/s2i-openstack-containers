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
import tempfile
import unittest

import yaml

from openstack_image_builder.local import inventory
from openstack_image_builder.local import state


class LocalInventoryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo_root = pathlib.Path(self.temporary.name)
        self.local_root = self.repo_root / ".tmp/local"
        self.workspace = self.local_root / "workspace"
        self.output = self.local_root / "zuul-output"
        self.workspace.mkdir(parents=True)
        self.output.mkdir(parents=True)
        self.inventory_path = self.local_root / "inventory.yaml"
        self.manifest_path = self.local_root / "source-manifest.json"
        self.container_project = "github.com/example/containers"
        self.projects = {
            self.container_project: {
                "commit": "container-head",
                "authority": "local-worktree-overlay",
            },
            "opendev.org/openstack/watcher": {
                "commit": "watcher-pin",
                "authority": "maintained-pin",
            },
        }

    def test_builder_group_maps_exactly_to_localhost(self):
        inventory.write(
            repo_root=self.repo_root,
            workspace_root=self.workspace,
            output_dir=self.output,
            inventory_path=self.inventory_path,
            source_manifest_path=self.manifest_path,
            container_project=self.container_project,
            prepared_projects=self.projects,
            images=["watcher/watcher-base"],
            stream="master",
            tox_executable="/venv/bin/tox",
        )

        value = yaml.safe_load(self.inventory_path.read_text(encoding="utf-8"))
        builder = value["all"]["children"]["builder"]
        self.assertEqual(["localhost"], list(builder["hosts"]))
        self.assertEqual(
            "local", builder["hosts"]["localhost"]["ansible_connection"]
        )
        variables = value["all"]["vars"]
        self.assertEqual(
            set(self.projects), set(variables["zuul"]["projects"])
        )
        self.assertEqual(["watcher/watcher-base"], variables["s2i_ci_images"])
        for project in variables["zuul"]["projects"].values():
            self.assertTrue(str(project["src_dir"]).startswith("src/"))
            self.assertNotIn(
                "..", pathlib.PurePosixPath(project["src_dir"]).parts
            )

    def test_missing_or_ambiguous_builder_group_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "no builder group"):
            inventory.validate_builder({"all": {"children": {}}})
        with self.assertRaisesRegex(ValueError, "exactly localhost"):
            inventory.validate_builder(
                {
                    "all": {
                        "children": {
                            "builder": {
                                "hosts": {
                                    "localhost": {
                                        "ansible_connection": "local"
                                    },
                                    "other": {"ansible_connection": "local"},
                                }
                            }
                        }
                    }
                }
            )

    def test_atomic_state_round_trip_and_partial_update(self):
        local_state = state.LocalState(self.local_root / "state.json")
        local_state.write({"phase": "preparing", "cache_entries": []})
        local_state.update(phase="prepared")

        value = local_state.read()
        self.assertEqual(1, value["version"])
        self.assertEqual("prepared", value["phase"])
        self.assertEqual([], value["cache_entries"])
        self.assertEqual([], list(self.local_root.glob(".state.json.*")))


if __name__ == "__main__":
    unittest.main()
