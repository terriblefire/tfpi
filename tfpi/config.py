"""Paths, tool locations and the persistent state file.

Everything can be redirected with environment variables so the tests can run
on a desktop with stub tools.
"""

import json
import os
import tempfile
import time
from pathlib import Path

def set_paths(etc, state_dir, share):
    global ETC, STATE_DIR, SHARE, STATE_FILE, FIRMWARE_DIR, UPLOAD_DIR, JOB_DIR
    global LOCK_FILE, SPIROM_FILE, WEB_CONF, VERSIONS_FILE
    ETC, STATE_DIR, SHARE = Path(etc), Path(state_dir), Path(share)
    STATE_FILE = STATE_DIR / "state.json"
    FIRMWARE_DIR = STATE_DIR / "firmware"     # stored release zips (rollback)
    UPLOAD_DIR = STATE_DIR / "uploads"        # zips uploaded but not yet flashed
    JOB_DIR = STATE_DIR / "jobs"              # per-flash logs and results
    LOCK_FILE = STATE_DIR / "flash.lock"
    SPIROM_FILE = ETC / "spirom.img"
    WEB_CONF = ETC / "web.json"
    VERSIONS_FILE = SHARE / "versions.json"


set_paths(os.environ.get("TFPI_ETC", "/etc/tfpi"),
          os.environ.get("TFPI_STATE_DIR", "/var/lib/tfpi"),
          os.environ.get("TFPI_SHARE", "/opt/tfpi"))

FOMU_FLASH = os.environ.get("TFPI_FOMU_FLASH", "fomu-flash")
XC3SPROG = os.environ.get("TFPI_XC3SPROG", "xc3sprog")
XC3SPROG_CABLE = os.environ.get("TFPI_XC3SPROG_CABLE", "gpiod_creator")
PINCTRL = os.environ.get("TFPI_PINCTRL", "pinctrl")
SYSTEMCTL = os.environ.get("TFPI_SYSTEMCTL", "systemctl")
SETGPIO = os.environ.get("TFPI_SETGPIO", str(SHARE / "setgpio.sh"))
CDONE_GPIO = int(os.environ.get("TFPI_CDONE_GPIO", "23"))
A314_SERVICE = "a314d"

KEEP_RELEASES = 3


def read_conf():
    """/etc/tfpi/tfpi.conf: simple KEY=value lines (BOARD=tf4060r5)."""
    conf = {"BOARD": "tf4060r5"}
    path = ETC / "tfpi.conf"
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                conf[key.strip()] = value.strip().strip('"')
    return conf


def board():
    return read_conf().get("BOARD") or None


def spirom():
    return SPIROM_FILE.read_bytes() if SPIROM_FILE.exists() else None


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    # The Pi often loses power with the Amiga: get the data onto the card before
    # the rename, then make the rename itself durable.
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    temporary.replace(path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def empty_state():
    return {
        "release": None,          # name of the zip last flashed through this tool
        "seed": None,
        "fpga": {"sha": None, "flashes": 0, "last_result": None, "time": None},
        "cpld": {"sha": None, "device": None, "flashes": 0, "last_result": None,
                 "time": None, "fault": None},
        "history": [],
    }


def load_state():
    state = empty_state()
    stored = read_json(STATE_FILE, {})
    for key, value in stored.items():
        if isinstance(value, dict) and isinstance(state.get(key), dict):
            state[key].update(value)
        else:
            state[key] = value
    return state


def save_state(state):
    write_json(STATE_FILE, state)


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")
