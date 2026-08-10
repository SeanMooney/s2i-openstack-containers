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

"""Small subprocess boundary for local Git operations."""

from __future__ import annotations

import pathlib
import subprocess


def run(
    *arguments: str,
    cwd: pathlib.Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run Git without a shell and capture its textual output."""
    return subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def output(*arguments: str, cwd: pathlib.Path | None = None) -> str:
    """Return stripped stdout from a successful Git command."""
    return run(*arguments, cwd=cwd).stdout.strip()
