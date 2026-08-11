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

import pathlib
import tempfile
import unittest

from openstack_image_builder import python_metadata


class PythonMetadataTest(unittest.TestCase):
    def test_normalize_name_uses_pep_503_rules(self):
        self.assertEqual(
            "os-resource-classes",
            python_metadata.normalize_name("OS_Resource.Classes"),
        )
        for invalid in ("", "-broken", "contains space"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    python_metadata.normalize_name(invalid)

    def test_distribution_names_support_static_metadata_formats(self):
        fixtures = {
            "pyproject.toml": '[project]\nname = "OS_Resource.Classes"\n',
            "setup.cfg": "[metadata]\nname = os-resource-classes\n",
            "setup.py": "from setuptools import setup\nsetup(name='os_resource_classes')\n",
        }
        for filename, content in fixtures.items():
            with (
                self.subTest(filename=filename),
                tempfile.TemporaryDirectory() as root,
            ):
                repository = pathlib.Path(root)
                (repository / filename).write_text(content, encoding="utf-8")
                self.assertEqual(
                    ["os-resource-classes"],
                    python_metadata.distribution_names(repository),
                )

    def test_distribution_names_accept_agreement_and_reject_conflict(self):
        with tempfile.TemporaryDirectory() as root:
            repository = pathlib.Path(root)
            (repository / "pyproject.toml").write_text(
                '[project]\nname = "oslo.limit"\n', encoding="utf-8"
            )
            (repository / "setup.cfg").write_text(
                "[metadata]\nname = oslo-limit\n", encoding="utf-8"
            )
            self.assertEqual(
                ["oslo-limit"],
                python_metadata.distribution_names(repository),
            )
            (repository / "setup.cfg").write_text(
                "[metadata]\nname = unrelated\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(
                ValueError, "conflicting package metadata"
            ):
                python_metadata.distribution_names(repository)

    def test_dynamic_setup_name_is_not_executed(self):
        with tempfile.TemporaryDirectory() as root:
            repository = pathlib.Path(root)
            marker = repository / "executed"
            (repository / "setup.py").write_text(
                "import pathlib\n"
                f"pathlib.Path({str(marker)!r}).touch()\n"
                "name = 'unsafe'\n"
                "setup(name=name)\n",
                encoding="utf-8",
            )
            self.assertEqual(
                [], python_metadata.distribution_names(repository)
            )
            self.assertFalse(marker.exists())

    def test_requirement_names_parse_supported_forms(self):
        with tempfile.TemporaryDirectory() as root:
            path = pathlib.Path(root) / "requirements.txt"
            path.write_text(
                "# retained comment\n"
                "OS_Resource.Classes[extra]==1.2; python_version >= '3.12'\n"
                "oslo.limit @ https://example.invalid/oslo.limit.whl\n"
                "openstacksdk~=4.0  # inline comment\n"
                "requests==2.0 \\\n"
                "    --hash=sha256:deadbeef\n"
                "--constraint other.txt\n",
                encoding="utf-8",
            )
            self.assertEqual(
                {
                    "openstacksdk",
                    "os-resource-classes",
                    "oslo-limit",
                    "requests",
                },
                python_metadata.requirement_names(path),
            )

    def test_filter_removes_complete_records_and_preserves_other_bytes(self):
        content = (
            "# header\r\n"
            "keep_me==1.0\r\n"
            "OS_Resource.Classes==1.1.0 \\\r\n"
            "    --hash=sha256:first \\\r\n"
            "    --hash=sha256:second\r\n"
            "\r\n"
            "trailing==2.0"
        )
        self.assertEqual(
            "# header\r\nkeep_me==1.0\r\n\r\ntrailing==2.0",
            python_metadata.filter_requirements(
                content, {"os-resource-classes"}, "fixture.lock"
            ),
        )

    def test_malformed_and_unterminated_requirements_fail(self):
        for content in ("not a valid requirement ???\n", "package==1 \\\n"):
            with self.subTest(content=content):
                with self.assertRaises(ValueError):
                    python_metadata.filter_requirements(content, set())


if __name__ == "__main__":
    unittest.main()
