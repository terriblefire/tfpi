"""Subprocess driver for the power-cut tests:
    python3 -m tfdiag.tests.run_session ENVDIR KILL_AT|record ARGS...
Builds the test Env from ENVDIR/env.json, runs `tfdiag run`, and os._exit()s at the
named checkpoint (KILL_AT = "name#n", the n-th time it is reached)."""

import json
import os
import sys
from pathlib import Path

from tfdiag import hooks
from tfdiag.tests import helpers


def main():
    envdir, kill = Path(sys.argv[1]), sys.argv[2]
    env = helpers.Env.__new__(helpers.Env)
    d = json.loads((envdir / "env.json").read_text())
    env.tmp = envdir
    env.state_path = Path(d["state_path"])
    env.images = Path(d["images"])
    env.statedir = Path(d["statedir"])
    env.cfg = helpers.C.Config(**d["cfg"])
    env.clock = helpers.fakes.FakeClock()
    env.board = helpers.fakes.State(env.state_path)
    seen = {}
    rec = envdir / "checkpoints.txt"

    def hook(name):
        seen[name] = seen.get(name, 0) + 1
        tag = f"{name}#{seen[name]}"
        if kill == "record":
            with open(rec, "a") as fh:
                fh.write(tag + "\n")
        elif tag == kill:
            os._exit(137)

    hooks.HOOK = hook
    from tfdiag import cli
    import io
    rc = cli.main(sys.argv[3:], hw_factory=env.hw_factory(), stream=io.StringIO(), cfg=env.cfg)
    sys.exit(rc)


if __name__ == "__main__":
    main()
