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

"""Plan per-image source package maps and effective constraint inputs."""

from __future__ import annotations

import hashlib
import pathlib

from openstack_image_builder import dependencies
from openstack_image_builder import python_metadata


def _relative_source_path(image: str, destination: str) -> str | None:
    project, _name = image.split("/", 1)
    prefixes = (f"{project}/src/", f"{image}/src/")
    for prefix in prefixes:
        if destination.startswith(prefix):
            value = destination.removeprefix(prefix)
            path = pathlib.PurePosixPath(value)
            if path.is_absolute() or ".." in path.parts or not value:
                raise ValueError(f"unsafe source package path: {destination}")
            return path.as_posix()
    return None


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def safe_generated_destination(image: str, value: str) -> str:
    """Require a generated file destination beneath its selected image."""
    path = pathlib.PurePosixPath(value)
    image_path = pathlib.PurePosixPath(image)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ValueError(f"unsafe generated file destination: {value}")
    try:
        path.relative_to(image_path)
    except ValueError as error:
        raise ValueError(
            f"generated file destination is not image-local for {image}: {value}"
        ) from error
    if path == image_path:
        raise ValueError(f"generated file destination has no filename: {value}")
    return path.as_posix()


def create(
    repo_root: pathlib.Path,
    workspace_root: pathlib.Path,
    selected: list[str],
    planned_sources: list[dict[str, object]],
    projects_by_name: dict[str, dict[str, object]],
    stream: str,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Return deterministic package provenance and generated file records."""
    source_packages: list[dict[str, object]] = []
    generated_files: list[dict[str, object]] = []
    metadata_by_project: dict[str, list[str]] = {}
    for image in selected:
        if image == "base":
            continue
        project_name, _image_name = image.split("/", 1)
        rows: list[tuple[str, str, dict[str, object]]] = []
        paths: dict[str, tuple[str, str]] = {}
        distributions: dict[str, str] = {}
        for source in planned_sources:
            if source["type"] != "repository":
                continue
            relative_path = _relative_source_path(
                image, str(source["destination"])
            )
            if relative_path is None:
                continue
            canonical_name = str(source["canonical_name"])
            source_identity = (canonical_name, str(source["destination"]))
            previous_path = paths.get(relative_path)
            if previous_path and previous_path != source_identity:
                raise ValueError(
                    f"source package path collision for {image}: {relative_path}"
                )
            paths[relative_path] = source_identity
            names = source.get("distributions")
            if names is None:
                names = metadata_by_project.get(canonical_name)
                if names is None:
                    project = projects_by_name[canonical_name]
                    repository = dependencies.prepared_repository(
                        workspace_root, project
                    )
                    names = python_metadata.distribution_names(repository)
                    if (
                        not names
                        and python_metadata.has_project_metadata(repository)
                    ):
                        raise ValueError(
                            "prepared Python source has no static distribution "
                            f"name: {canonical_name}"
                        )
                    metadata_by_project[canonical_name] = names
            if not isinstance(names, list):
                raise ValueError(
                    f"source distributions must be a list: {canonical_name}"
                )
            origin = str(source.get("origin", "manifest"))
            if origin == "transitive" and not names:
                raise ValueError(
                    f"transitive source has no static distribution: {canonical_name}"
                )
            for distribution in names:
                normalized = python_metadata.normalize_name(str(distribution))
                previous_distribution = distributions.get(normalized)
                if (
                    previous_distribution
                    and previous_distribution != relative_path
                ):
                    raise ValueError(
                        f"source distribution collision for {image}: {normalized}"
                    )
                distributions[normalized] = relative_path
                rows.append((relative_path, normalized, source))

        map_content = "".join(
            f"{relative_path} {distribution}\n"
            for relative_path, distribution, _source in rows
        )
        lock_path = (
            repo_root
            / "containers"
            / project_name
            / f"requirements.lock.{stream}"
        )
        if not lock_path.is_file():
            raise ValueError(f"missing service requirements lock: {lock_path}")
        lock_content = lock_path.read_text(encoding="utf-8")
        excluded = set(distributions)
        effective_content = python_metadata.filter_requirements(
            lock_content, excluded, str(lock_path)
        )
        destinations = (
            (
                f"{image}/source-package-map.effective.txt",
                "source-package-map",
                map_content,
            ),
            (
                f"{image}/requirements.lock.effective.{stream}",
                "effective-requirements-lock",
                effective_content,
            ),
        )
        for destination, kind, content in destinations:
            generated_files.append(
                {
                    "image": image,
                    "kind": kind,
                    "destination": safe_generated_destination(
                        image, destination
                    ),
                    "source": (
                        str(lock_path.relative_to(repo_root))
                        if kind == "effective-requirements-lock"
                        else None
                    ),
                    "excluded_distributions": sorted(excluded),
                    "sha256": _digest(content),
                    "content": content,
                }
            )
        for relative_path, distribution, source in rows:
            source_packages.append(
                {
                    "image": image,
                    "canonical_name": source["canonical_name"],
                    "source_path": relative_path,
                    "distribution": distribution,
                    "destination": source["destination"],
                    "inventory_commit": source["inventory_commit"],
                    "authority": source["authority"],
                }
            )
    return source_packages, generated_files
