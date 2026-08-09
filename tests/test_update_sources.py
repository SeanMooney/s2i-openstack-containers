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

"""Tests for build.sh update-sources behavior."""

import os
import pathlib
import subprocess
import tempfile
import unittest


class UpdateSourcesTest(unittest.TestCase):
    def setUp(self):
        self.repo_root = pathlib.Path(__file__).resolve().parents[1]
        tmp_root = self.repo_root / ".tmp"
        tmp_root.mkdir(exist_ok=True)
        self.temporary_directory = tempfile.TemporaryDirectory(
            dir=tmp_root, prefix="update-sources-test."
        )
        self.addCleanup(self.temporary_directory.cleanup)
        self.test_root = pathlib.Path(self.temporary_directory.name)
        upstream_root = self.test_root / "upstream"
        upstream_root.mkdir()

        self.upstream_requirements = upstream_root / "requirements.git"
        self.requirements_old, self.requirements_new = self.create_remote(
            self.upstream_requirements,
            [
                {"upper-constraints.txt": "six==1.17.0\n"},
                {"upper-constraints.txt": ("six==1.17.0\npbr==7.0.3\n")},
            ],
        )
        self.upstream_service = upstream_root / "test-svc.git"
        self.service_old, self.service_new = self.create_remote(
            self.upstream_service,
            [
                {"requirements.txt": "six\npbr\n"},
                {"requirements.txt": "six\npbr\n# v2\n"},
            ],
        )
        (self.test_root / "build.sh").symlink_to(self.repo_root / "build.sh")
        self.project_root = self.test_root / "containers" / "test-svc"
        image_root = self.project_root / "test-svc"
        (self.project_root / "src").mkdir(parents=True)
        (image_root / "src").mkdir(parents=True)
        self.write_sources(
            "master upper-constraints "
            f"{self.upstream_requirements} master {self.requirements_old}\n"
            "master test-svc "
            f"{self.upstream_service} master {self.service_old}\n"
        )
        (image_root / "Containerfile").write_text(
            "FROM scratch\n", encoding="utf-8"
        )
        (image_root / "bindeps.txt").write_text("python3\n", encoding="utf-8")
        (image_root / "builddeps.txt").write_text("gcc\n", encoding="utf-8")
        (image_root / "pythondeps.txt").touch()
        (image_root / "pythonbuilddeps.txt").touch()

    def run_command(self, command, cwd=None):
        result = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            self.fail(
                f"command failed with {result.returncode}: {command}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result.stdout.strip()

    def create_remote(self, destination, commits):
        work = self.test_root / f"work-{destination.stem}"
        self.run_command(["git", "init", "-b", "master", str(work)])
        self.run_command(
            ["git", "config", "user.email", "test@example.com"], work
        )
        self.run_command(["git", "config", "user.name", "Test"], work)
        hashes = []
        for index, files in enumerate(commits, start=1):
            for name, content in files.items():
                (work / name).write_text(content, encoding="utf-8")
            self.run_command(["git", "add", "-A"], work)
            self.run_command(["git", "commit", "-m", f"v{index}"], work)
            hashes.append(self.run_command(["git", "rev-parse", "HEAD"], work))
        self.run_command(
            ["git", "clone", "--bare", str(work), str(destination)]
        )
        return hashes[0], hashes[-1]

    def write_sources(self, content):
        (self.project_root / "sources.txt").write_text(
            content, encoding="utf-8"
        )

    def run_update(self, **environment):
        command_environment = os.environ.copy()
        command_environment.update({"STREAM": "master"})
        command_environment.update(environment)
        result = subprocess.run(
            ["bash", "./build.sh", "update-sources", "test-svc"],
            cwd=self.test_root,
            check=False,
            capture_output=True,
            text=True,
            env=command_environment,
        )
        (self.test_root / "build.log").write_text(
            result.stdout + result.stderr, encoding="utf-8"
        )
        if result.returncode != 0:
            self.fail(
                f"update-sources failed with {result.returncode}:\n"
                f"{result.stdout}{result.stderr}"
            )
        return result.stdout + result.stderr

    def source_field(self, name, field):
        for line in (
            (self.project_root / "sources.txt")
            .read_text(encoding="utf-8")
            .splitlines()
        ):
            columns = line.split()
            if columns[:2] == ["master", name]:
                return columns[field - 1]
        self.fail(f"source entry was not found: {name}")

    def test_updates_hashes_to_branch_tip(self):
        self.run_update()

        self.assertEqual(
            self.requirements_new,
            self.source_field("upper-constraints", 5),
        )
        self.assertEqual(self.service_new, self.source_field("test-svc", 5))

    def test_fetches_upper_constraints(self):
        self.run_update()

        constraints = (
            self.project_root / "upper-constraints.txt.master"
        ).read_text(encoding="utf-8")
        self.assertIn("six==1.17.0", constraints)
        self.assertIn("pbr==7.0.3", constraints)

    def test_generates_rpms_in_yaml(self):
        self.run_update()

        rpms = (self.project_root / "rpms.in.yaml").read_text(encoding="utf-8")
        self.assertIn("python3", rpms)
        self.assertIn("gcc", rpms)

    def test_generates_requirements_lock(self):
        self.run_update()

        lock = (self.project_root / "requirements.lock.master").read_text(
            encoding="utf-8"
        )
        self.assertIn("six", lock)

    def test_generates_buildrequirements_lock(self):
        self.run_update()

        self.assertTrue(
            (self.project_root / "buildrequirements.lock.master").is_file()
        )

    def test_creates_default_stream_symlinks(self):
        self.run_update(DEFAULT_STREAM="master")

        expected = {
            "upper-constraints.txt": "upper-constraints.txt.master",
            "requirements.lock": "requirements.lock.master",
            "buildrequirements.lock": "buildrequirements.lock.master",
        }
        for name, target in expected.items():
            link = self.project_root / name
            self.assertTrue(link.is_symlink())
            self.assertEqual(target, os.readlink(link))

    def test_skips_symlinks_for_non_default_stream(self):
        self.run_update(DEFAULT_STREAM="other")

        for name in (
            "upper-constraints.txt",
            "requirements.lock",
            "buildrequirements.lock",
        ):
            self.assertFalse((self.project_root / name).is_symlink())

    def test_skip_hash_update_preserves_hashes(self):
        self.run_update(SKIP_HASH_UPDATE="1")

        self.assertEqual(
            self.requirements_old,
            self.source_field("upper-constraints", 5),
        )
        self.assertEqual(self.service_old, self.source_field("test-svc", 5))
        self.assertTrue(
            (self.project_root / "requirements.lock.master").is_file()
        )

    def test_skip_hash_update_uses_pinned_constraints(self):
        self.run_update(SKIP_HASH_UPDATE="1")

        constraints = (
            self.project_root / "upper-constraints.txt.master"
        ).read_text(encoding="utf-8")
        self.assertIn("six==1.17.0", constraints)
        self.assertNotIn("pbr", constraints)

    def test_hash_in_branch_field_selects_constraints_commit(self):
        self.write_sources(
            "master upper-constraints "
            f"{self.upstream_requirements} {self.requirements_old} "
            f"{self.requirements_new}\n"
            "master test-svc "
            f"{self.upstream_service} master {self.service_old}\n"
        )

        self.run_update()

        self.assertEqual(
            self.requirements_old,
            self.source_field("upper-constraints", 5),
        )
        constraints = (
            self.project_root / "upper-constraints.txt.master"
        ).read_text(encoding="utf-8")
        self.assertIn("six==1.17.0", constraints)
        self.assertNotIn("pbr", constraints)

    def test_hash_in_branch_field_selects_regular_repo_commit(self):
        self.write_sources(
            "master upper-constraints "
            f"{self.upstream_requirements} master {self.requirements_old}\n"
            "master test-svc "
            f"{self.upstream_service} {self.service_old} "
            f"{self.service_new}\n"
        )

        self.run_update()

        self.assertEqual(self.service_old, self.source_field("test-svc", 5))
        self.assertTrue(
            (self.project_root / "requirements.lock.master").is_file()
        )

    def test_lockfile_excludes_rpm_python_packages(self):
        image_root = self.project_root / "test-svc"
        (image_root / "bindeps.txt").write_text(
            "python3\npython3-six\n", encoding="utf-8"
        )

        output = self.run_update()

        lock = (self.project_root / "requirements.lock.master").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("six==", lock)
        self.assertIn("Filtering RPM-provided packages", output)

    def test_preexisting_checkout_is_preserved(self):
        source = self.project_root / "src" / "test-svc"
        source.mkdir()
        (source / "MARKER").write_text("local-dev\n", encoding="utf-8")
        (source / "requirements.txt").write_text(
            "six\npbr\n", encoding="utf-8"
        )

        self.run_update()

        self.assertEqual(
            "local-dev\n",
            (source / "MARKER").read_text(encoding="utf-8"),
        )
        self.assertEqual(self.service_old, self.source_field("test-svc", 5))


if __name__ == "__main__":
    unittest.main()
