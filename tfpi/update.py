"""Software updates for this Pi: is there a newer tfpi, what would it change,
and run `tfpi-update` as a job the web page can follow.

The check reads the tfpi repo's main branch with `git ls-remote` (no API
limits) and that commit's sources.conf, and compares the pins with what the
last install recorded in versions.json. The installer only rebuilds parts
whose pin changed, so a314d is interrupted only if the a314 pin moves.

Run: python3 -m tfpi.update check | run --log FILE --result FILE
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from . import config

REPO = os.environ.get("TFPI_UPDATE_REPO", "https://github.com/terriblefire/tfpi")
BRANCH = "main"
UPDATE_CMD = os.environ.get("TFPI_UPDATE_CMD", "/usr/local/bin/tfpi-update")
SYSTEMD_RUN = os.environ.get("TFPI_SYSTEMD_RUN", "systemd-run")
A314D = Path(os.environ.get("TFPI_A314D", "/opt/a314/a314d"))
BOOT_FILES = [Path(p) for p in os.environ.get(
    "TFPI_BOOT_FILES", "/boot/firmware/config.txt:/boot/firmware/cmdline.txt").split(":")]
CHECK_EVERY = 12 * 3600
TIMEOUT = 20

# sources.conf key -> versions.json name. xc3sprog has two pins (libgpiod 1/2).
PINS = {"fomu-flash": ("FOMU_FLASH_REF",), "xc3sprog": ("XC3SPROG_REF", "XC3SPROG_REF_GPIOD1"),
        "a314": ("A314_REF",)}


class UpdateError(RuntimeError):
    pass


def check_file():
    return config.STATE_DIR / "update_check.json"


def lock_file():
    return config.STATE_DIR / "update.lock"


def _github_path():
    match = re.match(r"https://github\.com/([^/]+/[^/.]+)", REPO)
    return match.group(1) if match else None


def remote_head():
    try:
        out = subprocess.run(["git", "ls-remote", REPO, f"refs/heads/{BRANCH}"], capture_output=True,
                             text=True, timeout=TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise UpdateError(f"can't reach {REPO}: {error}") from error
    if out.returncode or not out.stdout.strip():
        raise UpdateError(f"can't reach {REPO}: {(out.stderr or 'no answer').strip()[-200:]}")
    return out.stdout.split()[0]


def _get(url):
    request = urllib.request.Request(url, headers={"User-Agent": "tfpi-update-check"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as reply:
        return reply.read()


def remote_pins(commit):
    """{KEY: value} from sources.conf at `commit`."""
    text = _get(f"https://raw.githubusercontent.com/{_github_path()}/{commit}/sources.conf").decode()
    pins = {}
    for line in text.splitlines():
        match = re.match(r"\s*([A-Z0-9_]+)=(\S+)", line)
        if match:
            pins[match.group(1)] = match.group(2).strip("\"'")
    return pins


def remote_changes(old, new):
    """Commit subjects between two tfpi commits (best effort: GitHub API)."""
    try:
        data = json.loads(_get(f"https://api.github.com/repos/{_github_path()}/compare/{old}...{new}"))
        return [c["commit"]["message"].splitlines()[0] for c in data.get("commits", [])][-20:]
    except Exception:          # rate limit, unknown old commit, offline: just no list
        return []


def sha256(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def compare(installed, head, pins):
    """What updating to `head` would do."""
    tfpi_ref = installed.get("tfpi", {}).get("ref", "")
    parts = []
    for name, keys in PINS.items():
        have = installed.get(name, {}).get("ref", "")
        wanted = [pins[k] for k in keys if k in pins]
        if wanted and have not in wanted:
            parts.append(name)
    a314 = installed.get("a314", {})
    built = a314.get("a314d_sha256") or None
    current = sha256(A314D)
    return {
        "available": bool(head) and head != tfpi_ref,
        "installed": tfpi_ref,
        "latest": head,
        "rebuild": parts,
        "a314d_interrupted": "a314" in parts,
        # a314d differs from what the installer built: a hand-built one.
        "a314d_local": bool(built and current and built != current),
        "a314d_unknown": not built,
    }


def check():
    """Ask GitHub, cache and return the result."""
    installed = config.read_json(config.VERSIONS_FILE, {})
    result = {"time": time.time(), "error": None}
    try:
        head = remote_head()
        result.update(compare(installed, head, remote_pins(head)))
        if result["available"] and result["installed"]:
            result["changes"] = remote_changes(result["installed"], head)
    except (UpdateError, OSError, ValueError) as error:
        result["error"] = str(error)
    config.write_json(check_file(), result)
    return result


def cached():
    return config.read_json(check_file(), None)


def stale():
    last = cached()
    return last is None or time.time() - last.get("time", 0) > CHECK_EVERY


def running():
    path = lock_file()
    if not path.exists():
        return False
    with open(path) as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
    return False


def start(job_dir, job, cwd):
    """Start `run` detached. Under systemd it gets its own unit: the installer
    restarts tfpi-web, which must not take the update down with it."""
    log, result = job_dir / f"{job}.log", job_dir / f"{job}.json"
    args = [sys.executable, "-m", "tfpi.update", "run", "--log", str(log), "--result", str(result)]
    log.write_text("")
    try:
        launched = subprocess.run([SYSTEMD_RUN, f"--unit=tfpi-update-{job}", "--collect", "--quiet",
                                   f"--setenv=PYTHONPATH={cwd}", f"--working-directory={cwd}", *args],
                                  capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        launched = None                 # no systemd (tests, containers): plain child
    except (OSError, subprocess.TimeoutExpired) as error:
        raise UpdateError(f"systemd-run failed: {error}") from error
    if launched is not None:
        if launched.returncode:
            raise UpdateError(f"systemd-run failed: {launched.stderr.strip()[-300:]}")
        return
    child = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True, cwd=cwd)
    threading.Thread(target=child.wait, daemon=True).start()


def run(log, result):
    """Run the installer with both locks held (no flash can start meanwhile)."""
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(lock_file(), "w") as update_lock, open(config.LOCK_FILE, "a") as flash_lock, \
            open(log, "a", buffering=1) as out:
        try:
            fcntl.flock(update_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(flash_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            message = "a flash or another update is running"
            out.write(message + "\n")
            config.write_json(Path(result), {"ok": False, "result": message, "kind": "update"})
            return 1
        boot_before = [sha256(p) for p in BOOT_FILES]
        out.write(f"$ {UPDATE_CMD}\n")
        try:
            rc = subprocess.run([UPDATE_CMD], stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL).returncode
        except OSError as error:
            out.write(f"{error}\n")
            rc = 127
        reboot = boot_before != [sha256(p) for p in BOOT_FILES]
        os.sync()
        if rc == 0:
            summary = "updated" + ("; config.txt/cmdline.txt changed, reboot the Pi (and the Amiga) to use it"
                                   if reboot else "")
        else:
            summary = f"tfpi-update exited with status {rc}; the log above says where it stopped"
        config.write_json(Path(result), {"ok": rc == 0, "result": summary, "kind": "update",
                                         "reboot": reboot})
    try:
        check()             # refresh "update available" for the page
    except Exception:
        pass
    return 0 if rc == 0 else 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="tfpi.update")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    runner = sub.add_parser("run")
    runner.add_argument("--log", required=True)
    runner.add_argument("--result", required=True)
    args = parser.parse_args(argv)
    if args.cmd == "check":
        print(json.dumps(check(), indent=2))
        return 0
    return run(args.log, args.result)


if __name__ == "__main__":
    sys.exit(main())
