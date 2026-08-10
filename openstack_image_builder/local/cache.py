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

"""Persistent, exact-pin Git caches for the local lifecycle."""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import os
import pathlib
import shutil
import tempfile
import urllib.parse

from openstack_image_builder import projects
from openstack_image_builder.local import git
from openstack_image_builder.local import paths


@dataclasses.dataclass(frozen=True)
class ProjectSpec:
    """One immutable external checkout request."""

    canonical_name: str
    url: str
    declared_ref: str
    commit: str

    def validate(self) -> None:
        if projects.canonical_project(self.url) != self.canonical_name:
            raise ValueError(
                f"source URL does not match {self.canonical_name}: {self.url}"
            )
        parsed = urllib.parse.urlparse(self.url)
        if parsed.username or parsed.password:
            raise ValueError(
                f"source URL must not contain credentials: {self.canonical_name}"
            )
        projects.safe_relative_path(
            f"{self.canonical_name}.git", "cache project path"
        )
        if not self.declared_ref:
            raise ValueError(f"source has no declared ref: {self.canonical_name}")
        if len(self.commit) != 40 or any(
            character not in "0123456789abcdefABCDEF"
            for character in self.commit
        ):
            raise ValueError(
                f"source pin must be a full Git SHA: {self.canonical_name}"
            )


