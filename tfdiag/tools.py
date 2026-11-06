"""The only place tfdiag starts external programs."""

import re
import subprocess
import time


class ToolError(RuntimeError):
    pass


def run(cmd, timeout=120, events=None, check=False):
    """Run cmd, return (rc, output). Output (stdout + stderr) goes to the event log."""
    cmd = [str(c) for c in cmd]
    if events:
        events.info("$ " + " ".join(cmd))
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                           errors="replace", timeout=timeout)
    except FileNotFoundError as e:
        raise ToolError(f"cannot run {cmd[0]}: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise ToolError(f"{cmd[0]} timed out after {timeout} s") from e
    out = p.stdout or ""
    if events:
        for line in out.splitlines()[-40:]:
            events.info("    " + line)
    if check and p.returncode:
        raise ToolError(f"{cmd[0]} exited with status {p.returncode}")
    return p.returncode, out


class Gpio:
    def __init__(self, cfg):
        self.cfg = cfg

    def cdone(self):
        try:
            rc, out = run([self.cfg.pinctrl, "get", str(self.cfg.cdone_gpio)], timeout=10)
        except ToolError:
            return None
        if rc:
            return None
        return bool(re.search(r"\|\s*hi(\s|$)", out))

    def wait_cdone(self, seconds=None, sleep=time.sleep):
        deadline = time.monotonic() + (self.cfg.cdone_timeout if seconds is None else seconds)
        while True:
            if self.cdone():
                return True
            if time.monotonic() >= deadline:
                return False
            sleep(0.1)

    def setgpio(self, events=None):
        """Hand the SPI pins back to the SPI controller (share/setgpio.sh)."""
        return run(["bash", self.cfg.setgpio], timeout=30, events=events)


class Service:
    def __init__(self, cfg, name=None):
        self.cfg = cfg
        self.name = name or cfg.a314_service

    def active(self):
        try:
            rc, out = run([self.cfg.systemctl, "is-active", self.name], timeout=30)
        except ToolError:
            return None
        return out.strip() == "active"

    def stop(self, events=None):
        return run([self.cfg.systemctl, "stop", self.name], timeout=60, events=events)[0] == 0

    def start(self, events=None, wait=True):
        # wait=False (--no-block) for the boot-time recovery unit: it is ordered Before=a314d,
        # so a blocking start from inside it would wait for itself until the timeout.
        cmd = [self.cfg.systemctl, "start"] + ([] if wait else ["--no-block"]) + [self.name]
        return run(cmd, timeout=60, events=events)[0] == 0
