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

import asyncio
import contextlib
import io
import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from openstack_image_builder import build
from openstack_image_builder import build_plan


class BrokenOutput(io.StringIO):
    def write(self, value):
        raise RuntimeError("output sink failed")


class NativeBuildTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repo_root = pathlib.Path(__file__).resolve().parents[1]
        self.temporary = tempfile.TemporaryDirectory(
            dir=self.repo_root / ".tmp", prefix="native-build-test."
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.contexts = self.root / "contexts"
        self.logs = self.root / "logs"
        self.bin = self.root / "bin"
        self.state = self.root / "state.json"
        self.control = self.root / "control.json"
        self.child_pid = self.root / "child.pid"
        self._create_contexts()
        self._create_buildah()
        self.original_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{self.bin}:{self.original_path}"
        os.environ["NATIVE_BUILD_STATE"] = str(self.state)
        os.environ["NATIVE_BUILD_CONTROL"] = str(self.control)
        os.environ["NATIVE_BUILD_CHILD_PID"] = str(self.child_pid)
        self.addCleanup(self._restore_environment)

    def _restore_environment(self):
        os.environ["PATH"] = self.original_path
        for name in (
            "NATIVE_BUILD_STATE",
            "NATIVE_BUILD_CONTROL",
            "NATIVE_BUILD_CHILD_PID",
        ):
            os.environ.pop(name, None)

    def _create_contexts(self):
        base = self.contexts / "base"
        base.mkdir(parents=True)
        (base / "Containerfile").write_text("FROM scratch\n", encoding="utf-8")
        (base / "requirements.lock.master").write_text(
            "base\n", encoding="utf-8"
        )
        for project, images in {
            "watcher": ("watcher-base",),
            "cyborg": ("cyborg", "cyborg-agent"),
        }.items():
            context = self.contexts / project
            (context / "src" / project).mkdir(parents=True)
            for image in images:
                image_dir = context / image
                image_dir.mkdir()
                (image_dir / "Containerfile").write_text(
                    "FROM scratch\n", encoding="utf-8"
                )
                (image_dir / "requirements.lock.effective.master").write_text(
                    "lock\n", encoding="utf-8"
                )
                (image_dir / "source-package-map.effective.txt").write_text(
                    f"{project} {project}\n", encoding="utf-8"
                )

    def _create_buildah(self):
        self.bin.mkdir()
        command = self.bin / "buildah"
        command.write_text(
            "#!/usr/bin/env python3\n"
            "import fcntl\n"
            "import json\n"
            "import os\n"
            "import pathlib\n"
            "import subprocess\n"
            "import sys\n"
            "import time\n"
            "args = sys.argv[1:]\n"
            "containerfile = pathlib.Path(args[args.index('-f') + 1])\n"
            "image = 'base' if containerfile.parent.name == 'base' else containerfile.parent.name\n"
            "control = json.loads(pathlib.Path(os.environ['NATIVE_BUILD_CONTROL']).read_text())\n"
            "state_path = pathlib.Path(os.environ['NATIVE_BUILD_STATE'])\n"
            "state_path.touch(exist_ok=True)\n"
            "with state_path.open('r+', encoding='utf-8') as stream:\n"
            "    fcntl.flock(stream, fcntl.LOCK_EX)\n"
            "    text = stream.read()\n"
            "    state = json.loads(text) if text else {'active': 0, 'maximum': 0}\n"
            "    state['active'] += 1\n"
            "    state['maximum'] = max(state['maximum'], state['active'])\n"
            "    stream.seek(0); stream.truncate(); json.dump(state, stream); stream.flush()\n"
            "    fcntl.flock(stream, fcntl.LOCK_UN)\n"
            "settings = control.get(image, {})\n"
            "if settings.get('child'):\n"
            "    child = subprocess.Popen(['sleep', '30'])\n"
            "    pathlib.Path(os.environ['NATIVE_BUILD_CHILD_PID']).write_text(str(child.pid))\n"
            "print(f'{image} started', flush=True)\n"
            "time.sleep(settings.get('sleep', 0.05))\n"
            "print(f'{image} finished', flush=True)\n"
            "with state_path.open('r+', encoding='utf-8') as stream:\n"
            "    fcntl.flock(stream, fcntl.LOCK_EX)\n"
            "    state = json.load(stream); state['active'] -= 1\n"
            "    stream.seek(0); stream.truncate(); json.dump(state, stream); stream.flush()\n"
            "    fcntl.flock(stream, fcntl.LOCK_UN)\n"
            "raise SystemExit(settings.get('returncode', 0))\n",
            encoding="utf-8",
        )
        command.chmod(0o755)

    def _plan(self, images, parallel=2):
        source_plan = {
            "version": 3,
            "stream": "master",
            "images": images,
            "target_expression": ",".join(images),
        }
        return build_plan.create(
            self.repo_root,
            source_plan,
            contexts_root=self.contexts,
            registry="registry.test:5000",
            namespace="openstack",
            tags="test",
            parallel=parallel,
        )

    def _control(self, value):
        self.control.write_text(json.dumps(value), encoding="utf-8")

    async def test_streams_prefixed_output_retains_logs_and_bounds_parallelism(
        self,
    ):
        self._control(
            {
                "base": {"sleep": 0.01},
                "watcher-base": {"sleep": 0.2},
                "cyborg": {"sleep": 0.2},
                "cyborg-agent": {"sleep": 0.2},
            }
        )
        value = self._plan(
            [
                "base",
                "watcher/watcher-base",
                "cyborg/cyborg",
                "cyborg/cyborg-agent",
            ],
            parallel=2,
        )
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            task = asyncio.create_task(build.execute(value, self.logs))
            await asyncio.sleep(0.1)
            self.assertFalse(task.done())
            self.assertIn("started", output.getvalue())
            await task

        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(2, state["maximum"])
        self.assertEqual(0, state["active"])
        for image in value.images:
            content = (self.logs / image.log_name).read_text(encoding="utf-8")
            self.assertIn(f"[{image.image}]", content)
            self.assertIn("started", content)
            self.assertIn("finished", content)
        self.assertIn(
            "[watcher/watcher-base] watcher-base started", output.getvalue()
        )

    async def test_failure_cancels_process_group_and_propagates_status(self):
        self._control(
            {
                "base": {"sleep": 0.01},
                "watcher-base": {"sleep": 30, "child": True},
                "cyborg": {"sleep": 0.2, "returncode": 23},
            }
        )
        value = self._plan(
            [
                "base",
                "watcher/watcher-base",
                "cyborg/cyborg",
                "cyborg/cyborg-agent",
            ],
            parallel=2,
        )

        with self.assertRaises(build.BuildFailure) as raised:
            await build.execute(value, self.logs)

        self.assertEqual("cyborg/cyborg", raised.exception.image)
        self.assertEqual(23, raised.exception.exit_status)
        child = int(self.child_pid.read_text(encoding="utf-8"))
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)
        self.assertTrue((self.logs / "watcher_watcher-base.log").is_file())
        self.assertFalse((self.logs / "cyborg_cyborg-agent.log").exists())

    async def test_output_failure_terminates_process_group(self):
        self._control({"base": {"sleep": 30, "child": True}})
        value = self._plan(["base"])

        with contextlib.redirect_stdout(BrokenOutput()):
            with self.assertRaisesRegex(RuntimeError, "output sink failed"):
                await build.execute(value, self.logs)

        child = int(self.child_pid.read_text(encoding="utf-8"))
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)

    async def test_task_cancellation_terminates_base_process_group(self):
        self._control({"base": {"sleep": 30, "child": True}})
        value = self._plan(["base"])
        task = asyncio.create_task(build.execute(value, self.logs))
        for _attempt in range(100):
            if self.child_pid.exists():
                break
            await asyncio.sleep(0.01)
        self.assertTrue(self.child_pid.exists())

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        child = int(self.child_pid.read_text(encoding="utf-8"))
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)

    def test_cli_signals_cleanup_process_groups_and_return_status(self):
        value = self._plan(["base"])
        plan_path = self.root / "native-plan.json"
        build_plan.write_atomic(plan_path, value)
        for sent_signal, expected in (
            (signal.SIGINT, 130),
            (signal.SIGTERM, 143),
        ):
            with self.subTest(sent_signal=sent_signal):
                self.child_pid.unlink(missing_ok=True)
                self._control({"base": {"sleep": 30, "child": True}})
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "openstack_image_builder",
                        "build",
                        "--plan",
                        str(plan_path),
                        "--logs-dir",
                        str(self.logs),
                    ],
                    cwd=self.repo_root,
                    env=os.environ.copy(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for _attempt in range(200):
                    if self.child_pid.exists():
                        break
                    time.sleep(0.01)
                self.assertTrue(self.child_pid.exists())
                process.send_signal(sent_signal)
                stdout, stderr = process.communicate(timeout=10)

                self.assertEqual(expected, process.returncode, stdout + stderr)
                self.assertIn(f"status {expected}", stderr)
                child = int(self.child_pid.read_text(encoding="utf-8"))
                with self.assertRaises(ProcessLookupError):
                    os.kill(child, 0)

    async def test_base_failure_prevents_service_start(self):
        self._control({"base": {"returncode": 7}})
        value = self._plan(["base", "watcher/watcher-base"])

        with self.assertRaises(build.BuildFailure) as raised:
            await build.execute(value, self.logs)

        self.assertEqual(7, raised.exception.exit_status)
        self.assertFalse((self.logs / "watcher_watcher-base.log").exists())


if __name__ == "__main__":
    unittest.main()
