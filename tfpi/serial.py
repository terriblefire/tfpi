"""The Amiga's serial port through the Pi: with FPGA firmware that has the
fake serial port, the SSBL sends what the Amiga writes with RawPutChar
(debug output, kprintf, Enforcer hits) to the FPGA, and a314d writes it to
serial.log in its runtime directory. a314d also offers a pty there (tty);
what is written to it reaches the Amiga's RawMayGetChar. The Serial Port
page gets both live through tfpi/serialws.py."""

import os
from pathlib import Path

# a314d's runtime directory (RuntimeDirectory=a314d, systemd/a314d.service.d),
# and the one it uses when that is missing
DIRS = (Path("/run/a314d"), Path("/tmp/a314d"))
LOG_NAME = "serial.log"
OLD_NAME = "serial.log.1"           # a314d moves serial.log here at 1 MB
TTY_NAME = "tty"
TAIL = 64 * 1024                    # what the page shows first: the end of the log
CHUNK = 256 * 1024


def directory():
    """a314d's directory with the log or the pty, or None."""
    for d in DIRS:
        if (d / LOG_NAME).exists() or (d / TTY_NAME).exists():
            return d
    return None


def _read(path, offset, limit):
    try:
        with open(path, "rb") as handle:
            handle.seek(offset)
            return handle.read(limit)
    except FileNotFoundError:
        return b""


def read(offset):
    """(text, next offset): the log from byte offset on, as the Amiga sent it
    (one character per byte, Latin-1); offset < 0 gives the last TAIL bytes.
    When a314d has started a new log since (offset past its end), the rest of
    the old one comes first, then the new one from 0. text is None without
    a314d's directory."""
    d = directory()
    if d is None:
        return None, 0
    path = d / LOG_NAME
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return "", 0
    data = b""
    if offset < 0:
        offset = max(0, size - TAIL)
    elif offset > size:
        data = _read(d / OLD_NAME, offset, CHUNK)
        offset = 0
    new = _read(path, offset, CHUNK)
    return (data + new).decode("latin-1"), offset + len(new)


def log_bytes():
    """The whole log (the old part first), for download."""
    d = directory()
    if d is None:
        return b""
    return _read(d / OLD_NAME, 0, 2 * 1024 * 1024) + _read(d / LOG_NAME, 0, 2 * 1024 * 1024)


def send_raw(text):
    """Type text on the Amiga's side of the port, byte for byte (Latin-1).
    Raises OSError without the pty."""
    d = directory()
    if d is None or not (d / TTY_NAME).exists():
        raise OSError("no serial port: a314d is not running, or this a314d has no serial port")
    data = text.encode("latin-1", "replace")
    fd = os.open(d / TTY_NAME, os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
