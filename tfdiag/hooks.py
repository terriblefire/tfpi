"""Named checkpoints between the steps that change the board's state.

They do nothing in production. The power-cut tests set HOOK to a function that
kills the process at a chosen checkpoint (os._exit), then run `recover`.
"""

HOOK = None


def checkpoint(name):
    if HOOK is not None:
        HOOK(name)