class GitCache:
    """Manage bare repositories beneath one persistent cache root."""

    def __init__(self, repo_root: pathlib.Path, cache_root: pathlib.Path):
        self.repo_root = repo_root.resolve()
        self.tmp_root = self.repo_root / ".tmp"
        self.root = paths.require_beneath(
            cache_root, self.tmp_root, "Git cache root"
        )

    def path_for(self, spec: ProjectSpec) -> pathlib.Path:
        spec.validate()
        return paths.require_beneath(
            self.root / f"{spec.canonical_name}.git",
            self.root,
            "Git cache repository",
        )

    @contextlib.contextmanager
    def _lock(self, spec: ProjectSpec):
        lock_name = spec.canonical_name.replace("/", "_") + ".lock"
        lock_path = self.root / ".locks" / lock_name
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            yield

    @staticmethod
    def _has_commit(cache_path: pathlib.Path, commit: str) -> bool:
        result = git.run(
            "cat-file",
            "-e",
            f"{commit}^{{commit}}",
            cwd=cache_path,
            check=False,
        )
        return result.returncode == 0

    @staticmethod
    def _validate_bare(cache_path: pathlib.Path) -> None:
        result = git.run(
            "rev-parse",
            "--is-bare-repository",
            cwd=cache_path,
            check=False,
        )
        if result.returncode != 0 or result.stdout.strip() != "true":
            raise ValueError(f"Git cache is corrupt or is not bare: {cache_path}")

    @staticmethod
    def _validate_origin(cache_path: pathlib.Path, expected: str) -> None:
        actual = git.output("remote", "get-url", "origin", cwd=cache_path)
        if actual != expected:
            raise ValueError(
                f"Git cache origin mismatch for {cache_path}: "
                f"expected {expected}, found {actual}"
            )

    def _initialize(self, spec: ProjectSpec, cache_path: pathlib.Path) -> None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = pathlib.Path(
            tempfile.mkdtemp(prefix=f".{cache_path.name}.", dir=cache_path.parent)
        )
        try:
            git.run("init", "--bare", "--quiet", str(temporary))
            git.run("remote", "add", "origin", spec.url, cwd=temporary)
            os.replace(temporary, cache_path)
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

    def _ensure_locked(
        self, spec: ProjectSpec, cache_path: pathlib.Path
    ) -> dict[str, object]:
        if not cache_path.exists():
            self._initialize(spec, cache_path)
        self._validate_bare(cache_path)
        self._validate_origin(cache_path, spec.url)
        hit = self._has_commit(cache_path, spec.commit)
        fetched_ref: str | None = None
        if not hit:
            direct = git.run(
                "fetch",
                "--quiet",
                "--tags",
                "origin",
                spec.commit,
                cwd=cache_path,
                check=False,
            )
            fetched_ref = spec.commit
            if direct.returncode != 0:
                git.run(
                    "fetch",
                    "--quiet",
                    "--tags",
                    "origin",
                    spec.declared_ref,
                    cwd=cache_path,
                )
                fetched_ref = spec.declared_ref
            if not self._has_commit(cache_path, spec.commit):
                raise ValueError(
                    f"fetch did not provide pinned commit {spec.commit} "
                    f"for {spec.canonical_name}"
                )
        resolved = git.output(
            "rev-parse", f"{spec.commit}^{{commit}}", cwd=cache_path
        )
        return {
            "canonical_name": spec.canonical_name,
            "url": spec.url,
            "cache_path": str(cache_path),
            "hit": hit,
            "fetched_ref": fetched_ref,
            "resolved_commit": resolved,
        }

    def ensure(self, spec: ProjectSpec) -> dict[str, object]:
        """Ensure one pinned object exists, fetching only on a cache miss."""
        cache_path = self.path_for(spec)
        with self._lock(spec):
            return self._ensure_locked(spec, cache_path)

    def _clone_locked(
        self,
        spec: ProjectSpec,
        cache_path: pathlib.Path,
        destination: pathlib.Path,
    ) -> None:
        self._validate_bare(cache_path)
        self._validate_origin(cache_path, spec.url)
        if not self._has_commit(cache_path, spec.commit):
            raise ValueError(
                f"pinned commit disappeared from cache: {spec.canonical_name}"
            )
        paths.remove(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        git.run("clone", "--quiet", str(cache_path), str(destination))
        git.run(
            "checkout", "--quiet", "--detach", spec.commit, cwd=destination
        )

    def clone(self, spec: ProjectSpec, destination: pathlib.Path) -> None:
        """Create an independent disposable clone while holding the cache lock."""
        cache_path = self.path_for(spec)
        with self._lock(spec):
            self._clone_locked(spec, cache_path, destination)

    def materialize(
        self, spec: ProjectSpec, destination: pathlib.Path
    ) -> dict[str, object]:
        """Hold one lock across exact fetch validation and local cloning."""
        cache_path = self.path_for(spec)
        with self._lock(spec):
            result = self._ensure_locked(spec, cache_path)
            self._clone_locked(spec, cache_path, destination)
            return result

    def refresh(self, spec: ProjectSpec) -> dict[str, object]:
        """Fetch a declared ref without changing the requested maintained pin."""
        cache_path = self.path_for(spec)
        with self._lock(spec):
            if not cache_path.exists():
                self._initialize(spec, cache_path)
            self._validate_bare(cache_path)
            self._validate_origin(cache_path, spec.url)
            git.run(
                "fetch",
                "--quiet",
                "--tags",
                "origin",
                spec.declared_ref,
                cwd=cache_path,
            )
            if not self._has_commit(cache_path, spec.commit):
                raise ValueError(
                    f"refresh did not provide pinned commit {spec.commit} "
                    f"for {spec.canonical_name}"
                )
            resolved = git.output(
                "rev-parse", f"{spec.commit}^{{commit}}", cwd=cache_path
            )
        return {
            "canonical_name": spec.canonical_name,
            "url": spec.url,
            "cache_path": str(cache_path),
            "hit": False,
            "fetched_ref": spec.declared_ref,
            "resolved_commit": resolved,
        }

    def prune(self, spec: ProjectSpec) -> None:
        cache_path = self.path_for(spec)
        with self._lock(spec):
            self._validate_bare(cache_path)
            self._validate_origin(cache_path, spec.url)
            git.run("gc", "--prune=now", cwd=cache_path)

    def clear(self, spec: ProjectSpec) -> None:
        cache_path = self.path_for(spec)
        with self._lock(spec):
            if cache_path.exists():
                self._validate_bare(cache_path)
                self._validate_origin(cache_path, spec.url)
                paths.remove(cache_path)

    def inspect(self, spec: ProjectSpec) -> dict[str, object]:
        cache_path = self.path_for(spec)
        with self._lock(spec):
            if not cache_path.exists():
                return {
                    "canonical_name": spec.canonical_name,
                    "cache_path": str(cache_path),
                    "exists": False,
                }
            self._validate_bare(cache_path)
            self._validate_origin(cache_path, spec.url)
            return {
                "canonical_name": spec.canonical_name,
                "cache_path": str(cache_path),
                "exists": True,
                "has_pin": self._has_commit(cache_path, spec.commit),
                "origin": spec.url,
            }
