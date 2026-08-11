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

import contextlib
import io
import json
import pathlib
import tempfile
import unittest

from openstack_image_builder import build_plan
from openstack_image_builder import cli


class BuildCliTest(unittest.TestCase):
    def setUp(self):
        self.repo_root = pathlib.Path(__file__).resolve().parents[1]
        self.temporary = tempfile.TemporaryDirectory(
            dir=self.repo_root / ".tmp", prefix="native-cli-test."
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.contexts = self.root / "contexts"
        self.source_plan = self.root / "source-plan.json"
        self.native_plan = self.root / "native-plan.json"
        self._create_contexts()
        self.source_plan.write_text(
            json.dumps(
                {
                    "version": 3,
                    "stream": "master",
                    "images": ["base", "watcher/watcher-base"],
                    "target_expression": "watcher/watcher-base",
                }
            ),
            encoding="utf-8",
        )

    def _create_contexts(self):
        base = self.contexts / "base"
        base.mkdir(parents=True)
        (base / "Containerfile").write_text("FROM scratch\n", encoding="utf-8")
        (base / "requirements.lock.master").write_text(
            "base\n", encoding="utf-8"
        )
        watcher = self.contexts / "watcher"
        (watcher / "src/watcher").mkdir(parents=True)
        image = watcher / "watcher-base"
        image.mkdir()
        (image / "Containerfile").write_text(
            "FROM scratch\n", encoding="utf-8"
        )
        (image / "requirements.lock.effective.master").write_text(
            "lock\n", encoding="utf-8"
        )
        (image / "source-package-map.effective.txt").write_text(
            "watcher python-watcher\n", encoding="utf-8"
        )

    def test_build_plan_list_and_refs_round_trip(self):
        result = cli.main(
            [
                "build-plan",
                "--repo-root",
                str(self.repo_root),
                "--source-plan",
                str(self.source_plan),
                "--contexts-root",
                str(self.contexts),
                "--registry",
                "registry.test:5000",
                "--namespace",
                "openstack",
                "--tags",
                "one,two",
                "--parallel",
                "2",
                "--output",
                str(self.native_plan),
            ]
        )
        self.assertEqual(0, result)
        value = build_plan.load(self.native_plan)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = cli.main(["list", "--plan", str(self.native_plan)])
        self.assertEqual(0, result)
        self.assertEqual("base\nwatcher/watcher-base\n", output.getvalue())

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = cli.main(
                [
                    "refs",
                    "--plan",
                    str(self.native_plan),
                    "--format",
                    "json",
                ]
            )
        self.assertEqual(0, result)
        self.assertEqual(list(value.references), json.loads(output.getvalue()))


if __name__ == "__main__":
    unittest.main()
