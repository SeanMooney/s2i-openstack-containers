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

from openstack_image_builder import packages
from openstack_image_builder import plan


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.repo_root = pathlib.Path(__file__).resolve().parents[1]
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace_root = pathlib.Path(self.temporary.name) / "workspace"
        for project, distribution in (
            ("requirements", None),
            ("watcher", "watcher"),
            ("cyborg", "cyborg"),
        ):
            repository = (
                self.workspace_root / "src/opendev.org/openstack" / project
            )
            (repository / ".git").mkdir(parents=True)
            if distribution:
                (repository / "setup.cfg").write_text(
                    f"[metadata]\nname = {distribution}\n", encoding="utf-8"
                )
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
        }
        self.input = {
            "workspace_root": str(self.workspace_root),
            "container_project": (
                "github.com/openstack-k8s-operators/s2i-openstack-containers"
            ),
            "projects": self.projects,
            "images": [
                "watcher/watcher-base",
                "cyborg/cyborg",
                "cyborg/cyborg-agent",
            ],
            "infer_images": True,
            "zuul_items": [
                {
                    "project": {
                        "canonical_name": (
                            "github.com/openstack-k8s-operators/"
                            "s2i-openstack-containers"
                        )
                    }
                }
            ],
            "zuul_project": {
                "canonical_name": (
                    "github.com/openstack-k8s-operators/"
                    "s2i-openstack-containers"
                )
            },
            "stream": "master",
            "image_mappings": {},
        }

    def _add_prepared_project(self, canonical_name, distribution):
        relative = f"src/{canonical_name}"
        repository = self.workspace_root / relative
        (repository / ".git").mkdir(parents=True)
        if distribution is not None:
            (repository / "setup.cfg").write_text(
                f"[metadata]\nname = {distribution}\n", encoding="utf-8"
            )
        self.projects[canonical_name] = {
            "src_dir": relative,
            "commit": f"{distribution or 'non-python'}-inventory-head",
        }
        return repository

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
            "watcher/watcher-base,cyborg/cyborg,cyborg/cyborg-agent",
            result["target_expression"],
        )
        self.assertEqual(["base", "watcher", "cyborg"], result["contexts"])
        self.assertEqual(3, result["version"])
        self.assertEqual("explicit", result["selection"]["reason"])
        self.assertEqual([], result["selection"]["changed_projects"])
        self.assertEqual(6, len(result["generated_files"]))
        generated = {
            item["destination"]: item for item in result["generated_files"]
        }
        self.assertIn(
            "watcher/watcher-base/source-package-map.effective.txt",
            generated,
        )
        self.assertEqual(
            "watcher watcher\n",
            generated["watcher/watcher-base/source-package-map.effective.txt"][
                "content"
            ],
        )
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

    def test_direct_cyborg_plan_keeps_empty_deployment_mappings(self):
        self.input["images"] = []
        self.input["zuul_items"].append(
            {"project": "opendev.org/openstack/cyborg"}
        )

        result = plan.create(self.repo_root, self.input)
        metadata = {item["image"]: item for item in result["image_metadata"]}

        self.assertEqual(
            ["base", "cyborg/cyborg", "cyborg/cyborg-agent"],
            result["images"],
        )
        self.assertEqual([], metadata["cyborg/cyborg"]["deployment_keys"])
        self.assertEqual(
            [], metadata["cyborg/cyborg-agent"]["deployment_keys"]
        )

    def test_direct_cyborg_inventory_mapping_opts_in_one_image(self):
        self.input["images"] = []
        self.input["zuul_items"].append(
            {"project": "opendev.org/openstack/cyborg"}
        )
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
        self.assertEqual(
            [], metadata["cyborg/cyborg-agent"]["deployment_keys"]
        )

    def test_transitive_project_extends_explicit_union_and_generated_inputs(
        self,
    ):
        project = "opendev.org/openstack/os-resource-classes"
        repository = self._add_prepared_project(project, "OS_Resource.Classes")
        self.input["images"] = ["watcher/watcher-base"]
        self.input["zuul_items"].append({"project": project})
        before = {
            path.relative_to(repository): path.read_bytes()
            for path in repository.rglob("*")
            if path.is_file()
        }

        result = plan.create(self.repo_root, self.input)

        self.assertEqual("explicit+transitive", result["selection"]["reason"])
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
            {
                project: [
                    "cyborg/cyborg",
                    "cyborg/cyborg-agent",
                    "watcher/watcher-base",
                ]
            },
            result["selection"]["transitive_affected_images_by_project"],
        )
        destinations = {item["destination"] for item in result["sources"]}
        for image in (
            "cyborg/cyborg",
            "cyborg/cyborg-agent",
            "watcher/watcher-base",
        ):
            self.assertIn(
                f"{image}/src/overrides/os-resource-classes",
                destinations,
            )
        generated = {
            item["destination"]: item for item in result["generated_files"]
        }
        watcher_map = generated[
            "watcher/watcher-base/source-package-map.effective.txt"
        ]["content"]
        self.assertEqual(
            "watcher watcher\n"
            "overrides/os-resource-classes os-resource-classes\n",
            watcher_map,
        )
        watcher_lock = generated[
            "watcher/watcher-base/requirements.lock.effective.master"
        ]["content"]
        self.assertNotIn("os-resource-classes==", watcher_lock)
        self.assertIn("openstacksdk==", watcher_lock)
        after = {
            path.relative_to(repository): path.read_bytes()
            for path in repository.rglob("*")
            if path.is_file()
        }
        self.assertEqual(before, after)

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

    def test_generated_destinations_must_be_safe_and_image_local(self):
        self.assertEqual(
            "watcher/watcher-base/generated.txt",
            packages.safe_generated_destination(
                "watcher/watcher-base",
                "watcher/watcher-base/generated.txt",
            ),
        )
        for destination in (
            "../generated.txt",
            "/tmp/generated.txt",
            "cyborg/cyborg/generated.txt",
            "watcher/watcher-base/../generated.txt",
        ):
            with self.subTest(destination=destination):
                with self.assertRaises(ValueError):
                    packages.safe_generated_destination(
                        "watcher/watcher-base", destination
                    )

    def test_runtime_source_path_collision_is_rejected_without_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = pathlib.Path(temporary)
            shutil.copytree(
                self.repo_root / "containers", repository / "containers"
            )
            (
                repository / "containers/watcher/watcher-base/sources.txt"
            ).write_text(
                "master watcher "
                "https://opendev.org/openstack/watcher-extra.git master "
                "deadbeef\n",
                encoding="utf-8",
            )
            self._add_prepared_project(
                "opendev.org/openstack/watcher-extra", None
            )

            with self.assertRaisesRegex(
                ValueError, "source package path collision"
            ):
                plan.create(repository, self.input)

    def test_direct_python_source_requires_static_distribution_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = pathlib.Path(temporary)
            shutil.copytree(
                self.repo_root / "containers", repository / "containers"
            )
            (
                repository / "containers/watcher/watcher-base/sources.txt"
            ).write_text(
                "master dynamic-lib "
                "https://opendev.org/openstack/dynamic-lib.git master "
                "deadbeef\n",
                encoding="utf-8",
            )
            prepared = self._add_prepared_project(
                "opendev.org/openstack/dynamic-lib", None
            )
            (prepared / "setup.py").write_text(
                "from setuptools import setup\n"
                "name = 'dynamic-lib'\n"
                "setup(name=name)\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError, "no static distribution name"
            ):
                plan.create(repository, self.input)

    def test_direct_non_python_source_remains_valid_and_unmapped(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = pathlib.Path(temporary)
            shutil.copytree(
                self.repo_root / "containers", repository / "containers"
            )
            (
                repository / "containers/watcher/watcher-base/sources.txt"
            ).write_text(
                "master policy-data "
                "https://opendev.org/openstack/policy-data.git master "
                "deadbeef\n",
                encoding="utf-8",
            )
            self._add_prepared_project(
                "opendev.org/openstack/policy-data", None
            )

            result = plan.create(repository, self.input)

        self.assertIn(
            "watcher/watcher-base/src/policy-data",
            {item["destination"] for item in result["sources"]},
        )
        watcher_packages = [
            item
            for item in result["source_packages"]
            if item["image"] == "watcher/watcher-base"
        ]
        self.assertEqual(
            ["watcher"], [item["source_path"] for item in watcher_packages]
        )

    def test_duplicate_distribution_across_source_scopes_is_rejected(self):
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

            with self.assertRaisesRegex(
                ValueError, "source distribution collision"
            ):
                plan.create(repository, self.input)

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
            self.assertEqual(3, json.loads(output_path.read_text())["version"])
            self.assertFalse(list(output_path.parent.glob(".plan.json.*")))


if __name__ == "__main__":
    unittest.main()
