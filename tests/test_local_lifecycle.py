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
import pathlib
import tempfile
import unittest
import unittest.mock

from openstack_image_builder import cli
from openstack_image_builder.local import lifecycle
from openstack_image_builder.local import state


class LocalLifecycleTest(unittest.TestCase):
    def _args(self, repo_root=".", keep=False):
        return argparse.Namespace(
            repo_root=repo_root,
            target="all",
            stream="master",
            namespace="s2i-ci",
            tag=None,
            parallel=1,
            registry_port=15000,
            strict_worktree=False,
            keep=keep,
        )

    def test_installed_cli_dispatches_local_ci(self):
        with unittest.mock.patch.object(
            lifecycle, "ci", autospec=True
        ) as local_ci:
            result = cli.main(["local", "ci", "--repo-root", "/repo"])

        self.assertEqual(0, result)
        local_ci.assert_called_once()
        self.assertEqual("/repo", local_ci.call_args.args[0].repo_root)

    def test_ci_always_cleans_after_run_failure(self):
        args = self._args()
        with (
            unittest.mock.patch.object(
                lifecycle, "prepare", autospec=True
            ) as prepare,
            unittest.mock.patch.object(
                lifecycle,
                "run",
                autospec=True,
                side_effect=ValueError("failed"),
            ) as run,
            unittest.mock.patch.object(
                lifecycle, "cleanup", autospec=True
            ) as cleanup,
        ):
            with self.assertRaisesRegex(ValueError, "failed"):
                lifecycle.ci(args)

        prepare.assert_called_once_with(args)
        run.assert_called_once_with(args)
        cleanup.assert_called_once_with(args)

    def test_keep_retains_state_after_success(self):
        args = self._args(keep=True)
        with (
            unittest.mock.patch.object(lifecycle, "prepare", autospec=True),
            unittest.mock.patch.object(lifecycle, "run", autospec=True),
            unittest.mock.patch.object(
                lifecycle, "cleanup", autospec=True
            ) as cleanup,
        ):
            lifecycle.ci(args)

        cleanup.assert_not_called()

    def test_registry_ownership_is_recorded_before_readiness(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo_root = pathlib.Path(temporary)
            (repo_root / ".tmp").mkdir()
            role_spec = lifecycle.cache.ProjectSpec(
                canonical_name="opendev.org/zuul/zuul-jobs",
                url="https://opendev.org/zuul/zuul-jobs",
                declared_ref="master",
                commit="role-commit",
            )
            manager = unittest.mock.Mock()
            manager.materialize.return_value = {"hit": True}

            def run_playbook(_args, _current, name, _extra=None):
                if name == "registry-ready":
                    raise ValueError("registry did not become ready")

            with (
                unittest.mock.patch.object(
                    lifecycle, "selected_images", return_value=[]
                ),
                unittest.mock.patch.object(
                    lifecycle, "source_specs", return_value=[]
                ),
                unittest.mock.patch.object(
                    lifecycle, "zuul_jobs_spec", return_value=role_spec
                ),
                unittest.mock.patch.object(
                    lifecycle.git,
                    "output",
                    side_effect=[
                        "https://opendev.org/example/containers",
                        "role-commit",
                    ],
                ),
                unittest.mock.patch.object(
                    lifecycle.workspace,
                    "prepare_current",
                    return_value={"commit": "container-commit"},
                ),
                unittest.mock.patch.object(
                    lifecycle.cache, "GitCache", return_value=manager
                ),
                unittest.mock.patch.object(lifecycle.inventory, "write"),
                unittest.mock.patch.object(
                    lifecycle, "_tox_executable", return_value="/usr/bin/tox"
                ),
                unittest.mock.patch.object(
                    lifecycle, "_run_playbook", side_effect=run_playbook
                ) as playbook,
            ):
                with self.assertRaisesRegex(
                    ValueError, "did not become ready"
                ):
                    lifecycle.prepare(self._args(repo_root=str(repo_root)))

            self.assertEqual(
                ["base", "registry", "registry-ready"],
                [call.args[2] for call in playbook.call_args_list],
            )
            record = state.LocalState(
                lifecycle.layout(repo_root).state_path
            ).read()
            self.assertEqual("failed", record["phase"])
            self.assertTrue(record["registry"]["owned"])

    def _prepared_cleanup(self, repo_root):
        current = lifecycle.layout(repo_root)
        current.inventory_path.parent.mkdir(parents=True, exist_ok=True)
        current.inventory_path.write_text("all: {}\n", encoding="utf-8")
        current.workspace_root.mkdir(parents=True)
        current.zuul_jobs_dir.mkdir(parents=True)
        current.registry_root.mkdir(parents=True)
        state.LocalState(current.state_path).write(
            {
                "phase": "failed",
                "ansible_started": True,
                "registry": {"owned": True},
                "options": {},
            }
        )
        return current

    def test_image_cleanup_failure_still_removes_registry_and_is_retryable(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            repo_root = pathlib.Path(temporary)
            current = self._prepared_cleanup(repo_root)

            def fail_images(_args, _current, name, _extra=None):
                if name == "post-images":
                    raise ValueError("image cleanup failed")

            with unittest.mock.patch.object(
                lifecycle, "_run_playbook", side_effect=fail_images
            ) as playbook:
                with self.assertRaisesRegex(
                    ValueError, "image cleanup failed"
                ):
                    lifecycle.cleanup(self._args(repo_root=str(repo_root)))

            self.assertEqual(
                ["post-logs", "post-images", "post"],
                [call.args[2] for call in playbook.call_args_list],
            )
            record = state.LocalState(current.state_path).read()
            self.assertEqual("cleanup-failed", record["phase"])
            self.assertFalse(record["registry"]["owned"])
            self.assertTrue(current.inventory_path.is_file())
            self.assertTrue(current.workspace_root.is_dir())

            with unittest.mock.patch.object(lifecycle, "_run_playbook"):
                lifecycle.cleanup(self._args(repo_root=str(repo_root)))

            record = state.LocalState(current.state_path).read()
            self.assertEqual("cleaned", record["phase"])
            self.assertFalse(current.inventory_path.exists())
            self.assertFalse(current.workspace_root.exists())

    def test_registry_cleanup_failure_retains_ownership_and_retry_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo_root = pathlib.Path(temporary)
            current = self._prepared_cleanup(repo_root)

            def fail_registry(_args, _current, name, _extra=None):
                if name == "post":
                    raise ValueError("registry cleanup failed")

            with unittest.mock.patch.object(
                lifecycle, "_run_playbook", side_effect=fail_registry
            ):
                with self.assertRaisesRegex(
                    ValueError, "registry cleanup failed"
                ):
                    lifecycle.cleanup(self._args(repo_root=str(repo_root)))

            record = state.LocalState(current.state_path).read()
            self.assertEqual("cleanup-failed", record["phase"])
            self.assertTrue(record["registry"]["owned"])
            self.assertTrue(current.registry_root.is_dir())
            self.assertTrue(current.inventory_path.is_file())

            with unittest.mock.patch.object(lifecycle, "_run_playbook"):
                lifecycle.cleanup(self._args(repo_root=str(repo_root)))

            record = state.LocalState(current.state_path).read()
            self.assertEqual("cleaned", record["phase"])
            self.assertFalse(record["registry"]["owned"])
            self.assertFalse(current.inventory_path.exists())
            self.assertFalse(current.registry_root.exists())

    def test_cleanup_preserves_cache_and_retained_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo_root = pathlib.Path(temporary)
            current = lifecycle.layout(repo_root)
            current.cache_root.mkdir(parents=True)
            current.output_dir.mkdir(parents=True)
            marker = current.cache_root / "marker"
            marker.write_text("cache\n", encoding="utf-8")
            output = current.output_dir / "result"
            output.write_text("retained\n", encoding="utf-8")
            state.LocalState(current.state_path).write(
                {
                    "phase": "failed",
                    "ansible_started": False,
                    "registry": {"owned": False},
                }
            )

            lifecycle.cleanup(self._args(repo_root=str(repo_root)))

            self.assertTrue(marker.is_file())
            self.assertTrue(output.is_file())
            self.assertEqual(
                "cleaned", state.LocalState(current.state_path).read()["phase"]
            )

    def test_explicit_target_resolution_does_not_infer_dependencies(self):
        repo_root = pathlib.Path(__file__).resolve().parents[1]

        self.assertEqual(
            ["watcher/watcher-base"],
            lifecycle.selected_images(repo_root, "watcher/watcher-base"),
        )
        self.assertEqual(
            ["cyborg/cyborg", "cyborg/cyborg-agent"],
            lifecycle.selected_images(repo_root, "cyborg"),
        )


if __name__ == "__main__":
    unittest.main()
