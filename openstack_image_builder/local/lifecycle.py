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

"""Failure-safe prepare, run, cleanup, and composed local CI phases."""

from __future__ import annotations

import argparse
import dataclasses
import json
import pathlib
import shutil
import sys

from openstack_image_builder import images as image_config
from openstack_image_builder import projects
from openstack_image_builder import sources
from openstack_image_builder.local import ansible
from openstack_image_builder.local import cache
from openstack_image_builder.local import git
from openstack_image_builder.local import inventory
from openstack_image_builder.local import paths
from openstack_image_builder.local import state
from openstack_image_builder.local import workspace


@dataclasses.dataclass(frozen=True)
class Layout:
    repo_root: pathlib.Path
    root: pathlib.Path
    cache_root: pathlib.Path
    workspace_root: pathlib.Path
    output_dir: pathlib.Path
    inventory_path: pathlib.Path
    source_manifest_path: pathlib.Path
    state_path: pathlib.Path
    registry_root: pathlib.Path
    zuul_jobs_dir: pathlib.Path


def layout(repo_root: pathlib.Path) -> Layout:
    repo_root = repo_root.resolve()
    root = repo_root / ".tmp/local"
    return Layout(
        repo_root=repo_root,
        root=root,
        cache_root=root / "git-cache",
        workspace_root=root / "workspace",
        output_dir=root / "zuul-output",
        inventory_path=root / "inventory.yaml",
        source_manifest_path=root / "source-manifest.json",
        state_path=root / "state.json",
        registry_root=root / "registry",
        zuul_jobs_dir=root / "zuul-jobs",
    )


def selected_images(repo_root: pathlib.Path, target: str) -> list[str]:
    """Resolve an explicit local target without dependency inference."""
    available = image_config.discover(repo_root / "containers")
    services = [image for image in available if image != "base"]
    if target == "all":
        return services
    if target in services:
        return [target]
    project_images = [
        image for image in services if image.split("/", 1)[0] == target
    ]
    if project_images:
        return project_images
    raise ValueError(f"unknown local image target: {target}")


def source_specs(
    repo_root: pathlib.Path, requested: list[str], stream: str
) -> list[cache.ProjectSpec]:
    selected, _target_expression = image_config.ordered_selection(
        repo_root / "containers", requested
    )
    contexts = image_config.context_scopes(selected)
    records = sources.placement_records(repo_root, selected, contexts, stream)
    values: dict[str, cache.ProjectSpec] = {}
    for record in records:
        spec = cache.ProjectSpec(
            canonical_name=str(record["canonical_name"]),
            url=str(record["url"]),
            declared_ref=str(record["declared_ref"]),
            commit=str(record["maintained_commit"]),
        )
        previous = values.get(spec.canonical_name)
        if previous and previous != spec:
            raise ValueError(
                f"conflicting local source declarations for {spec.canonical_name}"
            )
        values[spec.canonical_name] = spec
    return [values[name] for name in sorted(values)]


def zuul_jobs_spec(repo_root: pathlib.Path) -> cache.ProjectSpec:
    pin_path = repo_root / ".zuul-jobs-ref"
    pin = pin_path.read_text(encoding="utf-8").strip()
    return cache.ProjectSpec(
        canonical_name="opendev.org/zuul/zuul-jobs",
        url="https://opendev.org/zuul/zuul-jobs",
        declared_ref="master",
        commit=pin,
    )


def _tox_executable() -> str:
    value = shutil.which("tox")
    if not value:
        raise ValueError("tox is not installed in the OIB-local environment")
    return value


def _base_extra(args: argparse.Namespace, current: Layout) -> dict[str, object]:
    return {
        "s2i_ci_cleanup_images": True,
        "s2i_ci_namespace": args.namespace,
        "s2i_ci_output_dir": str(current.output_dir),
        "s2i_ci_parallel": args.parallel,
        "s2i_ci_registry_port": args.registry_port,
        "s2i_ci_registry_root": str(current.registry_root),
        "s2i_ci_repo_root": str(current.repo_root),
        "s2i_ci_stream": args.stream,
        "s2i_ci_tag": args.tag or f"{args.stream}-latest",
        "s2i_ci_workspace_root": str(current.workspace_root),
        "s2i_ci_zuul_jobs_dir": str(current.zuul_jobs_dir),
    }


def _run_playbook(
    args: argparse.Namespace,
    current: Layout,
    name: str,
    extra_vars: dict[str, object] | None = None,
) -> None:
    values = _base_extra(args, current)
    values.update(extra_vars or {})
    ansible.run_playbook(
        playbook=current.repo_root / f"playbooks/container-ci/local/{name}.yaml",
        inventory=current.inventory_path,
        extra_vars=values,
        roles_path=current.zuul_jobs_dir / "roles",
        local_root=current.root,
    )


def _active(record: dict[str, object] | None) -> bool:
    return bool(record and record.get("phase") not in {"cleaned"})


