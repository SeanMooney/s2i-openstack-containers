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

    def test_zuul_tox_jobs_use_python_312(self):
        self.assertFalse((self.repo_root / ".zuul.yaml").exists())
        configuration = self.read("zuul.d/jobs.yaml")

        self.assertIn(
            "name: s2i-openstack-containers-update-sources", configuration
        )
        self.assertEqual(4, configuration.count('python_version: "3.12"'))
        update_job = configuration.split(
            "name: s2i-openstack-containers-update-sources", 1
        )[1].split("\n- job:", 1)[0]
        self.assertNotIn("pre-run:", update_job)
        self.assertIn(
            "post-run: playbooks/testing/update-sources-diff.yaml",
            update_job,
        )
        self.assertIn('SKIP_HASH_UPDATE: "1"', update_job)
        self.assertNotIn("- project:", configuration)
        self.assertFalse(
            (
                self.repo_root / "playbooks/testing/update-sources-pre.yaml"
            ).exists()
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

    def test_github_workflows_use_the_runner_python(self):
        setup = self.read(".github/actions/setup-tox/action.yml")

        self.assertNotIn("setup-python", setup)
        self.assertNotIn("python-version", setup)
        self.assertIn("python3 -m pip install tox", setup)

    def test_generator_runtime_and_cache_are_canonical(self):
        tox = self.read("tox.ini")
        update_environment = tox.split("[testenv:update-sources]", 1)[1].split(
            "[testenv:build]", 1
        )[0]

        self.assertNotIn("basepython", update_environment)
        self.assertIn(
            "XDG_CACHE_HOME = {toxworkdir}/{envname}/cache", update_environment
        )

    def test_lock_generation_is_python_version_neutral(self):
        build = self.read("build.sh")

        self.assertIn(
            "pip-compile --allow-unsafe --no-annotate --strip-extras", build
        )
        self.assertIn("pybuild-deps compile \\\n      --no-annotate", build)
        self.assertEqual(3, build.count("normalize_generated_lock"))
        for directive in (
            "index-url",
            "extra-index-url",
            "trusted-host",
        ):
            self.assertIn(directive, build)
        for project in (
            "cyborg/cyborg",
            "glance/glance-api",
            "watcher/watcher-base",
        ):
            self.assertIn(
                "legacy-cgi",
                self.read(f"containers/{project}/pythondeps.txt"),
            )
        for lock in (self.repo_root / "containers").glob(
            "**/*requirements.lock.*"
        ):
            content = lock.read_text(encoding="utf-8")
            self.assertNotIn("    # via", content)
            self.assertNotIn("--index-url ", content)
            self.assertNotIn("--extra-index-url ", content)
            self.assertNotIn("--trusted-host ", content)


if __name__ == "__main__":
    unittest.main()
