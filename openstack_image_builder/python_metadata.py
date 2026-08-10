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

"""Read Python package metadata without importing or executing project code."""

from __future__ import annotations

import ast
import configparser
import pathlib
import re
import tomllib


_NAME_SEPARATORS = re.compile(r"[-_.]+")
_REQUIREMENT = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?:\[[A-Za-z0-9._,-]+\])?\s*(.*)$"
)
_REQUIREMENT_PREFIXES = ("@", "===", "==", "~=", "!=", "<=", ">=", "<", ">", ";")


def normalize_name(value: str) -> str:
    """Return the PEP 503 normalized distribution name."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("distribution name must be a non-empty string")
    normalized = _NAME_SEPARATORS.sub("-", value.strip()).lower()
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", normalized):
        raise ValueError(f"invalid distribution name: {value!r}")
    return normalized


def _setup_py_names(path: pathlib.Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as error:
        raise ValueError(f"cannot parse static package metadata: {path}") from error
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        setup_call = (
            isinstance(function, ast.Name) and function.id == "setup"
        ) or (
            isinstance(function, ast.Attribute) and function.attr == "setup"
        )
        if not setup_call:
            continue
        for keyword in node.keywords:
            if (
                keyword.arg == "name"
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, str)
                and keyword.value.value.strip()
            ):
                names.add(keyword.value.value.strip())
    if len({normalize_name(name) for name in names}) > 1:
        raise ValueError(f"conflicting literal package names in {path}")
    return names


def has_project_metadata(repository: pathlib.Path) -> bool:
    """Return whether a repository declares a recognized Python project file."""
    return any(
        (repository / name).is_file()
        for name in ("pyproject.toml", "setup.cfg", "setup.py")
    )


def distribution_names(repository: pathlib.Path) -> list[str]:
    """Return unambiguous normalized names declared by static project metadata."""
    candidates: list[tuple[str, str]] = []
    pyproject = repository / "pyproject.toml"
    if pyproject.is_file():
        try:
            value = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise ValueError(f"cannot parse static package metadata: {pyproject}") from error
        project = value.get("project")
        if isinstance(project, dict):
            name = project.get("name")
            if isinstance(name, str) and name.strip():
                candidates.append(("pyproject.toml", name.strip()))

    setup_cfg = repository / "setup.cfg"
    if setup_cfg.is_file():
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(setup_cfg, encoding="utf-8")
        except (OSError, configparser.Error) as error:
            raise ValueError(f"cannot parse static package metadata: {setup_cfg}") from error
        if parser.has_option("metadata", "name"):
            name = parser.get("metadata", "name").strip()
            if name:
                candidates.append(("setup.cfg", name))

    setup_py = repository / "setup.py"
    if setup_py.is_file():
        candidates.extend(
            ("setup.py", name) for name in sorted(_setup_py_names(setup_py))
        )

    normalized = {normalize_name(name) for _source, name in candidates}
    if len(normalized) > 1:
        details = ", ".join(f"{source}={name}" for source, name in candidates)
        raise ValueError(f"conflicting package metadata in {repository}: {details}")
    return sorted(normalized)


def _logical_records(content: str) -> list[tuple[list[str], int]]:
    lines = content.splitlines(keepends=True)
    records: list[tuple[list[str], int]] = []
    index = 0
    while index < len(lines):
        start = index + 1
        record = [lines[index]]
        while record[-1].rstrip("\r\n").rstrip().endswith("\\"):
            index += 1
            if index >= len(lines):
                raise ValueError(f"line {start}: unterminated requirement continuation")
            record.append(lines[index])
        records.append((record, start))
        index += 1
    return records


def _record_name(lines: list[str], source: str, line_number: int) -> str | None:
    stripped = lines[0].strip()
    if not stripped or stripped.startswith(("#", "-", "\\")):
        return None
    logical_parts: list[str] = []
    for line in lines:
        part = line.strip()
        if part.endswith("\\"):
            part = part[:-1].rstrip()
        if not part or part.startswith("--hash="):
            continue
        logical_parts.append(part)
    logical = " ".join(logical_parts)
    logical = re.sub(r"\s+#.*$", "", logical).strip()
    match = _REQUIREMENT.fullmatch(logical)
    if not match:
        raise ValueError(f"{source}:{line_number}: unsupported requirement {logical!r}")
    remainder = match.group(2).lstrip()
    if remainder and not remainder.startswith(_REQUIREMENT_PREFIXES):
        raise ValueError(f"{source}:{line_number}: unsupported requirement {logical!r}")
    return normalize_name(match.group(1))


def requirement_names(path: pathlib.Path) -> set[str]:
    """Return normalized active requirement names from one dependency file."""
    content = path.read_text(encoding="utf-8")
    result: set[str] = set()
    for record, line_number in _logical_records(content):
        name = _record_name(record, str(path), line_number)
        if name is not None:
            result.add(name)
    return result


def filter_requirements(
    content: str, excluded: set[str], source: str = "<requirements>"
) -> str:
    """Remove complete excluded requirement records without changing other bytes."""
    normalized = {normalize_name(name) for name in excluded}
    kept: list[str] = []
    for record, line_number in _logical_records(content):
        name = _record_name(record, source, line_number)
        if name not in normalized:
            kept.extend(record)
    return "".join(kept)
