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

"""Canonical project identities used by the side-effect-free planner."""

from __future__ import annotations

import pathlib
import urllib.parse


def canonical_project(url: str) -> str:
    """Return the canonical host/organization/project name for a Git URL."""
    if "://" in url:
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname
        project = parsed.path
    elif "@" in url and ":" in url:
        host_part, project = url.split(":", 1)
        host = host_part.rsplit("@", 1)[-1]
    else:
        raise ValueError(f"unsupported repository URL: {url}")

    if not host:
        raise ValueError(f"repository URL has no canonical host: {url}")
    project = project.strip("/")
    if project.endswith(".git"):
        project = project[:-4]
    if not project or "/" not in project:
        raise ValueError(
            f"repository URL has no organization/project path: {url}"
        )
    return f"{host.lower()}/{project}"


def safe_relative_path(value: object, description: str) -> str:
    """Validate a non-empty relative POSIX path without resolving it."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{description} must be a non-empty string")
    path = pathlib.PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ValueError(f"{description} must be a safe relative path")
    return path.as_posix()


def inventory_project(
    projects: dict[str, object], canonical_name: str
) -> dict[str, object]:
    """Validate and normalize one entry from ``zuul.projects``."""
    value = projects.get(canonical_name)
    if not isinstance(value, dict):
        raise ValueError(f"project is absent from zuul.projects: {canonical_name}")
    src_dir = safe_relative_path(
        value.get("src_dir"), f"zuul.projects[{canonical_name!r}].src_dir"
    )
    commit = value.get("commit")
    if not isinstance(commit, str) or not commit:
        raise ValueError(f"zuul.projects[{canonical_name!r}] has no commit")
    return {
        "canonical_name": canonical_name,
        "src_dir": src_dir,
        "inventory_commit": commit,
        "authority": "zuul-prepared-workspace-head",
    }
