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

"""Atomic state for the phased local lifecycle."""

from __future__ import annotations

import json
import os
import pathlib
import tempfile


class LocalState:
    """Read and atomically replace one local lifecycle record."""

    def __init__(self, path: pathlib.Path):
        self.path = path

    def read(self) -> dict[str, object] | None:
        if not self.path.exists():
            return None
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("version") != 1:
            raise ValueError(f"invalid local lifecycle state: {self.path}")
        return value

    def write(self, value: dict[str, object]) -> None:
        record = {"version": 1, **value}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent, text=True
        )
        temporary = pathlib.Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(record, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def update(self, **changes: object) -> dict[str, object]:
        value = self.read() or {}
        value.update(changes)
        self.write(value)
        return {"version": 1, **value}
