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
import re
import unittest


class ProviderArchitectureTest(unittest.TestCase):
    def setUp(self):
        self.repo_root = pathlib.Path(__file__).resolve().parents[1]
        self.container_ci = self.repo_root / "playbooks" / "container-ci"

    def _read(self, path):
        return (self.repo_root / path).read_text(encoding="utf-8")

    def _image_mappings(self):
        mappings = {}
        current_target = None
        for line in self._read("containers/image-mappings.yaml").splitlines():
            target = re.match(r"^    (\S+):$", line)
            if target:
                current_target = target.group(1)
                mappings[current_target] = []
                continue
            key = re.match(r"^      - (\S+)$", line)
            if key and current_target:
                mappings[current_target].append(key.group(1))
        return mappings

    def test_deployment_mappings_are_centralized(self):
        self.assertTrue(
            (self.repo_root / "containers/image-mappings.yaml").is_file()
        )
        self.assertFalse(
            list((self.repo_root / "containers").glob("**/image.yaml"))
        )

    def test_tracked_deployment_mappings_are_intentional(self):
        self.assertEqual(
            {
                "glance/glance-api": ["glanceAPIImage"],
                "manila/manila-api": ["manilaAPIImage"],
                "manila/manila-scheduler": ["manilaSchedulerImage"],
                "manila/manila-share": ["manilaShareImage"],
                "watcher/watcher-base": [
                    "watcherAPIImage",
                    "watcherApplierImage",
                    "watcherDecisionEngineImage",
                ],
            },
            self._image_mappings(),
        )

    def test_container_ci_mutations_target_only_builder(self):
        playbooks = sorted(self.container_ci.glob("**/*.yaml"))

        self.assertTrue(playbooks)
        for playbook in playbooks:
            content = playbook.read_text(encoding="utf-8")
            self.assertNotIn("hosts: all", content, playbook)
            self.assertNotIn("hosts: localhost", content, playbook)
            self.assertNotIn("delegate_to: localhost", content, playbook)
            self.assertNotIn("local_action:", content, playbook)
            for host_pattern in re.findall(r"^  hosts: (.+)$", content, re.M):
                self.assertEqual("builder", host_pattern, playbook)

    def test_exact_cleanup_does_not_hide_failures(self):
        cleanup = self._read(
            "playbooks/container-ci/shared/cleanup-images.yaml"
        )

        self.assertNotIn("failed_when: false", cleanup)
        self.assertIn("podman", cleanup)
        self.assertIn("buildah", cleanup)
        self.assertIn(
            "Require exact workflow references to be absent", cleanup
        )

    def test_shared_and_zuul_ownership_is_separated(self):
        shared = {
            path.name for path in (self.container_ci / "shared").glob("*")
        }
        zuul = {path.name for path in (self.container_ci / "zuul").glob("*")}

        self.assertTrue(
            {
                "prepare-host.yaml",
                "validate-registry.yaml",
                "run.yaml",
                "cleanup-images.yaml",
            }.issubset(shared)
        )
        self.assertTrue(
            {
                "pre.yaml",
                "run.yaml",
                "post.yaml",
                "reset-static-node.yaml",
                "content-provider-return.yaml",
            }.issubset(zuul)
        )
        self.assertNotIn("content-provider-return.yaml", shared)

    def test_provider_job_keeps_explicit_builder_contract(self):
        zuul = self._read("zuul.d/jobs.yaml")

        self.assertIn("name: builder", zuul)
        self.assertEqual(
            2,
            zuul.count("nodeset: s2i-openstack-containers-image-builder"),
        )
        self.assertIn("s2i_ci_images: all", zuul)
        self.assertNotIn("- project:", zuul)
        self.assertNotIn("abstract: true", zuul)

    def test_upstream_github_check_runs_configured_jobs(self):
        layout = self._read("zuul.d/projects.yaml")
        jobs = re.findall(r"^        - (\S+):$", layout, re.M)

        self.assertEqual(
            [
                "s2i-openstack-containers-molecule",
                "s2i-openstack-container-content-provider",
            ],
            jobs,
        )
        self.assertIn("irrelevant-files:", layout)
        self.assertNotIn("noop", layout)

    def test_provider_validates_repository_on_builder(self):
        run = self._read("playbooks/container-ci/shared/run.yaml")

        self.assertIn("ansible.builtin.stat:", run)
        self.assertIn("s2i_ci_build_entry_point.stat.isreg", run)
        self.assertIn("argv: [realpath, --canonicalize-existing", run)
        self.assertNotIn(" is file", run)
        self.assertNotIn(" | realpath", run)

    def test_central_mapping_validates_all_tracked_entries(self):
        loader = self._read(
            "playbooks/container-ci/shared/load-image-mappings.yaml"
        )
        item = self._read(
            "playbooks/container-ci/shared/validate-tracked-mapping-item.yaml"
        )

        self.assertIn("resolve", loader)
        self.assertIn(
            "difference(s2i_ci_available_images.stdout_lines)", loader
        )
        self.assertIn("Validate every tracked image mapping", loader)
        self.assertIn("globally duplicated tracked deployment keys", loader)
        self.assertIn("s2i_ci_tracked_mapping_keys is sequence", item)
        self.assertIn("reject('match', '^[A-Za-z][A-Za-z0-9]*$')", item)

    def test_inventory_provider_inputs_are_not_masked_by_play_vars(self):
        shared_run = self._read("playbooks/container-ci/shared/run.yaml")
        zuul_run = self._read("playbooks/container-ci/zuul/run.yaml")
        prepare_host = self._read(
            "playbooks/container-ci/shared/prepare-host.yaml"
        )

        shared_play_vars = shared_run.split("  tasks:", 1)[0]
        for variable in (
            "s2i_ci_namespace:",
            "s2i_ci_tag:",
            "s2i_ci_stream:",
            "s2i_ci_parallel:",
            "s2i_ci_images:",
            "s2i_ci_image_mappings:",
        ):
            self.assertNotIn(variable, shared_play_vars)
        self.assertNotIn("s2i_ci_content_provider: false", zuul_run)
        self.assertNotIn("s2i_ci_install_host_packages: true", prepare_host)
        self.assertIn("s2i_ci_images | default('all')", shared_run)
        self.assertIn("s2i_ci_target_expression", shared_run)
        self.assertIn("build.sh", shared_run)
        self.assertIn("resolve", shared_run)
        self.assertIn(
            "s2i_ci_content_provider | default(false) | bool", zuul_run
        )

    def test_return_contract_is_selective_and_secret_free(self):
        returned = self._read(
            "playbooks/container-ci/zuul/content-provider-return.yaml"
        )

        for field in (
            "s2i_ci_content:",
            "content_provider_os_custom_container_images:",
            'content_provider_os_registry_url: "null"',
            "content_provider_dlrn_md5_hash:",
            "content_provider_gating_repo_available: false",
            "content_provider_gating_repo_url:",
            "content_provider_registry_ip:",
            "content_provider_registry_ip_port:",
            "cifmw_build_images_output: {}",
            "pause: true",
        ):
            self.assertIn(field, returned)
        for secret in ("password", "username", "auth_file", "cert_dir"):
            self.assertNotIn(secret, returned)
        self.assertIn("s2i_ci_public_registry_endpoint", returned)
        self.assertNotIn("s2i_ci_registry.endpoint", returned)

    def test_watcher_image_is_process_neutral(self):
        containerfile = self._read(
            "containers/watcher/watcher-base/Containerfile"
        )
        bindeps = set(
            line
            for line in self._read(
                "containers/watcher/watcher-base/bindeps.txt"
            ).splitlines()
            if line and not line.startswith("#")
        )

        self.assertIn(
            "Consolidated Watcher API, applier, and decision-engine",
            containerfile,
        )
        self.assertTrue(
            {
                "httpd",
                "python3-mod_wsgi",
                "libffi",
                "libxml2",
                "libxslt",
            }.issubset(bindeps)
        )

    def test_c2_has_no_oib_or_local_adapter(self):
        self.assertFalse((self.repo_root / "openstack_image_builder").exists())
        self.assertFalse((self.container_ci / "local").exists())
        all_content = "\n".join(
            path.read_text(encoding="utf-8")
            for path in self.container_ci.glob("**/*.yaml")
        )
        for forbidden in ("S2I_CONTEXTS_ROOT", "ERROR_ON_CLONE", "oib"):
            self.assertNotIn(forbidden, all_content)


if __name__ == "__main__":
    unittest.main()
