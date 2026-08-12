#!/usr/bin/env python3

import json
import os
import pathlib
import sys


state_path = pathlib.Path(os.environ["S2I_FAKE_STATE"])
client = pathlib.Path(sys.argv[0]).name
arguments = sys.argv[1:]
state = json.loads(state_path.read_text(encoding="utf-8"))
state["commands"].append([client, *arguments])
images = state[client]
return_code = 0

if client == "podman":
    if arguments[:1] == ["pull"]:
        images.append(arguments[-1])
    elif arguments[:1] == ["tag"]:
        images.append(arguments[-1])
    elif arguments[:1] == ["push"]:
        pass
    elif arguments[:1] == ["untag"]:
        if arguments[-1] in images:
            images.remove(arguments[-1])
    elif arguments[:2] in (["image", "inspect"], ["image", "exists"]):
        return_code = 0 if arguments[-1] in images else 1
    elif arguments[:2] == ["image", "rm"]:
        if arguments[-1] in images:
            images.remove(arguments[-1])
        else:
            return_code = 1
    elif arguments[:1] == ["images"]:
        print("\n".join(images))
    else:
        return_code = 2
elif client == "buildah":
    if arguments[:1] == ["inspect"]:
        return_code = 0 if arguments[-1] in images else 125
    elif arguments[:1] == ["rmi"]:
        if arguments[-1] in images:
            images.remove(arguments[-1])
        else:
            return_code = 125
    elif arguments[:1] == ["images"]:
        print("\n".join(images))
    else:
        return_code = 2
else:
    return_code = 2

state[client] = list(dict.fromkeys(images))
state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
sys.exit(return_code)
