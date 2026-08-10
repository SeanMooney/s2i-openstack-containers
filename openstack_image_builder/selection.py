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

"""Select explicit and directly affected images without workspace mutation."""

from __future__ import annotations

import pathlib

from openstack_image_builder import images
from openstack_image_builder import sources


def canonical_project(value: object) -> str:
    """Normalize a Zuul project string or project-data object."""
    if isinstance(value, str) and value:
        return value
    if not isinstance(value, dict):
        raise ValueError("Zuul item project must be a string or object")
    canonical_name = value.get("canonical_name")
    if isinstance(canonical_name, str) and canonical_name:
        return canonical_name
    hostname = value.get("canonical_hostname")
    name = value.get("name")
    if (
        isinstance(hostname, str)
        and hostname
        and isinstance(name, str)
        and name
    ):
        return f"{hostname}/{name}"
    raise ValueError("Zuul item project has no canonical name")


def changed_projects(
    items: object, primary_project: object, container_project: str
) -> list[str]:
    """Return unique external projects from the speculative item sequence."""
    if items is None:
        items = []
    if not isinstance(items, list):
        raise ValueError("zuul_items must be a list")
    project_values: list[object] = []
    for item in items:
        if not isinstance(item, dict) or "project" not in item:
            raise ValueError("zuul_items entries must contain project")
        project_values.append(item["project"])
    if not project_values and primary_project is not None:
        project_values.append(primary_project)

    result: list[str] = []
    for value in project_values:
        project = canonical_project(value)
        if project != container_project and project not in result:
            result.append(project)
    return result


def explicit_images(
    containers_dir: pathlib.Path, requested: object
) -> list[str]:
    """Validate an optional explicit image sequence without reordering it."""
    if not isinstance(requested, list):
        raise ValueError("images must be a list")
    if any(not isinstance(image, str) or not image for image in requested):
        raise ValueError("images entries must be non-empty strings")
    if len(set(requested)) != len(requested):
        raise ValueError("images entries must be unique")
    available = set(images.discover(containers_dir))
    unknown = set(requested) - available
    if unknown:
        raise ValueError("unknown images: " + ", ".join(sorted(unknown)))
    return list(requested)


def directly_affected_images(
    repo_root: pathlib.Path, canonical_name: str, stream: str
) -> list[str]:
    """Map one primary source repository to images that stage it directly."""
    result: list[str] = []
    for image in images.discover(repo_root / "containers"):
        if image == "base":
            continue
        selected = ["base", image]
        contexts = images.context_scopes(selected)
        records = sources.placement_records(
            repo_root, selected, contexts, stream
        )
        if any(
            record["type"] == "repository"
            and record["canonical_name"] == canonical_name
            for record in records
        ):
            result.append(image)
    return result


def create(
    repo_root: pathlib.Path,
    requested: object,
    items: object,
    primary_project: object,
    container_project: str,
    stream: str,
    infer: object,
) -> dict[str, object]:
    """Create deterministic direct-selection diagnostics and final images."""
    if not isinstance(infer, bool):
        raise ValueError("infer_images must be a boolean")
    containers_dir = repo_root / "containers"
    available = images.discover(containers_dir)
    explicit = explicit_images(containers_dir, requested)
    changed = changed_projects(items, primary_project, container_project)
    affected: dict[str, list[str]] = {}
    inferred: list[str] = []

    if infer and changed:
        inferred_set: set[str] = set()
        for project in changed:
            matched = directly_affected_images(repo_root, project, stream)
            if not matched:
                raise ValueError(
                    "changed project is not referenced by a repository "
                    f"record in sources.txt: {project}"
                )
            affected[project] = matched
            inferred_set.update(matched)
        inferred = [image for image in available if image in inferred_set]
        reason = "explicit+direct" if explicit else "direct"
    elif explicit:
        reason = "explicit"
    elif infer:
        inferred = [image for image in available if image != "base"]
        reason = "all"
    else:
        raise ValueError("image selection is empty")

    services: list[str] = []
    for image in explicit:
        if image != "base" and image not in services:
            services.append(image)
    for image in inferred:
        if image != "base" and image not in services:
            services.append(image)
    selected = ["base", *services] if services else ["base"]
    if not services and "base" not in explicit:
        raise ValueError("image selection is empty")
    target_expression = ",".join(services) if services else "base"
    return {
        "reason": reason,
        "explicit_images": explicit,
        "changed_projects": changed,
        "inferred_images": inferred,
        "affected_images_by_project": affected,
        "images": selected,
        "target_expression": target_expression,
    }
