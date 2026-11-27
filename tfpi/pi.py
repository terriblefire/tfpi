"""Reboot the Pi from the page, and know when it needs one.

The Pi needs a reboot only when its boot files changed (config.txt, cmdline.txt,
the a314 SPI overlay). Whatever changes them (the installer, a software update)
calls mark_reboot(). The record holds the kernel's boot_id, so it stops counting
as soon as the Pi has rebooted.
"""

import os
import subprocess
import threading
import time
from pathlib import Path

from . import config

BOOT_ID = Path(os.environ.get("TFPI_BOOT_ID", "/proc/sys/kernel/random/boot_id"))
DELAY = 2.0         # seconds: the browser gets the reply and the next page before the Pi goes


def record_file():
    return config.STATE_DIR / "reboot.json"


def error_file():
    return config.STATE_DIR / "reboot-error.json"


def boot_id():
    try:
        return BOOT_ID.read_text().strip()
    except OSError:
        return ""


def mark_reboot(why):
    """This boot needs a reboot; every page shows `why` until it happens."""
    config.write_json(record_file(), {"boot_id": boot_id(), "why": why, "time": time.time()})


def reboot_needed():
    """The record of why this Pi needs a reboot, or None (none, or rebooted since)."""
    record = config.read_json(record_file(), None)
    current = boot_id()
    if not isinstance(record, dict) or not current or record.get("boot_id") != current:
        return None
    return record


def reboot_error():
    found = config.read_json(error_file(), None)
    return found.get("error") if isinstance(found, dict) else None


def reboot():
    """Reboot in DELAY seconds. A failure is kept for the page that waits for
    the Pi to come back (reboot_error)."""
    error_file().unlink(missing_ok=True)

    def work():
        try:
            done = subprocess.run([config.SYSTEMCTL, "reboot"], capture_output=True, text=True, timeout=30)
            error = None
            if done.returncode:
                error = (done.stderr or done.stdout).strip()[-300:] or f"systemctl reboot: status {done.returncode}"
        except (OSError, subprocess.TimeoutExpired) as exc:
            error = f"systemctl reboot: {exc}"
        if error:
            config.write_json(error_file(), {"error": error, "time": time.time()})

    timer = threading.Timer(DELAY, work)
    timer.daemon = True
    timer.start()
    return timer
