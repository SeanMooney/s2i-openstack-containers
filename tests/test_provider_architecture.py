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

    def _metadata_keys(self, path):
        keys = []
        for line in path.read_text(encoding="utf-8").splitlines():
            match = re.match(r"^    - (\S+)$", line)
            if match:
                keys.append(match.group(1))
        return keys

    def test_every_containerfile_has_local_image_metadata(self):
        containerfiles = sorted(
            (self.repo_root / "containers").glob("**/Containerfile")
        )

        self.assertEqual(4, len(containerfiles))
        for containerfile in containerfiles:
            metadata = containerfile.with_name("image.yaml")
            self.assertTrue(metadata.is_file(), metadata)
            self.assertIn(
                "openstack_version:\n  custom_container_images:",
                metadata.read_text(encoding="utf-8"),
            )

    def test_tracked_deployment_mappings_are_intentional(self):
        expected = {
            "containers/base/image.yaml": [],
            "containers/cyborg/cyborg/image.yaml": [],
            "containers/cyborg/cyborg-agent/image.yaml": [],
            "containers/watcher/watcher-base/image.yaml": [
                "watcherAPIImage",
                "watcherApplierImage",
                "watcherDecisionEngineImage",
            ],
        }

        for relative_path, keys in expected.items():
            self.assertEqual(
                keys,
                self._metadata_keys(self.repo_root / relative_path),
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
                "prepare-sources.yaml",
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

    def test_provider_job_keeps_direct_selection_builder_contract(self):
        zuul = self._read(".zuul.yaml")

        self.assertIn("name: builder", zuul)
        self.assertIn("nodeset: s2i-openstack-containers-image-builder", zuul)
        self.assertIn("s2i_ci_images: []", zuul)
        self.assertIn("s2i_ci_infer_images_from_dependencies: true", zuul)
        self.assertIn("deterministic union", zuul)
        self.assertNotIn("- project:", zuul)

    def test_provider_validates_repository_on_builder(self):
        run = self._read("playbooks/container-ci/shared/run.yaml")

        self.assertIn("ansible.builtin.stat:", run)
        self.assertIn("s2i_ci_build_entry_point.stat.isreg", run)
        self.assertIn("argv: [realpath, --canonicalize-existing", run)
        self.assertNotIn(" is file", run)
        self.assertNotIn(" | realpath", run)

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
        self.assertIn("s2i_ci_images | default([])", shared_run)
        self.assertIn("s2i_ci_selected_images | length > 0", shared_run)
        self.assertIn("s2i_ci_selected_images[0] == 'base'", shared_run)
        self.assertIn("'^(base|[A-Za-z0-9_.-]+/", shared_run)
        self.assertNotIn("s2i_ci_images | length > 0", shared_run)
        self.assertIn(
            "s2i_ci_content_provider | default(false) | bool", zuul_run
        )

    def test_c3_planner_and_staging_ownership_is_explicit(self):
        staging = self._read(
            "playbooks/container-ci/shared/prepare-sources.yaml"
        )
        run = self._read("playbooks/container-ci/shared/run.yaml")
        zuul_run = self._read("playbooks/container-ci/zuul/run.yaml")

        self.assertIn("installed side-effect-free OIB planner", staging)
        self.assertIn("source-placements.json", staging)
        self.assertIn("build-contexts.json", staging)
        self.assertIn("Atomically activate assembled contexts", staging)
        self.assertIn("../shared/prepare-sources.yaml", zuul_run)
        self.assertNotIn("image-metadata-item.yaml", run)
        self.assertNotIn("validate-mapping-overrides.yaml", run)
        self.assertIn('S2I_CONTEXTS_ROOT: "{{ s2i_ci_contexts_root }}"', run)
        self.assertIn('ERROR_ON_CLONE: "1"', run)
        self.assertIn("s2i_ci_selected_images | join(',')", run)
        self.assertIn("s2i_ci_backend_target_expression", run)

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

    def test_c4_local_adapter_does_not_leak_into_planner(self):
        planner = self.repo_root / "openstack_image_builder"
        local_modules = planner / "local"
        local_playbooks = self.container_ci / "local"

        self.assertTrue(local_modules.is_dir())
        self.assertTrue(local_playbooks.is_dir())
        planner_content = "\n".join(
            (planner / name).read_text(encoding="utf-8")
            for name in (
                "images.py",
                "plan.py",
                "projects.py",
                "selection.py",
                "sources.py",
            )
        )
        self.assertNotIn("openstack_image_builder.local", planner_content)
        for forbidden in (
            "shutil.copy",
            "subprocess",
            "buildah",
            "ansible-playbook",
        ):
            self.assertNotIn(forbidden, planner_content)

    def test_c5_plan_owns_direct_selection_without_transitive_parsing(self):
        selection = self._read("openstack_image_builder/selection.py")
        staging = self._read(
            "playbooks/container-ci/shared/prepare-sources.yaml"
        )

        self.assertIn("directly_affected_images", selection)
        self.assertIn("record[\"type\"] == \"repository\"", selection)
        self.assertIn("'zuul_items': zuul['items'] | default([])", staging)
        self.assertIn("s2i_ci_plan.version == 2", staging)
        for forbidden in ("setup.py", "pyproject.toml", "requirements.lock"):
            self.assertNotIn(forbidden, selection)

    def test_local_lifecycle_reuses_shared_run_ownership(self):
        local_run = self._read("playbooks/container-ci/local/run.yaml")
        tox = self._read("tox.ini")

        self.assertIn("../shared/prepare-sources.yaml", local_run)
        self.assertIn("../shared/run.yaml", local_run)
        self.assertIn("oib local {posargs}", tox)
        self.assertNotIn("build.sh", local_run)


if __name__ == "__main__":
    unittest.main()
