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

"""Create immutable native image-build plans from prepared contexts."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import pathlib
import re
import tempfile

from openstack_image_builder import images as image_config


BUILD_PLAN_VERSION = 1
_TAG = re.compile(r"^[A-Za-z0-9_.-]+$")
_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")
_PLATFORM = re.compile(
    r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?$"
)


@dataclasses.dataclass(frozen=True)
class ImageBuild:
    """One validated Buildah invocation."""

    image: str
    project: str | None
    name: str
    references: tuple[str, ...]
    context: str
    containerfile: str
    constraints: str
    source_package_map: str | None
    log_name: str
    argv: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class BuildPlan:
    """Complete backend-independent prepared-context build intent."""

    version: int
    repo_root: str
    contexts_root: str
    stream: str
    registry: str
    namespace: str
    image_prefix: str
    tags: tuple[str, ...]
    base_image: str
    base_os_image: str | None
    platform: str | None
    constraints_file: str
    source_policy: str
    pip_no_binary: str | None
    parallel: int
    target_expression: str
    images: tuple[ImageBuild, ...]

    def to_dict(self) -> dict[str, object]:
        value = dataclasses.asdict(self)
        value["images"] = [image.to_dict() for image in self.images]
        return value

    @property
    def references(self) -> tuple[str, ...]:
        return tuple(
            reference
            for image in self.images
            for reference in image.references
        )


def _required_string(value: dict[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ValueError(f"build plan has no {key}")
    return item


def _optional_string(value: dict[str, object], key: str) -> str | None:
    item = value.get(key)
    if item is None:
        return None
    if not isinstance(item, str) or not item:
        raise ValueError(f"build plan has invalid {key}")
    return item


def _safe_text(value: str, description: str) -> str:
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError(f"{description} contains an unsafe character")
    return value


def _image_reference(value: str, description: str) -> str:
    _safe_text(value, description)
    if any(character.isspace() for character in value):
        raise ValueError(f"{description} contains whitespace")
    return value


def _canonical_directory(path: pathlib.Path, description: str) -> pathlib.Path:
    try:
        result = path.resolve(strict=True)
    except OSError as error:
        raise ValueError(f"{description} does not exist: {path}") from error
    if not result.is_dir():
        raise ValueError(f"{description} is not a directory: {result}")
    return result


def _canonical_file(path: pathlib.Path, description: str) -> pathlib.Path:
    try:
        result = path.resolve(strict=True)
    except OSError as error:
        raise ValueError(f"{description} does not exist: {path}") from error
    if not result.is_file():
        raise ValueError(f"{description} is not a regular file: {result}")
    return result


def _beneath(
    path: pathlib.Path, root: pathlib.Path, description: str
) -> pathlib.Path:
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError(f"{description} escaped {root}: {path}") from error
    return path


def _tags(value: str) -> tuple[str, ...]:
    result = tuple(value.split(","))
    if not result or any(not _TAG.fullmatch(tag) for tag in result):
        raise ValueError("tags must be comma-separated safe non-empty names")
    if len(set(result)) != len(result):
        raise ValueError("tags must be unique")
    return result


def _image_name(image: str, prefix: str) -> str:
    leaf = image.split("/", 1)[-1]
    return f"{prefix}-{leaf}" if prefix else leaf


def _references(
    registry: str,
    namespace: str,
    name: str,
    tags: tuple[str, ...],
) -> tuple[str, ...]:
    return tuple(f"{registry}/{namespace}/{name}:{tag}" for tag in tags)


def _argv(
    image: str,
    references: tuple[str, ...],
    context: pathlib.Path,
    containerfile: pathlib.Path,
    constraints: str,
    source_package_map: str | None,
    base_image: str,
    pull_local_base: bool,
    base_os_image: str | None,
    platform: str | None,
    pip_no_binary: str | None,
) -> tuple[str, ...]:
    result = ["buildah", "bud"]
    if platform:
        result.extend(("--platform", platform))
    if image != "base" and pull_local_base:
        result.append("--pull-never")
    for reference in references:
        result.extend(("--tag", reference))
    if image == "base" and base_os_image:
        result.extend(("--build-arg", f"BASE_IMAGE={base_os_image}"))
    result.extend(("--build-arg", f"CONSTRAINTS_FILE={constraints}"))
    if source_package_map:
        result.extend(
            ("--build-arg", f"SOURCE_PACKAGE_MAP={source_package_map}")
        )
    if image != "base":
        result.extend(("--build-arg", f"BASE_IMAGE={base_image}"))
    if pip_no_binary:
        result.extend(("--build-arg", f"PIP_NO_BINARY={pip_no_binary}"))
    result.extend(("-f", str(containerfile), f"{context}/"))
    return tuple(result)


def create(
    repo_root: pathlib.Path,
    source_plan: dict[str, object],
    *,
    contexts_root: pathlib.Path,
    registry: str,
    namespace: str,
    tags: str,
    image_prefix: str = "openstack",
    base_image: str | None = None,
    base_os_image: str | None = None,
    platform: str | None = None,
    constraints_file: str = "requirements.lock",
    pip_no_binary: str | None = None,
    parallel: int = 1,
) -> BuildPlan:
    """Create one native plan from the immutable source/staging plan."""
    repo_root = _canonical_directory(repo_root, "repository root")
    contexts_root = _canonical_directory(contexts_root, "prepared context root")
    isolated_root = _canonical_directory(repo_root / ".tmp", "repository temporary root")
    _beneath(contexts_root, isolated_root, "prepared context root")

    if source_plan.get("version") != 3:
        raise ValueError("native build requires a source plan at version 3")
    source_images = source_plan.get("images")
    if not isinstance(source_images, list):
        raise ValueError("source plan images must be a list")
    selected, _ = image_config.ordered_selection(
        repo_root / "containers", source_images
    )
    if selected != source_images:
        raise ValueError("source plan images are not a normalized selection")
    target_expression = _required_string(source_plan, "target_expression")
    target_images, normalized_target = image_config.target_selection(
        repo_root / "containers", target_expression
    )
    if target_images != selected or normalized_target != target_expression:
        raise ValueError("source plan target does not match its images")
    stream = _safe_text(_required_string(source_plan, "stream"), "stream")

    base_image = base_image or None
    base_os_image = base_os_image or None
    platform = platform or None
    pip_no_binary = pip_no_binary or None
    registry = _safe_text(registry, "registry")
    namespace = _safe_text(namespace, "namespace")
    if (
        not registry
        or "/" in registry
        or any(character.isspace() for character in registry)
    ):
        raise ValueError("registry must be a safe host or host:port value")
    if not _COMPONENT.fullmatch(namespace):
        raise ValueError("namespace must be a safe image-name component")
    if image_prefix and not _COMPONENT.fullmatch(image_prefix):
        raise ValueError("image prefix must be empty or a safe component")
    if not _COMPONENT.fullmatch(constraints_file):
        raise ValueError("constraints file must be a safe filename component")
    planned_tags = _tags(tags)
    if not isinstance(parallel, int) or isinstance(parallel, bool) or parallel < 1:
        raise ValueError("parallel must be a positive integer")
    if platform and not _PLATFORM.fullmatch(platform):
        raise ValueError("platform must be OS/ARCH or OS/ARCH/VARIANT")
    if base_os_image:
        _image_reference(base_os_image, "base OS image")
    if pip_no_binary:
        _safe_text(pip_no_binary, "PIP_NO_BINARY")

    base_name = _image_name("base", image_prefix)
    base_references = _references(
        registry, namespace, base_name, planned_tags
    )
    service_base = _image_reference(
        base_image or base_references[0], "base image"
    )

    planned_images: list[ImageBuild] = []
    for image in selected:
        project = None if image == "base" else image.split("/", 1)[0]
        image_dir = image.split("/", 1)[-1]
        context = contexts_root / ("base" if image == "base" else project)
        context = _canonical_directory(context, f"{image} context")
        _beneath(context, contexts_root, f"{image} context")
        containerfile = (
            context / "Containerfile"
            if image == "base"
            else context / image_dir / "Containerfile"
        )
        containerfile = _canonical_file(
            containerfile, f"{image} Containerfile"
        )
        _beneath(containerfile, context, f"{image} Containerfile")

        if image == "base":
            constraints = f"{constraints_file}.{stream}"
            source_package_map = None
        else:
            main_source = _canonical_directory(
                context / "src" / str(project),
                f"{image} main source",
            )
            _beneath(main_source, context, f"{image} main source")
            constraints = (
                f"{image_dir}/requirements.lock.effective.{stream}"
            )
            source_package_map = (
                f"{image_dir}/source-package-map.effective.txt"
            )

        for relative in (constraints, source_package_map):
            if relative is None:
                continue
            path = _canonical_file(
                context / relative, f"{image} package input"
            )
            _beneath(path, context, f"{image} package input")

        name = _image_name(image, image_prefix)
        references = _references(
            registry, namespace, name, planned_tags
        )
        planned_images.append(
            ImageBuild(
                image=image,
                project=project,
                name=name,
                references=references,
                context=str(context),
                containerfile=str(containerfile),
                constraints=constraints,
                source_package_map=source_package_map,
                log_name=image.replace("/", "_") + ".log",
                argv=_argv(
                    image,
                    references,
                    context,
                    containerfile,
                    constraints,
                    source_package_map,
                    service_base,
                    service_base == base_references[0],
                    base_os_image,
                    platform,
                    pip_no_binary,
                ),
            )
        )

    return BuildPlan(
        version=BUILD_PLAN_VERSION,
        repo_root=str(repo_root),
        contexts_root=str(contexts_root),
        stream=stream,
        registry=registry,
        namespace=namespace,
        image_prefix=image_prefix,
        tags=planned_tags,
        base_image=service_base,
        base_os_image=base_os_image,
        platform=platform,
        constraints_file=constraints_file,
        source_policy="prepared-only",
        pip_no_binary=pip_no_binary,
        parallel=parallel,
        target_expression=target_expression,
        images=tuple(planned_images),
    )


def _image_from_dict(value: object) -> ImageBuild:
    if not isinstance(value, dict):
        raise ValueError("native build image record must be an object")
    project = value.get("project")
    if project is not None and not isinstance(project, str):
        raise ValueError("native build image project must be a string or null")
    references = value.get("references")
    argv = value.get("argv")
    if not isinstance(references, (list, tuple)) or any(
        not isinstance(item, str) for item in references
    ):
        raise ValueError("native build references must be strings")
    if not isinstance(argv, (list, tuple)) or any(
        not isinstance(item, str) for item in argv
    ):
        raise ValueError("native build argv must be strings")
    return ImageBuild(
        image=_required_string(value, "image"),
        project=project,
        name=_required_string(value, "name"),
        references=tuple(references),
        context=_required_string(value, "context"),
        containerfile=_required_string(value, "containerfile"),
        constraints=_required_string(value, "constraints"),
        source_package_map=_optional_string(value, "source_package_map"),
        log_name=_required_string(value, "log_name"),
        argv=tuple(argv),
    )


def from_dict(value: object) -> BuildPlan:
    """Load and validate a serialized native BuildPlan."""
    if not isinstance(value, dict):
        raise ValueError("native build plan must be an object")
    version = value.get("version")
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version != BUILD_PLAN_VERSION
    ):
        raise ValueError("unsupported native build plan version")
    tags = value.get("tags")
    images = value.get("images")
    parallel = value.get("parallel")
    if not isinstance(tags, (list, tuple)) or any(
        not isinstance(tag, str) for tag in tags
    ):
        raise ValueError("native build plan tags must be strings")
    if not isinstance(images, list):
        raise ValueError("native build plan images must be a list")
    if not isinstance(parallel, int) or isinstance(parallel, bool) or parallel < 1:
        raise ValueError("native build plan parallel must be positive")
    image_prefix = value.get("image_prefix", "")
    if not isinstance(image_prefix, str):
        raise ValueError("native build image prefix must be a string")
    plan = BuildPlan(
        version=BUILD_PLAN_VERSION,
        repo_root=_required_string(value, "repo_root"),
        contexts_root=_required_string(value, "contexts_root"),
        stream=_required_string(value, "stream"),
        registry=_required_string(value, "registry"),
        namespace=_required_string(value, "namespace"),
        image_prefix=image_prefix,
        tags=tuple(tags),
        base_image=_required_string(value, "base_image"),
        base_os_image=_optional_string(value, "base_os_image"),
        platform=_optional_string(value, "platform"),
        constraints_file=_required_string(value, "constraints_file"),
        source_policy=_required_string(value, "source_policy"),
        pip_no_binary=_optional_string(value, "pip_no_binary"),
        parallel=parallel,
        target_expression=_required_string(value, "target_expression"),
        images=tuple(_image_from_dict(item) for item in images),
    )
    _validate_loaded(plan)
    return plan


def _validate_loaded(plan: BuildPlan) -> None:
    source_plan = {
        "version": 3,
        "stream": plan.stream,
        "images": [image.image for image in plan.images],
        "target_expression": plan.target_expression,
    }
    expected = create(
        pathlib.Path(plan.repo_root),
        source_plan,
        contexts_root=pathlib.Path(plan.contexts_root),
        registry=plan.registry,
        namespace=plan.namespace,
        tags=",".join(plan.tags),
        image_prefix=plan.image_prefix,
        base_image=plan.base_image,
        base_os_image=plan.base_os_image,
        platform=plan.platform,
        constraints_file=plan.constraints_file,
        pip_no_binary=plan.pip_no_binary,
        parallel=plan.parallel,
    )
    if plan.source_policy != expected.source_policy:
        raise ValueError("native build source policy must be prepared-only")
    for actual_image, expected_image in zip(
        plan.images, expected.images, strict=True
    ):
        if actual_image.context != expected_image.context:
            raise ValueError(
                f"native build context changed for {actual_image.image}"
            )
        if actual_image.containerfile != expected_image.containerfile:
            raise ValueError(
                "native Buildah Containerfile changed for "
                f"{actual_image.image}"
            )
        if actual_image.argv != expected_image.argv:
            raise ValueError(
                f"native Buildah argv changed for {actual_image.image}"
            )
    if plan != expected:
        raise ValueError("native build plan changed from derived intent")


def load(path: pathlib.Path) -> BuildPlan:
    return from_dict(json.loads(path.read_text(encoding="utf-8")))


def write_atomic(path: pathlib.Path, value: BuildPlan) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(value.to_dict(), indent=2, sort_keys=True) + "\n"
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


def run_create(args: argparse.Namespace) -> None:
    source_plan = json.loads(pathlib.Path(args.source_plan).read_text(encoding="utf-8"))
    if not isinstance(source_plan, dict):
        raise ValueError("source plan must be an object")
    value = create(
        pathlib.Path(args.repo_root),
        source_plan,
        contexts_root=pathlib.Path(args.contexts_root),
        registry=args.registry,
        namespace=args.namespace,
        tags=args.tags,
        image_prefix=args.image_prefix,
        base_image=args.base_image,
        base_os_image=args.base_os_image,
        platform=args.platform,
        constraints_file=args.constraints_file,
        pip_no_binary=args.pip_no_binary,
        parallel=args.parallel,
    )
    write_atomic(pathlib.Path(args.output), value)


def run_list(args: argparse.Namespace) -> None:
    value = load(pathlib.Path(args.plan))
    if args.format == "json":
        print(json.dumps([image.image for image in value.images]))
        return
    for image in value.images:
        print(image.image)


def run_refs(args: argparse.Namespace) -> None:
    value = load(pathlib.Path(args.plan))
    if args.format == "json":
        print(json.dumps(list(value.references)))
        return
    for reference in value.references:
        print(reference)


def add_create_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--source-plan", required=True)
    parser.add_argument("--contexts-root", required=True)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--tags", required=True)
    parser.add_argument("--image-prefix", default="openstack")
    parser.add_argument("--base-image")
    parser.add_argument("--base-os-image")
    parser.add_argument("--platform")
    parser.add_argument("--constraints-file", default="requirements.lock")
    parser.add_argument("--pip-no-binary")
    parser.add_argument("--parallel", type=int, default=1)
    parser.add_argument("--output", required=True)


def add_output_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--plan", required=True)
    parser.add_argument("--format", choices=("plain", "json"), default="plain")
