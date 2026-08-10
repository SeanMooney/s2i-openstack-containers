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

from openstack_image_builder import projects
from openstack_image_builder.local import cache  # noqa: H306
from openstack_image_builder.local import git


class LocalGitCacheTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.repo_root = self.root / "container-project"
        self.repo_root.mkdir()
        (self.repo_root / ".tmp").mkdir()
        source = self.root / "source"
        source.mkdir()
        self._git("init", "--quiet", "--initial-branch=master", cwd=source)
        self._git("config", "user.email", "test@example.com", cwd=source)
        self._git("config", "user.name", "Test", cwd=source)
        (source / "content.txt").write_text("content\n", encoding="utf-8")
        self._git("add", "content.txt", cwd=source)
        self._git("commit", "--quiet", "-m", "fixture", cwd=source)
        self.commit = self._git("rev-parse", "HEAD", cwd=source).strip()
        self.remote = self.root / "remotes/openstack/example.git"
        self.remote.parent.mkdir(parents=True)
        self._git("clone", "--quiet", "--bare", str(source), str(self.remote))
        self.url = f"file://localhost{self.remote}"
        self.spec = cache.ProjectSpec(
            canonical_name=projects.canonical_project(self.url),
            url=self.url,
            declared_ref="master",
            commit=self.commit,
        )
        self.manager = cache.GitCache(
            self.repo_root, self.repo_root / ".tmp/git-cache"
        )

    def _git(self, *arguments, cwd=None):
        return subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout

    def test_cache_miss_then_offline_hit_and_local_clone(self):
        first = self.manager.ensure(self.spec)
        self.assertFalse(first["hit"])
        self.assertEqual(self.commit, first["resolved_commit"])

        self.remote.rename(self.remote.with_suffix(".offline"))
        destination = self.repo_root / ".tmp/workspace/example"
        second = self.manager.materialize(self.spec, destination)
        self.assertTrue(second["hit"])
        self.assertIsNone(second["fetched_ref"])

        self.assertEqual(
            self.commit, git.output("rev-parse", "HEAD", cwd=destination)
        )
        self.assertFalse(
            (destination / ".git/objects/info/alternates").exists()
        )

    def test_origin_mismatch_is_rejected(self):
        self.manager.ensure(self.spec)
        cache_path = self.manager.path_for(self.spec)
        git.run(
            "remote",
            "set-url",
            "origin",
            "https://example.invalid/openstack/example",
            cwd=cache_path,
        )

        with self.assertRaisesRegex(ValueError, "origin mismatch"):
            self.manager.ensure(self.spec)

    def test_corrupt_existing_cache_is_rejected(self):
        cache_path = self.manager.path_for(self.spec)
        cache_path.mkdir(parents=True)
        (cache_path / "not-git").write_text("broken\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "corrupt or is not bare"):
            self.manager.ensure(self.spec)

    def test_clear_preserves_other_cache_entries(self):
        self.manager.ensure(self.spec)
        sibling = self.manager.root / "unrelated.git"
        sibling.mkdir()

        self.manager.clear(self.spec)

        self.assertFalse(self.manager.path_for(self.spec).exists())
        self.assertTrue(sibling.exists())


if __name__ == "__main__":
    unittest.main()
