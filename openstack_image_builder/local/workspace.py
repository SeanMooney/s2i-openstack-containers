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

"""Disposable local checkouts and developer worktree overlays."""

from __future__ import annotations

import hashlib
import os
import pathlib
import shutil

from openstack_image_builder.local import git
from openstack_image_builder.local import paths


_EXCLUDED_PARTS = {".git", ".tmp", ".tox", ".venv", "__pycache__"}


def _excluded(relative: pathlib.PurePath) -> bool:
    return bool(_EXCLUDED_PARTS.intersection(relative.parts)) or any(
        part.endswith(".pyc") for part in relative.parts
    )


def dirty_checksum(repo_root: pathlib.Path) -> tuple[bool, str | None]:
    """Return a deterministic checksum of tracked and non-ignored changes."""
    status = git.output("status", "--porcelain=v1", "-z", cwd=repo_root)
    if not status:
        return False, None
    digest = hashlib.sha256(status.encode("utf-8", errors="surrogateescape"))
    diff = git.run("diff", "--binary", "HEAD", cwd=repo_root).stdout
    digest.update(diff.encode("utf-8", errors="surrogateescape"))
    untracked = git.run(
        "ls-files", "--others", "--exclude-standard", "-z", cwd=repo_root
    ).stdout
    for name in sorted(item for item in untracked.split("\0") if item):
        relative = pathlib.PurePosixPath(name)
        if _excluded(relative):
            continue
        source = repo_root / relative
        digest.update(name.encode("utf-8", errors="surrogateescape"))
        if source.is_symlink():
            digest.update(os.readlink(source).encode("utf-8"))
        elif source.is_file():
            digest.update(source.read_bytes())
    return True, digest.hexdigest()


def overlay_worktree(
    source: pathlib.Path, destination: pathlib.Path
) -> list[dict[str, str]]:
    """Overlay tracked and non-ignored worktree files onto a clean clone."""
    for child in destination.iterdir():
        if child.name != ".git":
            paths.remove(child)
    names = git.run(
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",
        cwd=source,
    ).stdout
    copied: list[dict[str, str]] = []
    for name in sorted(item for item in names.split("\0") if item):
        relative = pathlib.PurePosixPath(name)
        if _excluded(relative):
            continue
        source_path = source / relative
        if not source_path.exists() and not source_path.is_symlink():
            continue
        destination_path = destination / relative
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        if source_path.is_symlink():
            target = os.readlink(source_path)
            destination_path.symlink_to(target)
            content = target.encode("utf-8")
        else:
            shutil.copy2(source_path, destination_path)
            content = source_path.read_bytes()
        copied.append(
            {
                "path": relative.as_posix(),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    return copied


def prepare_current(
    repo_root: pathlib.Path,
    destination: pathlib.Path,
    strict: bool = False,
) -> dict[str, object]:
    """Clone HEAD and optionally apply the current developer worktree."""
    commit = git.output("rev-parse", "HEAD", cwd=repo_root)
    branch = git.output("branch", "--show-current", cwd=repo_root) or commit
    dirty, checksum = dirty_checksum(repo_root)
    if strict and dirty:
        raise ValueError("strict local preparation requires a clean worktree")
    paths.remove(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    git.run(
        "clone",
        "--quiet",
        "--no-checkout",
        str(repo_root),
        str(destination),
    )
    git.run("checkout", "--quiet", "--detach", commit, cwd=destination)
    copied = [] if strict else overlay_worktree(repo_root, destination)
    return {
        "branch": branch,
        "commit": commit,
        "dirty": dirty,
        "dirty_checksum": checksum,
        "overlay_files": copied,
        "authority": (
            "maintained-commit" if strict or not dirty else "local-worktree-overlay"
        ),
    }