def _recorded_args(
    args: argparse.Namespace, record: dict[str, object]
) -> argparse.Namespace:
    """Use prepare-phase options for later run and cleanup phases."""
    values = vars(args).copy()
    options = record.get("options", {})
    if isinstance(options, dict):
        values.update(options)
    return argparse.Namespace(**values)


def prepare(args: argparse.Namespace) -> None:
    """Prepare exact pinned sources, inventory, roles, and local registry."""
    current = layout(pathlib.Path(args.repo_root))
    local_state = state.LocalState(current.state_path)
    existing = local_state.read()
    if _active(existing):
        raise ValueError("local lifecycle is active; run 'oib local cleanup' first")
    paths.require_beneath(current.root, current.repo_root / ".tmp", "local root")
    requested = selected_images(current.repo_root, args.target)
    specs = source_specs(current.repo_root, requested, args.stream)
    role_spec = zuul_jobs_spec(current.repo_root)
    origin = git.output("remote", "get-url", "origin", cwd=current.repo_root)
    container_project = projects.canonical_project(origin)
    record: dict[str, object] = {
        "phase": "preparing",
        "images": requested,
        "stream": args.stream,
        "target": args.target,
        "options": {
            "namespace": args.namespace,
            "parallel": args.parallel,
            "registry_port": args.registry_port,
            "stream": args.stream,
            "tag": args.tag or f"{args.stream}-latest",
            "target": args.target,
        },
        "workspace_root": str(current.workspace_root),
        "output_dir": str(current.output_dir),
        "inventory_path": str(current.inventory_path),
        "source_manifest": str(current.source_manifest_path),
        "cache_root": str(current.cache_root),
        "zuul_jobs_dir": str(current.zuul_jobs_dir),
        "registry": {
            "container": "s2i_ci_registry",
            "port": args.registry_port,
            "root": str(current.registry_root),
            "owned": False,
        },
        "ansible_started": False,
        "cache_entries": [],
    }
    local_state.write(record)
    try:
        paths.remove(current.workspace_root)
        paths.remove(current.zuul_jobs_dir)
        current.output_dir.mkdir(parents=True, exist_ok=True)
        prepared: dict[str, dict[str, object]] = {}
        current_destination = (
            current.workspace_root / "src" / container_project
        )
        current_data = workspace.prepare_current(
            current.repo_root,
            current_destination,
            strict=args.strict_worktree,
        )
        prepared[container_project] = {
            "source": str(current.repo_root),
            **current_data,
        }

        manager = cache.GitCache(current.repo_root, current.cache_root)
        cache_entries: list[dict[str, object]] = []
        for spec in specs:
            destination = current.workspace_root / "src" / spec.canonical_name
            paths.require_beneath(
                destination, current.workspace_root, "local source checkout"
            )
            cache_entry = manager.materialize(spec, destination)
            cache_entries.append(cache_entry)
            prepared[spec.canonical_name] = {
                "source": spec.url,
                "declared_ref": spec.declared_ref,
                "commit": git.output("rev-parse", "HEAD", cwd=destination),
                "maintained_commit": spec.commit,
                "authority": "maintained-pin",
                "dirty": False,
                "cache": cache_entry,
            }

        role_cache_entry = manager.materialize(
            role_spec, current.zuul_jobs_dir
        )
        cache_entries.append(role_cache_entry)
        role_head = git.output("rev-parse", "HEAD", cwd=current.zuul_jobs_dir)
        if role_head != role_spec.commit:
            raise ValueError("prepared zuul-jobs checkout does not match its pin")

        inventory.write(
            repo_root=current.repo_root,
            workspace_root=current.workspace_root,
            output_dir=current.output_dir,
            inventory_path=current.inventory_path,
            source_manifest_path=current.source_manifest_path,
            container_project=container_project,
            prepared_projects=prepared,
            images=requested,
            stream=args.stream,
            tox_executable=_tox_executable(),
        )
        record.update(
            {
                "container_project": container_project,
                "prepared_projects": prepared,
                "cache_entries": cache_entries,
                "zuul_jobs_commit": role_head,
                "ansible_started": True,
            }
        )
        local_state.write(record)
        _run_playbook(args, current, "base")
        _run_playbook(args, current, "registry")
        registry = dict(record["registry"])
        registry["owned"] = True
        record["registry"] = registry
        local_state.write(record)
        _run_playbook(args, current, "registry-ready")
        _run_playbook(args, current, "pre")
        record["phase"] = "prepared"
        local_state.write(record)
        print(f"Local inventory: {current.inventory_path}")
        print(f"Local source manifest: {current.source_manifest_path}")
        print(f"Local output: {current.output_dir}")
    except Exception:
        record["phase"] = "failed"
        local_state.write(record)
        raise


