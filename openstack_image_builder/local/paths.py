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

"""Filesystem boundaries for locally owned state."""

from __future__ import annotations

import pathlib
import shutil


def require_beneath(
    path: pathlib.Path, parent: pathlib.Path, description: str
) -> pathlib.Path:
    """Return a resolved path only when it is strictly beneath *parent*."""
    resolved_parent = parent.resolve()
    resolved = path.resolve()
    if resolved == resolved_parent or resolved_parent not in resolved.parents:
        raise ValueError(
            f"{description} must remain beneath {resolved_parent}: {resolved}"
        )
    return resolved


def remove(path: pathlib.Path) -> None:
    """Remove one explicitly owned path without following a directory link."""
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)
