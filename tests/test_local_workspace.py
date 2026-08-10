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
import subprocess
import tempfile
import unittest

from openstack_image_builder.local import workspace


class LocalWorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self._git("init", "--quiet", "--initial-branch=main")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test")
        (self.repository / ".gitignore").write_text(
            "ignored/\n", encoding="utf-8"
        )
        (self.repository / "tracked.txt").write_text(
            "original\n", encoding="utf-8"
        )
        self._git("add", ".gitignore", "tracked.txt")
        self._git("commit", "--quiet", "-m", "fixture")

    def _git(self, *arguments):
        return subprocess.run(
            ["git", *arguments],
            cwd=self.repository,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout

    def test_developer_overlay_excludes_generated_state(self):
        (self.repository / "tracked.txt").write_text(
            "modified\n", encoding="utf-8"
        )
        (self.repository / "untracked.txt").write_text(
            "included\n", encoding="utf-8"
        )
        for relative in (".tmp/state", ".tox/state", "ignored/state"):
            path = self.repository / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("excluded\n", encoding="utf-8")
        destination = self.root / "workspace/project"

        result = workspace.prepare_current(self.repository, destination)

        self.assertTrue(result["dirty"])
        self.assertEqual("local-worktree-overlay", result["authority"])
        self.assertEqual(
            "modified\n",
            (destination / "tracked.txt").read_text(encoding="utf-8"),
        )
        self.assertTrue((destination / "untracked.txt").is_file())
        self.assertFalse((destination / ".tmp").exists())
        self.assertFalse((destination / ".tox").exists())
        self.assertFalse((destination / "ignored").exists())
        copied = {item["path"] for item in result["overlay_files"]}
        self.assertIn("tracked.txt", copied)
        self.assertIn("untracked.txt", copied)

    def test_dirty_checksum_is_deterministic(self):
        (self.repository / "tracked.txt").write_text(
            "modified\n", encoding="utf-8"
        )

        self.assertEqual(
            workspace.dirty_checksum(self.repository),
            workspace.dirty_checksum(self.repository),
        )

    def test_strict_mode_rejects_a_dirty_worktree(self):
        (self.repository / "untracked.txt").write_text(
            "dirty\n", encoding="utf-8"
        )

        with self.assertRaisesRegex(ValueError, "requires a clean worktree"):
            workspace.prepare_current(
                self.repository, self.root / "strict", strict=True
            )

    def test_deleted_tracked_file_remains_deleted(self):
        (self.repository / "tracked.txt").unlink()
        destination = self.root / "deleted"

        workspace.prepare_current(self.repository, destination)

        self.assertFalse((destination / "tracked.txt").exists())


if __name__ == "__main__":
    unittest.main()
