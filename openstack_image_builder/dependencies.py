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

"""Match prepared Python projects to container dependency inputs."""

from __future__ import annotations

import pathlib

from openstack_image_builder import images
from openstack_image_builder import projects
from openstack_image_builder import python_metadata


_DEPENDENCY_FILENAMES = ("pythondeps.txt", "pythonbuilddeps.txt")


def dependency_files(
    containers_dir: pathlib.Path, image: str, stream: str
) -> list[pathlib.Path]:
    """Return effective dependency inputs for one image in precedence order."""
    scopes = [containers_dir / "base"]
    if image != "base":
        project, _name = image.split("/", 1)
        scopes.extend((containers_dir / project, containers_dir / image))
    result: list[pathlib.Path] = []
    for scope in scopes:
        names = (
            f"requirements.lock.{stream}",
            f"buildrequirements.lock.{stream}",
            *_DEPENDENCY_FILENAMES,
        )
        for name in names:
            candidate = scope / name
            if candidate.is_file() and candidate not in result:
                result.append(candidate)
    return result


def image_dependencies(
    containers_dir: pathlib.Path, image: str, stream: str
) -> set[str]:
    """Return normalized Python distributions consumed by one image."""
    result: set[str] = set()
    for path in dependency_files(containers_dir, image, stream):
        result.update(python_metadata.requirement_names(path))
    return result


def prepared_repository(
    workspace_root: pathlib.Path, project: dict[str, object]
) -> pathlib.Path:
    """Resolve an existing inventory checkout safely beneath the workspace."""
    try:
        root = workspace_root.resolve(strict=True)
    except OSError as error:
        raise ValueError(
            f"workspace_root does not exist: {workspace_root}"
        ) from error
    source = root / str(project["src_dir"])
    try:
        repository = source.resolve(strict=True)
    except OSError as error:
        raise ValueError(f"prepared repository does not exist: {source}") from error
    try:
        repository.relative_to(root)
    except ValueError as error:
        raise ValueError(
            "prepared repository must remain beneath workspace_root: "
            f"{project['canonical_name']}"
        ) from error
    if not repository.is_dir() or not (repository / ".git").exists():
        raise ValueError(f"prepared repository is not a Git checkout: {repository}")
    return repository


def resolve_transitive(
    repo_root: pathlib.Path,
    workspace_root: pathlib.Path,
    project_values: dict[str, object],
    canonical_names: list[str],
    stream: str,
) -> tuple[dict[str, list[str]], list[dict[str, object]]]:
    """Resolve unmatched changed projects through static Python dependencies."""
    containers_dir = repo_root / "containers"
    available = images.discover(containers_dir)
    dependencies_by_image = {
        image: image_dependencies(containers_dir, image, stream)
        for image in available
    }
    affected: dict[str, list[str]] = {}
    resolved: list[dict[str, object]] = []
    destinations: dict[tuple[str, str], str] = {}
    for canonical_name in canonical_names:
        project = projects.inventory_project(project_values, canonical_name)
        repository = prepared_repository(workspace_root, project)
        distributions = python_metadata.distribution_names(repository)
        if not distributions:
            raise ValueError(
                "changed project has no static Python package metadata and is "
                f"not referenced by sources.txt: {canonical_name}"
            )
        matches = [
            image
            for image in available
            if set(distributions) & dependencies_by_image[image]
        ]
        if "base" in matches:
            raise ValueError(
                "source-building an additional dependency used by base is not "
                f"supported: {canonical_name}"
            )
        if not matches:
            raise ValueError(
                "changed project distributions are not used by any image: "
                f"{canonical_name} ({', '.join(distributions)})"
            )
        source_name = canonical_name.rsplit("/", 1)[-1]
        if pathlib.PurePosixPath(source_name).name != source_name or not source_name:
            raise ValueError(f"transitive project has an unsafe source name: {canonical_name}")
        for image in matches:
            key = (image, source_name)
            previous = destinations.get(key)
            if previous and previous != canonical_name:
                raise ValueError(
                    f"transitive source destination collision for {image}: "
                    f"{previous} and {canonical_name}"
                )
            destinations[key] = canonical_name
        affected[canonical_name] = matches
        resolved.append(
            {
                **project,
                "source_name": source_name,
                "distributions": distributions,
                "affected_images": matches,
            }
        )
    return affected, resolved
