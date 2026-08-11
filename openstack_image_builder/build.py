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

"""Execute native prepared-context Buildah plans."""

from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import signal

from openstack_image_builder import build_plan


_TERMINATE_TIMEOUT = 5.0


class BuildInterrupted(Exception):
    """The native build was interrupted after child cleanup."""

    def __init__(self, exit_status: int) -> None:
        self.exit_status = exit_status
        super().__init__(f"native build interrupted with status {exit_status}")


class BuildFailure(Exception):
    """A Buildah child failed with an exact status."""

    def __init__(self, image: str, returncode: int) -> None:
        self.image = image
        self.returncode = returncode
        super().__init__(f"image build failed for {image} with status {returncode}")

    @property
    def exit_status(self) -> int:
        if self.returncode < 0:
            return 128 + abs(self.returncode)
        return self.returncode or 1


async def _terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        await process.wait()
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=_TERMINATE_TIMEOUT)
        return
    except TimeoutError:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await process.wait()


async def _run_image(
    image: build_plan.ImageBuild,
    logs_dir: pathlib.Path,
) -> None:
    log_path = logs_dir / image.log_name
    try:
        log_stream = log_path.open("wb")
    except OSError as error:
        raise BuildFailure(image.image, 127) from error
    with log_stream as log:
        try:
            process = await asyncio.create_subprocess_exec(
                *image.argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
                limit=1024 * 1024,
            )
        except OSError as error:
            raise BuildFailure(image.image, 127) from error
        try:
            if process.stdout is None:
                raise RuntimeError("Buildah output pipe was not created")
            while data := await process.stdout.readline():
                prefix = f"[{image.image}] ".encode()
                output = prefix + data
                log.write(output)
                log.flush()
                print(
                    output.decode("utf-8", errors="replace"),
                    end="",
                    flush=True,
                )
            returncode = await process.wait()
        except BaseException as error:
            await _terminate(process)
            if isinstance(error, OSError):
                raise BuildFailure(image.image, 1) from error
            raise
    if returncode:
        raise BuildFailure(image.image, returncode)


async def _cancel_tasks(tasks: set[asyncio.Task[None]]) -> None:
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _run_services(
    images: tuple[build_plan.ImageBuild, ...],
    logs_dir: pathlib.Path,
    parallel: int,
) -> None:
    image_order = {image.image: index for index, image in enumerate(images)}
    next_image = 0
    active: set[asyncio.Task[None]] = set()
    try:
        while next_image < len(images) or active:
            while next_image < len(images) and len(active) < parallel:
                image = images[next_image]
                active.add(
                    asyncio.create_task(
                        _run_image(image, logs_dir), name=image.image
                    )
                )
                next_image += 1
            done, _pending = await asyncio.wait(
                active, return_when=asyncio.FIRST_COMPLETED
            )
            active.difference_update(done)
            failures: list[BuildFailure] = []
            unexpected: list[BaseException] = []
            for task in done:
                try:
                    task.result()
                except BuildFailure as error:
                    failures.append(error)
                except BaseException as error:
                    unexpected.append(error)
            if failures or unexpected:
                await _cancel_tasks(active)
                active.clear()
                if failures:
                    raise min(
                        failures,
                        key=lambda item: image_order[item.image],
                    )
                raise unexpected[0]
    finally:
        await _cancel_tasks(active)


async def execute(
    value: build_plan.BuildPlan,
    logs_dir: pathlib.Path,
) -> None:
    """Build base serially, then services with bounded concurrency."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    if not logs_dir.is_dir():
        raise ValueError(f"build log path is not a directory: {logs_dir}")
    if not value.images or value.images[0].image != "base":
        raise ValueError("native build plan must begin with base")

    await _run_image(value.images[0], logs_dir)
    await _run_services(value.images[1:], logs_dir, value.parallel)


async def _execute_cli(
    value: build_plan.BuildPlan, logs_dir: pathlib.Path
) -> None:
    current = asyncio.current_task()
    loop = asyncio.get_running_loop()
    installed_term_handler = False
    if current and os.name == "posix":
        try:
            loop.add_signal_handler(signal.SIGTERM, current.cancel)
            installed_term_handler = True
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await execute(value, logs_dir)
    finally:
        if installed_term_handler:
            loop.remove_signal_handler(signal.SIGTERM)


def run(args: argparse.Namespace) -> None:
    value = build_plan.load(pathlib.Path(args.plan))
    try:
        asyncio.run(
            _execute_cli(value, pathlib.Path(args.logs_dir))
        )
    except KeyboardInterrupt as error:
        raise BuildInterrupted(130) from error
    except asyncio.CancelledError as error:
        raise BuildInterrupted(143) from error


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--plan", required=True)
    parser.add_argument("--logs-dir", required=True)
