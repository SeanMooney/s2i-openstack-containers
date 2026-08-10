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

"""Architecture checks for pinned source reproducibility."""

import pathlib
import unittest


class ReproducibilityArchitectureTest(unittest.TestCase):
    def setUp(self):
        self.repo_root = pathlib.Path(__file__).resolve().parents[1]

    def read(self, path):
        return (self.repo_root / path).read_text(encoding="utf-8")

    def test_unattached_zuul_job_uses_pinned_python_314_generation(self):
        configuration = self.read(".zuul.yaml")

        self.assertIn(
            "name: s2i-openstack-containers-update-sources", configuration
        )
        self.assertIn(
            "pre-run: playbooks/testing/update-sources-pre.yaml",
            configuration,
        )
        self.assertIn(
            "post-run: playbooks/testing/update-sources-diff.yaml",
            configuration,
        )
        self.assertIn('SKIP_HASH_UPDATE: "1"', configuration)
        self.assertNotIn("- project:", configuration)
        self.assertIn(
            'python_version: "3.14"',
            self.read("playbooks/testing/update-sources-pre.yaml"),
        )

    def test_post_run_preserves_manifest_and_checks_tracked_diff(self):
        post_run = self.read("playbooks/testing/update-sources-diff.yaml")

        self.assertIn(
            'source_refs_manifest: "{{ zuul.project.src_dir }}/.tmp/source-maintenance/frozen-source-refs.master.tsv"',
            post_run,
        )
        self.assertIn(
            'source_refs_log_dir: "{{ ansible_user_dir }}/zuul-output/logs/source-maintenance"',
            post_run,
        )
        self.assertNotIn("zuul_output_dir", post_run)
        self.assertIn("remote_src: true", post_run)
        self.assertIn("source_refs_manifest_stat.stat.exists", post_run)
        self.assertIn("git", post_run)
        self.assertIn("diff", post_run)
        self.assertIn("--exit-code", post_run)

    def test_generator_runtime_and_cache_are_canonical(self):
        tox = self.read("tox.ini")
        update_environment = tox.split("[testenv:update-sources]", 1)[1].split(
            "[testenv:build]", 1
        )[0]

        self.assertIn("basepython = python3.14", update_environment)
        self.assertIn(
            "XDG_CACHE_HOME = {toxworkdir}/{envname}/cache", update_environment
        )

    def test_only_source_updater_selects_python_314(self):
        action = self.read(".github/actions/setup-tox/action.yml")
        updater = self.read(".github/workflows/update-sources.yml")
        other_workflows = [
            path.read_text(encoding="utf-8")
            for path in (self.repo_root / ".github/workflows").glob("*.yml")
            if path.name != "update-sources.yml"
        ]

        self.assertIn("python-version:", action)
        self.assertIn('default: "3.x"', action)
        self.assertIn('python-version: "3.14"', updater)
        self.assertTrue(
            all(
                'python-version: "3.14"' not in text
                for text in other_workflows
            )
        )


if __name__ == "__main__":
    unittest.main()
