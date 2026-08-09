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

import argparse
import json
import pathlib
import shutil
import tempfile
import unittest

from openstack_image_builder import plan


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.repo_root = pathlib.Path(__file__).resolve().parents[1]
        self.projects = {
            "github.com/openstack-k8s-operators/s2i-openstack-containers": {
                "src_dir": (
                    "src/github.com/openstack-k8s-operators/"
                    "s2i-openstack-containers"
                ),
                "commit": "container-head",
            },
            "opendev.org/openstack/requirements": {
                "src_dir": "src/opendev.org/openstack/requirements",
                "commit": "requirements-head",
            },
            "opendev.org/openstack/watcher": {
                "src_dir": "src/opendev.org/openstack/watcher",
                "commit": "watcher-head",
            },
            "opendev.org/openstack/cyborg": {
                "src_dir": "src/opendev.org/openstack/cyborg",
                "commit": "cyborg-head",
            },
            "opendev.org/openstack/glance": {
                "src_dir": "src/opendev.org/openstack/glance",
                "commit": "glance-head",
            },
        }
        self.input = {
            "workspace_root": "/home/zuul",
            "container_project": (
                "github.com/openstack-k8s-operators/s2i-openstack-containers"
            ),
            "projects": self.projects,
            "images": [
                "watcher/watcher-base",
                "cyborg/cyborg",
                "cyborg/cyborg-agent",
            ],
            "stream": "master",
            "image_mappings": {},
        }

    def test_explicit_plan_preserves_provider_contract(self):
        result = plan.create(self.repo_root, self.input)

        self.assertEqual(result, plan.create(self.repo_root, self.input))
        self.assertEqual(
            [
                "base",
                "watcher/watcher-base",
                "cyborg/cyborg",
                "cyborg/cyborg-agent",
            ],
            result["images"],
        )
        self.assertEqual(
            "base,watcher/watcher-base,cyborg/cyborg,cyborg/cyborg-agent",
            result["target_expression"],
        )
        self.assertEqual(["base", "watcher", "cyborg"], result["contexts"])
        metadata = {item["image"]: item for item in result["image_metadata"]}
        self.assertEqual(
            [
                "watcherAPIImage",
                "watcherApplierImage",
                "watcherDecisionEngineImage",
            ],
            metadata["watcher/watcher-base"]["deployment_keys"],
        )
        self.assertEqual([], metadata["cyborg/cyborg"]["deployment_keys"])
        self.assertEqual(
            "tracked", metadata["cyborg/cyborg"]["mapping_source"]
        )

        placements = {item["destination"]: item for item in result["sources"]}
        self.assertEqual(
            "requirements-head",
            placements["base/upper-constraints.txt.master"][
                "inventory_commit"
            ],
        )
        self.assertEqual(
            "watcher-head",
            placements["watcher/src/watcher"]["inventory_commit"],
        )
        self.assertEqual(
            "cyborg-head",
            placements["cyborg/src/cyborg"]["inventory_commit"],
        )
        self.assertTrue(
            all(
                item["authority"] == "zuul-prepared-workspace-head"
                for item in result["sources"]
            )
        )

    def test_glance_plan_uses_prepared_source_and_tracked_mapping(self):
        self.input["images"] = ["glance/glance-api"]

        result = plan.create(self.repo_root, self.input)

        self.assertEqual(["base", "glance/glance-api"], result["images"])
        self.assertEqual("base,glance/glance-api", result["target_expression"])
        self.assertEqual(["base", "glance"], result["contexts"])
        metadata = {item["image"]: item for item in result["image_metadata"]}
        self.assertEqual(
            ["glanceAPIImage"],
            metadata["glance/glance-api"]["deployment_keys"],
        )
        placements = {item["destination"]: item for item in result["sources"]}
        self.assertEqual(
            "glance-head",
            placements["glance/src/glance"]["inventory_commit"],
        )
        self.assertNotEqual(
            placements["base/upper-constraints.txt.master"][
                "maintained_commit"
            ],
            placements["glance/upper-constraints.txt.master"][
                "maintained_commit"
            ],
        )

    def test_inventory_mapping_replaces_tracked_keys(self):
        self.input["image_mappings"] = {"cyborg/cyborg": ["futureCyborgImage"]}
        result = plan.create(self.repo_root, self.input)
        metadata = {item["image"]: item for item in result["image_metadata"]}

        self.assertEqual(
            ["futureCyborgImage"],
            metadata["cyborg/cyborg"]["deployment_keys"],
        )
        self.assertEqual(
            "inventory", metadata["cyborg/cyborg"]["mapping_source"]
        )

    def test_duplicate_deployment_key_is_rejected(self):
        self.input["image_mappings"] = {"cyborg/cyborg": ["watcherAPIImage"]}
        with self.assertRaisesRegex(ValueError, "belongs to both"):
            plan.create(self.repo_root, self.input)

    def test_missing_required_project_is_rejected(self):
        del self.projects["opendev.org/openstack/watcher"]
        with self.assertRaisesRegex(ValueError, "absent from zuul.projects"):
            plan.create(self.repo_root, self.input)

    def test_unsafe_inventory_source_path_is_rejected(self):
        self.projects["opendev.org/openstack/watcher"]["src_dir"] = (
            "../executor"
        )
        with self.assertRaisesRegex(ValueError, "safe relative path"):
            plan.create(self.repo_root, self.input)

    def test_malformed_selected_source_manifest_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = pathlib.Path(temporary)
            shutil.copytree(
                self.repo_root / "containers", repository / "containers"
            )
            (repository / "containers/watcher/sources.txt").write_text(
                "master malformed record\n", encoding="utf-8"
            )

            with self.assertRaisesRegex(ValueError, "expected five fields"):
                plan.create(repository, self.input)

    def test_selected_image_source_scope_is_planned(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = pathlib.Path(temporary)
            shutil.copytree(
                self.repo_root / "containers", repository / "containers"
            )
            (
                repository / "containers/watcher/watcher-base/sources.txt"
            ).write_text(
                "master watcher-extra "
                "https://opendev.org/openstack/watcher.git master "
                "2b997f0a6b854c5370e0bd927c586b9c3ffa6893\n",
                encoding="utf-8",
            )

            result = plan.create(repository, self.input)

        self.assertIn(
            "watcher/watcher-base/src/watcher-extra",
            {item["destination"] for item in result["sources"]},
        )

    def test_missing_image_metadata_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = pathlib.Path(temporary)
            shutil.copytree(
                self.repo_root / "containers", repository / "containers"
            )
            (repository / "containers/cyborg/cyborg-agent/image.yaml").unlink()

            with self.assertRaisesRegex(ValueError, "missing image metadata"):
                plan.create(repository, self.input)

    def test_planner_only_writes_atomic_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            repository = root / "repository"
            shutil.copytree(
                self.repo_root / "containers", repository / "containers"
            )
            input_path = root / "input.json"
            output_path = root / "output" / "plan.json"
            input_path.write_text(json.dumps(self.input), encoding="utf-8")
            before = {
                path.relative_to(repository): path.read_bytes()
                for path in repository.rglob("*")
                if path.is_file()
            }

            plan.run(
                argparse.Namespace(
                    repo_root=str(repository),
                    input=str(input_path),
                    output=str(output_path),
                )
            )

            after = {
                path.relative_to(repository): path.read_bytes()
                for path in repository.rglob("*")
                if path.is_file()
            }
            self.assertEqual(before, after)
            self.assertEqual(1, json.loads(output_path.read_text())["version"])
            self.assertFalse(list(output_path.parent.glob(".plan.json.*")))


if __name__ == "__main__":
    unittest.main()
