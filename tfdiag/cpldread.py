"""Read the BUS CPLD's fuse map back, to see which design is really programmed (the
usercode is the same for every build).

This is the only place tfdiag runs xc3sprog, and it only ever runs its READ action.
xc3sprog's default action is 'w' (erase the whole device and program it), so every
call is built and checked here: exactly `-c CABLE -p 0 NEWFILE:r`, where NEWFILE does
not exist yet ('r' = read from the device into a new file, never overwrite).
Nothing else is accepted: no -e (erase), no -l (lock bits), no other filespec action.

Reading puts the XC9500XL in ISP mode for the read, which floats its outputs, so it is
only done while the Amiga is held in reset (the diag FPGA image holds RESET low).
Reading changes no fuses; a power cut during it is harmless.
"""

import os
import re
import time
from pathlib import Path

from . import tools

CABLE_RE = re.compile(r"^[A-Za-z0-9_]+$")


class CpldReadError(RuntimeError):
    pass


def check_args(args, cable):
    args = [str(a) for a in args]
    if not CABLE_RE.match(cable or ""):
        raise CpldReadError(f"refusing xc3sprog cable name {cable!r}")
    if len(args) != 5 or args[:4] != ["-c", cable, "-p", "0"]:
        raise CpldReadError(f"refusing xc3sprog arguments {args!r}: only '-c CABLE -p 0 FILE:r' is allowed")
    path, sep, action = args[4].rpartition(":")
    if sep != ":" or action != "r" or not path or ":" in path or path.startswith("-"):
        raise CpldReadError(f"refusing xc3sprog filespec {args[4]!r}: only the read action ':r' is allowed")
    if Path(path).exists():
        raise CpldReadError(f"refusing xc3sprog read into an existing file {path}")
    return args


def read_back(cfg, events=None, tmpdir=None):
    """Returns the readback JED text. Raises CpldReadError."""
    d = Path(tmpdir or Path(cfg.state_dir) / "diag-tmp")
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"cpld-readback-{os.getpid()}-{int(time.time() * 1000)}.jed"
    args = check_args(["-c", cfg.xc3sprog_cable, "-p", "0", f"{path}:r"], cfg.xc3sprog_cable)
    try:
        try:
            rc, out = tools.run([cfg.xc3sprog, *args], timeout=cfg.cpld_read_timeout, events=events)
        except tools.ToolError as e:
            raise CpldReadError(str(e)) from e
        text = path.read_text(errors="replace") if path.exists() else ""
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    if rc:
        raise CpldReadError(f"xc3sprog read failed (status {rc})")
    if "L" not in text or "*" not in text:
        raise CpldReadError("xc3sprog wrote no fuse data")
    return text
