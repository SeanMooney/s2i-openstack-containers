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

"""Generate the local Zuul-shaped inventory contract."""

from __future__ import annotations

import json
import pathlib
import shutil
import sys

import yaml

from openstack_image_builder import projects
from openstack_image_builder.local import paths


def project_data(
    canonical_name: str, commit: str
) -> dict[str, object]:
    """Create the subset of ``zuul.projects`` consumed by shared planning."""
    src_dir = projects.safe_relative_path(
        f"src/{canonical_name}", f"src_dir for {canonical_name}"
    )
    return {
        "canonical_name": canonical_name,
        "src_dir": src_dir,
        "commit": commit,
        "required": True,
    }


def validate_builder(value: dict[str, object]) -> None:
    """Require the local ``builder`` pattern to resolve only to localhost."""
    try:
        builder = value["all"]["children"]["builder"]
        hosts = builder["hosts"]
    except (KeyError, TypeError) as error:
        raise ValueError("local inventory has no builder group") from error
    if not isinstance(hosts, dict) or set(hosts) != {"localhost"}:
        raise ValueError("local builder group must contain exactly localhost")
    localhost = hosts["localhost"]
    if (
        not isinstance(localhost, dict)
        or localhost.get("ansible_connection") != "local"
    ):
        raise ValueError("local builder must use ansible_connection=local")


def write(
    *,
    repo_root: pathlib.Path,
    workspace_root: pathlib.Path,
    output_dir: pathlib.Path,
    inventory_path: pathlib.Path,
    source_manifest_path: pathlib.Path,
    container_project: str,
    prepared_projects: dict[str, dict[str, object]],
    images: list[str],
    stream: str,
    tox_executable: str,
) -> dict[str, object]:
    """Write inventory and a source-authority manifest beneath local state."""
    tmp_root = repo_root.resolve() / ".tmp"
    workspace_root = paths.require_beneath(
        workspace_root, tmp_root, "local workspace"
    )
    output_dir = paths.require_beneath(output_dir, tmp_root, "local output")
    inventory_path = paths.require_beneath(
        inventory_path, tmp_root, "local inventory"
    )
    source_manifest_path = paths.require_beneath(
        source_manifest_path, tmp_root, "local source manifest"
    )
    if container_project not in prepared_projects:
        raise ValueError("container project is absent from prepared projects")

    zuul_projects: dict[str, dict[str, object]] = {}
    for canonical_name, prepared in sorted(prepared_projects.items()):
        commit = prepared.get("commit")
        if not isinstance(commit, str) or not commit:
            raise ValueError(f"prepared project has no commit: {canonical_name}")
        data = project_data(canonical_name, commit)
        paths.require_beneath(
            workspace_root / str(data["src_dir"]),
            workspace_root,
            f"workspace source for {canonical_name}",
        )
        zuul_projects[canonical_name] = data

    primary = zuul_projects[container_project]
    project = {
        "canonical_name": container_project,
        "name": container_project.split("/", 1)[1],
        "short_name": container_project.rsplit("/", 1)[-1],
        "src_dir": primary["src_dir"],
    }
    inventory = {
        "all": {
            "children": {
                "builder": {
                    "hosts": {
                        "localhost": {
                            "ansible_connection": "local",
                            "ansible_python_interpreter": sys.executable,
                        }
                    }
                }
            },
            "vars": {
                "zuul_user_dir": str(workspace_root),
                "tox_executable": tox_executable,
                "s2i_ci_container_project": container_project,
                "s2i_ci_content_provider": False,
                "s2i_ci_images": images,
                "s2i_ci_install_host_packages": False,
                "s2i_ci_install_tox": False,
                "s2i_ci_stream": stream,
                "zuul": {
                    "build": "local",
                    "buildset": "local",
                    "job": "s2i-openstack-containers-local",
                    "pipeline": "local",
                    "tenant": "local",
                    "project": project,
                    "projects": zuul_projects,
                    "items": [{"project": project}],
                    "executor": {
                        "work_root": str(workspace_root),
                        "src_root": str(workspace_root / "src"),
                        "log_root": str(output_dir / "logs"),
                    },
                },
            },
        }
    }
    validate_builder(inventory)
    source_manifest = {
        "version": 1,
        "authority": "maintained-pins-with-local-worktree-overlay",
        "container_project": container_project,
        "images": images,
        "stream": stream,
        "workspace_root": str(workspace_root),
        "projects": dict(sorted(prepared_projects.items())),
    }

    inventory_path.parent.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    source_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    inventory_path.write_text(
        yaml.safe_dump(inventory, sort_keys=False), encoding="utf-8"
    )
    source_manifest_path.write_text(
        json.dumps(source_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    log_dir = output_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(inventory_path, log_dir / "inventory.yaml")
    shutil.copy2(source_manifest_path, log_dir / "source-manifest.json")
    return inventory
