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
import unittest

from openstack_image_builder import selection


class SelectionTest(unittest.TestCase):
    def setUp(self):
        self.repo_root = pathlib.Path(__file__).resolve().parents[1]
        self.container_project = (
            "github.com/openstack-k8s-operators/s2i-openstack-containers"
        )
        self.container_item = {
            "project": {"canonical_name": self.container_project}
        }

    def _create(self, images=None, projects=None, infer=True):
        items = [self.container_item]
        for project in projects or []:
            items.append({"project": project})
        return selection.create(
            repo_root=self.repo_root,
            requested=images or [],
            items=items,
            primary_project=None,
            container_project=self.container_project,
            stream="master",
            infer=infer,
        )

    def test_explicit_watcher_preserves_request(self):
        result = self._create(images=["watcher/watcher-base"])

        self.assertEqual("explicit", result["reason"])
        self.assertEqual(["base", "watcher/watcher-base"], result["images"])
        self.assertEqual([], result["inferred_images"])

    def test_empty_selection_without_external_change_builds_all(self):
        result = self._create()

        self.assertEqual("all", result["reason"])
        self.assertEqual(
            [
                "base",
                "cyborg/cyborg",
                "cyborg/cyborg-agent",
                "watcher/watcher-base",
            ],
            result["images"],
        )

    def test_direct_watcher_change_selects_consolidated_image(self):
        project = "opendev.org/openstack/watcher"
        result = self._create(projects=[project])

        self.assertEqual("direct", result["reason"])
        self.assertEqual(["base", "watcher/watcher-base"], result["images"])
        self.assertEqual(
            ["watcher/watcher-base"],
            result["affected_images_by_project"][project],
        )

    def test_direct_cyborg_change_selects_both_images(self):
        project = "opendev.org/openstack/cyborg"
        result = self._create(projects=[project])

        self.assertEqual(
            ["base", "cyborg/cyborg", "cyborg/cyborg-agent"],
            result["images"],
        )

    def test_explicit_and_inferred_images_form_deterministic_union(self):
        result = self._create(
            images=["watcher/watcher-base"],
            projects=[
                "opendev.org/openstack/watcher",
                "opendev.org/openstack/cyborg",
            ],
        )

        self.assertEqual("explicit+direct", result["reason"])
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
            [
                "cyborg/cyborg",
                "cyborg/cyborg-agent",
                "watcher/watcher-base",
            ],
            result["inferred_images"],
        )

    def test_duplicate_zuul_items_are_folded_once(self):
        project = {"canonical_name": "opendev.org/openstack/watcher"}
        result = self._create(projects=[project, project])

        self.assertEqual(
            ["opendev.org/openstack/watcher"], result["changed_projects"]
        )

    def test_canonical_hostname_and_name_are_supported(self):
        result = self._create(
            projects=[
                {
                    "canonical_hostname": "opendev.org",
                    "name": "openstack/watcher",
                }
            ]
        )

        self.assertEqual(
            ["opendev.org/openstack/watcher"], result["changed_projects"]
        )

    def test_empty_selection_with_inference_disabled_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "selection is empty"):
            self._create(infer=False)

    def test_non_boolean_inference_flag_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "must be a boolean"):
            selection.create(
                repo_root=self.repo_root,
                requested=[],
                items=[self.container_item],
                primary_project=None,
                container_project=self.container_project,
                stream="master",
                infer="true",
            )

    def test_malformed_zuul_items_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "zuul_items must be a list"):
            selection.create(
                repo_root=self.repo_root,
                requested=[],
                items={},
                primary_project=None,
                container_project=self.container_project,
                stream="master",
                infer=True,
            )

    def test_unknown_explicit_image_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown images"):
            self._create(images=["unknown/image"])

    def test_unmatched_project_is_deferred_to_transitive_selection(self):
        with self.assertRaisesRegex(
            ValueError, "not referenced by a repository record"
        ):
            self._create(projects=["opendev.org/openstack/oslo.config"])

    def test_constraints_project_is_not_a_direct_service_source(self):
        with self.assertRaisesRegex(
            ValueError, "not referenced by a repository record"
        ):
            self._create(projects=["opendev.org/openstack/requirements"])


if __name__ == "__main__":
    unittest.main()
