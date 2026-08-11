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

"""Image selection and deployment-key metadata parsing."""

from __future__ import annotations

import pathlib

import yaml


def discover(containers_dir: pathlib.Path) -> list[str]:
    """Discover buildable image names in stable filesystem order."""
    result: list[str] = []
    if (containers_dir / "base" / "Containerfile").is_file():
        result.append("base")
    for project_dir in sorted(containers_dir.iterdir()):
        if not project_dir.is_dir() or project_dir.name == "base":
            continue
        for image_dir in sorted(project_dir.iterdir()):
            if image_dir.name in {"common", "src"}:
                continue
            if (image_dir / "Containerfile").is_file():
                result.append(f"{project_dir.name}/{image_dir.name}")
    return result


def ordered_selection(
    containers_dir: pathlib.Path, requested: object
) -> tuple[list[str], str]:
    """Validate C2's explicit image list and prepend the base once."""
    if not isinstance(requested, list) or not requested:
        raise ValueError("images must be a non-empty list")
    if any(not isinstance(image, str) or not image for image in requested):
        raise ValueError("images entries must be non-empty strings")
    if len(set(requested)) != len(requested):
        raise ValueError("images entries must be unique")

    available = set(discover(containers_dir))
    unknown = set(requested) - available
    if unknown:
        raise ValueError("unknown images: " + ", ".join(sorted(unknown)))
    services = [image for image in requested if image != "base"]
    if not services:
        return ["base"], "base"
    selected = ["base", *services]
    return selected, ",".join(selected)


def target_selection(
    containers_dir: pathlib.Path, target: str
) -> tuple[list[str], str]:
    """Normalize shell-compatible all, image, project, or union targets."""
    if not target or any(character.isspace() for character in target):
        raise ValueError("target must be non-empty and contain no whitespace")
    available = discover(containers_dir)
    services = [image for image in available if image != "base"]

    def one(item: str) -> list[str]:
        if item == "all":
            return available
        if item in available:
            return [item]
        project = [
            image
            for image in services
            if image.split("/", 1)[0] == item
        ]
        if project:
            return project
        raise ValueError(f"unknown image or project: {item}")

    result = ["base"]
    seen = {"base"}
    for item in target.split(","):
        if not item:
            raise ValueError("target union contains an empty item")
        for image in one(item):
            if image not in seen:
                result.append(image)
                seen.add(image)
    expression = ",".join(result)
    return result, expression


def context_scopes(selected: list[str]) -> list[str]:
    """Return maintained context names in first-use order."""
    result: list[str] = []
    for image in selected:
        context = "base" if image == "base" else image.split("/", 1)[0]
        if context not in result:
            result.append(context)
    return result


def metadata_path(containers_dir: pathlib.Path, image: str) -> pathlib.Path:
    return containers_dir / image / "image.yaml"


def deployment_keys(path: pathlib.Path) -> list[str]:
    """Load and validate one mandatory image metadata file."""
    if not path.is_file():
        raise ValueError(f"missing image metadata: {path}")
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"image metadata must be a mapping: {path}")
    version = value.get("openstack_version")
    if not isinstance(version, dict):
        raise ValueError(f"image metadata has no openstack_version map: {path}")
    keys = version.get("custom_container_images")
    if not isinstance(keys, list):
        raise ValueError(
            f"image metadata has no custom_container_images list: {path}"
        )
    if any(not isinstance(key, str) or not key for key in keys):
        raise ValueError(f"image deployment keys must be strings: {path}")
    if len(set(keys)) != len(keys):
        raise ValueError(f"image deployment keys must be unique: {path}")
    return keys


def effective_metadata(
    repo_root: pathlib.Path,
    selected: list[str],
    overrides: object,
) -> list[dict[str, object]]:
    """Apply per-image inventory replacement mappings once."""
    if not isinstance(overrides, dict):
        raise ValueError("image_mappings must be an object")
    if any(not isinstance(image, str) for image in overrides):
        raise ValueError("image_mappings keys must be strings")
    unknown = set(overrides) - set(selected)
    if unknown:
        raise ValueError(
            "image mappings name unselected images: "
            + ", ".join(sorted(unknown))
        )

    containers_dir = repo_root / "containers"
    claimed: dict[str, str] = {}
    result: list[dict[str, object]] = []
    for image in selected:
        tracked = deployment_keys(metadata_path(containers_dir, image))
        keys_value = overrides.get(image, tracked)
        if not isinstance(keys_value, list):
            raise ValueError(f"image mapping for {image} must be a list")
        if any(not isinstance(key, str) or not key for key in keys_value):
            raise ValueError(f"image mapping for {image} contains an invalid key")
        if len(set(keys_value)) != len(keys_value):
            raise ValueError(f"image mapping for {image} contains duplicate keys")
        for key in keys_value:
            previous = claimed.get(key)
            if previous:
                raise ValueError(
                    f"deployment key {key!r} belongs to both "
                    f"{previous} and {image}"
                )
            claimed[key] = image
        result.append(
            {
                "image": image,
                "deployment_keys": keys_value,
                "mapping_source": (
                    "inventory" if image in overrides else "tracked"
                ),
            }
        )
    return result
