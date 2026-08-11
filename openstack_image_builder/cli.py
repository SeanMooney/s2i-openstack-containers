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

"""Side-effect-free planning for OpenStack image build inputs."""

import argparse
import json
import subprocess
import sys

from openstack_image_builder import plan
from openstack_image_builder.local import lifecycle


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oib", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser(
        "plan", help="write a validated source and image metadata plan"
    )
    plan.add_arguments(plan_parser)
    plan_parser.set_defaults(handler=plan.run)

    local_parser = subparsers.add_parser(
        "local", help="prepare and run a local Zuul-compatible lifecycle"
    )
    lifecycle.add_subcommands(local_parser)
    return parser


def invoke(args: argparse.Namespace) -> int:
    try:
        args.handler(args)
    except (
        json.JSONDecodeError,
        OSError,
        subprocess.CalledProcessError,
        ValueError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    return invoke(create_parser().parse_args(argv))
