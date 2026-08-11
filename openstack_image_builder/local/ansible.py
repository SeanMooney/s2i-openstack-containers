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

"""Visible Ansible invocation boundary for OIB-local."""

from __future__ import annotations

import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys


def executable() -> str:
    value = shutil.which("ansible-playbook")
    if not value:
        candidate = pathlib.Path(sys.executable).with_name("ansible-playbook")
        if candidate.is_file():
            value = str(candidate)
    if not value:
        raise ValueError("ansible-playbook is not installed in the OIB environment")
    return value


def run_playbook(
    *,
    playbook: pathlib.Path,
    inventory: pathlib.Path,
    extra_vars: dict[str, object],
    roles_path: pathlib.Path,
    local_root: pathlib.Path,
) -> None:
    """Run one playbook and print the exact non-secret argument vector."""
    command = [
        executable(),
        "--inventory",
        str(inventory),
        "--extra-vars",
        json.dumps(extra_vars, sort_keys=True),
        str(playbook),
    ]
    environment = os.environ.copy()
    environment.update(
        {
            "ANSIBLE_HOME": str(local_root / "ansible/home"),
            "ANSIBLE_COLLECTIONS_PATH": str(local_root / "ansible/collections"),
            "ANSIBLE_LOCAL_TEMP": str(local_root / "ansible/local-tmp"),
            "ANSIBLE_ROLES_PATH": str(roles_path),
        }
    )
    print("+ " + shlex.join(command), file=sys.stderr, flush=True)
    subprocess.run(command, check=True, env=environment)
