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

"""Parse maintained source declarations into safe placement records."""

from __future__ import annotations

import pathlib

from openstack_image_builder import projects


def manifest_scopes(
    containers_dir: pathlib.Path,
    selected: list[str],
    context: str,
) -> list[tuple[pathlib.Path, str]]:
    """Return global, project, and selected image source scopes."""
    candidates = [
        (containers_dir / "sources.txt", context),
        (containers_dir / context / "sources.txt", context),
    ]
    candidates.extend(
        (containers_dir / image / "sources.txt", image)
        for image in selected
        if image != "base" and image.split("/", 1)[0] == context
    )
    return [(path, scope) for path, scope in candidates if path.is_file()]


def parse_manifest(
    repo_root: pathlib.Path,
    path: pathlib.Path,
    scope: str,
    stream: str,
) -> list[dict[str, object]]:
    """Parse matching records from one five-field ``sources.txt`` file."""
    result: list[dict[str, object]] = []
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) != 5:
            raise ValueError(
                f"{path}:{line_number}: expected five fields, got {len(fields)}"
            )
        entry_stream, name, url, declared_ref, maintained_commit = fields
        if entry_stream != stream:
            continue
        if pathlib.PurePosixPath(name).name != name or not name:
            raise ValueError(f"{path}:{line_number}: unsafe source name")
        canonical_name = projects.canonical_project(url)
        if name == "upper-constraints":
            source_file = "upper-constraints.txt"
            destination = f"{scope}/upper-constraints.txt.{stream}"
            source_type = "constraints"
        else:
            source_file = "."
            destination = f"{scope}/src/{name}"
            source_type = "repository"
        projects.safe_relative_path(destination, "source destination")
        result.append(
            {
                "name": name,
                "canonical_name": canonical_name,
                "url": url,
                "declared_ref": declared_ref,
                "maintained_commit": maintained_commit,
                "manifest": path.relative_to(repo_root).as_posix(),
                "scope": scope,
                "type": source_type,
                "source_file": source_file,
                "destination": destination,
            }
        )
    return result


def placement_records(
    repo_root: pathlib.Path,
    selected: list[str],
    contexts: list[str],
    stream: str,
) -> list[dict[str, object]]:
    """Return deterministic, de-duplicated source placements."""
    containers_dir = repo_root / "containers"
    records: list[dict[str, object]] = []
    identities: set[tuple[str, str]] = set()
    declared: dict[str, tuple[str, str]] = {}
    for context in contexts:
        for manifest, scope in manifest_scopes(
            containers_dir, selected, context
        ):
            for record in parse_manifest(repo_root, manifest, scope, stream):
                canonical_name = record["canonical_name"]
                current = (record["url"], record["declared_ref"])
                previous = declared.get(canonical_name)
                if previous and previous != current:
                    raise ValueError(
                        f"conflicting declarations for {canonical_name}"
                    )
                declared[canonical_name] = current
                identity = (record["canonical_name"], record["destination"])
                if identity in identities:
                    continue
                identities.add(identity)
                records.append(record)
    return records
