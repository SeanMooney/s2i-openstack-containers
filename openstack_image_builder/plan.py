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

"""Produce a deterministic plan for builder-owned workspace staging."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import tempfile

from openstack_image_builder import images
from openstack_image_builder import projects
from openstack_image_builder import selection
from openstack_image_builder import sources


PLAN_VERSION = 2


def load_input(path: pathlib.Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("plan input must be a JSON object")
    return value


def required_string(value: dict[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ValueError(f"plan input has no {key}")
    return item


def create(repo_root: pathlib.Path, value: dict[str, object]) -> dict[str, object]:
    """Create a plan without reading or changing prepared source checkouts."""
    containers_dir = repo_root / "containers"
    if not containers_dir.is_dir():
        raise ValueError(f"repository has no containers directory: {repo_root}")

    workspace_root = required_string(value, "workspace_root")
    if not pathlib.PurePosixPath(workspace_root).is_absolute():
        raise ValueError("workspace_root must be an absolute path")
    container_project = required_string(value, "container_project")
    stream = required_string(value, "stream")
    project_values = value.get("projects")
    if not isinstance(project_values, dict):
        raise ValueError("plan input projects must be a JSON object")

    selected_data = selection.create(
        repo_root=repo_root,
        requested=value.get("images", []),
        items=value.get("zuul_items", []),
        primary_project=value.get("zuul_project"),
        container_project=container_project,
        stream=stream,
        infer=value.get("infer_images", True),
    )
    selected = selected_data["images"]
    target_expression = selected_data["target_expression"]
    contexts = images.context_scopes(selected)
    metadata = images.effective_metadata(
        repo_root, selected, value.get("image_mappings", {})
    )
    placements = sources.placement_records(
        repo_root, selected, contexts, stream
    )

    project_names = [container_project]
    for placement in placements:
        canonical_name = placement["canonical_name"]
        if canonical_name not in project_names:
            project_names.append(canonical_name)
    planned_projects = [
        projects.inventory_project(project_values, name) for name in project_names
    ]
    projects_by_name = {
        item["canonical_name"]: item for item in planned_projects
    }
    planned_sources: list[dict[str, object]] = []
    for placement in placements:
        project = projects_by_name[placement["canonical_name"]]
        planned_sources.append(
            {
                **placement,
                "src_dir": project["src_dir"],
                "inventory_commit": project["inventory_commit"],
                "authority": project["authority"],
            }
        )

    return {
        "version": PLAN_VERSION,
        "workspace_root": workspace_root,
        "container_project": container_project,
        "stream": stream,
        "selection": selected_data,
        "images": selected,
        "target_expression": target_expression,
        "contexts": contexts,
        "projects": planned_projects,
        "image_metadata": metadata,
        "sources": planned_sources,
    }


def write_atomic(path: pathlib.Path, value: dict[str, object]) -> None:
    """Atomically write the planner's only output."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(value, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as stream:
        temporary = pathlib.Path(stream.name)
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def run(args: argparse.Namespace) -> None:
    repo_root = pathlib.Path(args.repo_root).resolve()
    input_path = pathlib.Path(args.input).resolve()
    output_path = pathlib.Path(args.output).resolve()
    write_atomic(output_path, create(repo_root, load_input(input_path)))


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