def run(args: argparse.Namespace) -> None:
    """Run shared context assembly and shell-backed publication."""
    current = layout(pathlib.Path(args.repo_root))
    local_state = state.LocalState(current.state_path)
    record = local_state.read()
    if not record or record.get("phase") not in {"prepared", "ran"}:
        raise ValueError("local run requires a completed prepare phase")
    effective_args = _recorded_args(args, record)
    record["phase"] = "running"
    local_state.write(record)
    try:
        _run_playbook(effective_args, current, "run")
    except Exception:
        record["phase"] = "failed"
        local_state.write(record)
        raise
    record["phase"] = "ran"
    local_state.write(record)
    print(f"Published image manifest: {current.output_dir}/logs/container-build/published-images.json")


def cleanup(args: argparse.Namespace) -> None:
    """Remove owned runtime state while preserving caches and output logs."""
    current = layout(pathlib.Path(args.repo_root))
    local_state = state.LocalState(current.state_path)
    record = local_state.read()
    if not record:
        local_state.write(
            {
                "phase": "cleaned",
                "cache_root": str(current.cache_root),
                "output_dir": str(current.output_dir),
            }
        )
        return
    effective_args = _recorded_args(args, record)
    cleanup_errors: list[Exception] = []
    registry = dict(record.get("registry", {}))
    if record.get("ansible_started"):
        if not current.inventory_path.is_file():
            cleanup_errors.append(
                ValueError("local cleanup requires the prepared inventory")
            )
        else:
            cleanup_vars = {
                "s2i_ci_local_registry_owned": bool(registry.get("owned"))
            }
            for playbook in ("post-logs", "post-images"):
                try:
                    _run_playbook(
                        effective_args, current, playbook, cleanup_vars
                    )
                except Exception as error:
                    cleanup_errors.append(error)
            try:
                _run_playbook(
                    effective_args, current, "post", cleanup_vars
                )
            except Exception as error:
                cleanup_errors.append(error)
            else:
                registry["owned"] = False
                record["registry"] = registry
                local_state.write(record)

    if cleanup_errors:
        record["phase"] = "cleanup-failed"
    else:
        for owned in (
            current.workspace_root,
            current.zuul_jobs_dir,
            current.registry_root,
            current.inventory_path,
            current.output_dir / ".s2i-ci-registry-connection.json",
        ):
            paths.remove(owned)
        registry["owned"] = False
        record["registry"] = registry
        record["phase"] = "cleaned"
    local_state.write(record)
    print(f"Persistent Git cache: {current.cache_root}")
    print(f"Retained local output: {current.output_dir}")
    if cleanup_errors:
        raise cleanup_errors[0]


def ci(args: argparse.Namespace) -> None:
    """Compose all local phases with failure-safe cleanup."""
    try:
        prepare(args)
        run(args)
    finally:
        if not args.keep:
            cleanup(args)


def cache_action(args: argparse.Namespace) -> None:
    """Inspect or explicitly maintain known pinned caches."""
    current = layout(pathlib.Path(args.repo_root))
    requested = selected_images(current.repo_root, args.target)
    specs = source_specs(current.repo_root, requested, args.stream)
    specs.append(zuul_jobs_spec(current.repo_root))
    if args.project:
        specs = [spec for spec in specs if spec.canonical_name == args.project]
        if not specs:
            raise ValueError(f"unknown cached project: {args.project}")
    manager = cache.GitCache(current.repo_root, current.cache_root)
    results: list[dict[str, object]] = []
    for spec in specs:
        if args.cache_command == "inspect":
            results.append(manager.inspect(spec))
        elif args.cache_command == "refresh":
            results.append(manager.refresh(spec))
        elif args.cache_command == "prune":
            manager.prune(spec)
            results.append({"canonical_name": spec.canonical_name, "pruned": True})
        elif args.cache_command == "clear":
            manager.clear(spec)
            results.append({"canonical_name": spec.canonical_name, "cleared": True})
        else:
            raise ValueError(f"unsupported cache action: {args.cache_command}")
    json.dump(results, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--target", default="all")
    parser.add_argument("--stream", default="master")
    parser.add_argument("--namespace", default="s2i-ci")
    parser.add_argument("--tag")
    parser.add_argument("--parallel", type=int, default=1)
    parser.add_argument("--registry-port", type=int, default=15000)
    parser.add_argument("--strict-worktree", action="store_true")


def add_subcommands(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(dest="local_command", required=True)
    for name, handler in (
        ("prepare", prepare),
        ("run", run),
        ("cleanup", cleanup),
        ("ci", ci),
    ):
        command = subparsers.add_parser(name)
        add_arguments(command)
        if name == "ci":
            command.add_argument("--keep", action="store_true")
        else:
            command.set_defaults(keep=False)
        command.set_defaults(handler=handler)

    cache_parser = subparsers.add_parser("cache")
    add_arguments(cache_parser)
    cache_parser.add_argument("--project")
    cache_parser.set_defaults(keep=False)
    cache_subparsers = cache_parser.add_subparsers(
        dest="cache_command", required=True
    )
    for action in ("inspect", "refresh", "prune", "clear"):
        cache_subparsers.add_parser(action).set_defaults(handler=cache_action)
