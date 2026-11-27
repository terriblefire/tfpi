"""Flash a TF firmware release zip onto the board attached to this Pi.

Ported from tf4060/boards/tf4060r5/flash_ram.sh and flash_bus.sh:
  * a314d is stopped for the job and started again at the end (if it was
    running): since the TF4060 SPI bus handshake (tf4060v2 #123) it switches
    GPIO10/11 between input and ALT0 around every transfer, which would cut
    fomu-flash's bit-banged SPI clock between flash commands;
  * FPGA: identify the SPI flash by JEDEC ID (fomu-flash -i), check the image
    fits, fomu-flash -u -w, read the image length back (-b) and compare;
  * CPLD, on every flash: first find it on the JTAG chain (xc3sprog -j only
    reads IDCODEs, so it is tried up to CHAIN_TRIES times), then read its fuses
    back (FILE:v, verify only). If they already match the image, it isn't erased.
    Otherwise xc3sprog -c gpiod_creator -p 0 -v, one attempt per flash. A
    failure is recorded: the pages show it with Retry (the same release again),
    and the review page notes it before the next flash. Nothing is locked.
    "No JTAG Chain found" isn't recorded: xc3sprog stops there before it erases
    anything.
  * The FPGA is skipped when tfpi's record says its image is installed. The
    CPLD is always read back instead: the record can be out of date (the CPLD
    may have been programmed another way), and a readback costs no erase cycle.
  * FPGA boot: fomu-flash -r, setgpio.sh, wait for CDONE (GPIO 23). After a
    verified FPGA write (CDONE high is also the check that the board is powered
    before any CPLD attempt), and again as the job's last step after the CPLD or
    after any failure, so the FPGA is never left held in reset.

CLI:
  tfpi-flash status
  tfpi-flash inspect FILE.zip
  tfpi-flash flash FILE.zip [--force-fpga] [--name N] [--result R]
  tfpi-flash clear-cpld-fault
"""

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from . import config
from .fwimage import FirmwareError, load_release

# xc3sprog's exit code is the verify result (1 on a fuse mismatch); with -v it
# also prints "Success! Verify time ..." when every fuse matched.
CPLD_SUCCESS = re.compile(r"Success! Verify")
CPLD_PROBLEM = re.compile(r"Mismatch at|failed|No JTAG|not found|Unknown", re.I)
# xc3sprog -j lists each device it reads an IDCODE from, e.g.
# "JTAG loc.:   0  IDCODE: 0x59616093  Desc:   XC95288XL Rev: F  IR length:  8".
CPLD_CHAIN_ENTRY = re.compile(r"JTAG loc\.:\s*\d+\s+IDCODE:\s*0x[0-9a-f]{8}", re.I)
# Printed when no IDCODE could be read; xc3sprog then stops before any erase.
CPLD_NOT_REACHED = re.compile(r"No JTAG Chain found", re.I)
CHAIN_TRIES = 3
CHAIN_RETRY_SECONDS = 1.0
NOT_REACHED = ("CPLD not found on the JTAG chain (xc3sprog: No JTAG Chain found). Nothing was "
               "erased or written, so it is safe to flash again. If this keeps happening, check "
               "the board is powered and the Pi's JTAG connection to it.")


class FlashError(RuntimeError):
    pass


class CpldFailed(FlashError):
    """CPLD erase/program/verify failed (the job page offers Retry)."""


def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def run(cmd, check=False):
    """Run a command, echo its output into our log, return (rc, output)."""
    log("$ " + " ".join(str(part) for part in cmd))
    try:
        process = subprocess.Popen([str(part) for part in cmd], stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, errors="replace")
    except OSError as error:
        raise FlashError(f"cannot run {cmd[0]}: {error}") from error
    lines = []
    with process:
        for line in process.stdout:
            lines.append(line)
            print("    " + line.rstrip(), flush=True)
    rc = process.returncode
    if check and rc:
        raise FlashError(f"{cmd[0]} exited with status {rc}")
    return rc, "".join(lines)


@contextmanager
def flash_lock():
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    from . import diag
    if diag.running():
        raise FlashError("a diagnostic test is running")
    with open(config.LOCK_FILE, "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise FlashError("another flash is already running") from error
        yield


def plan(release, state, force_fpga=False):
    """Decide which chips to flash. Returns a dict the web page shows as-is."""
    fpga_same = release.fpga_sha == state["fpga"]["sha"]
    result = {
        "fpga": force_fpga or not fpga_same,
        "fpga_reason": ("forced" if force_fpga and fpga_same else
                        "already installed" if fpga_same else "new image"),
        "cpld": False,
        "cpld_reason": "",
        "note": None,
    }
    if release.cpld_jed is None:
        result["cpld_reason"] = ("no CPLD file for this machine in this zip"
                                 if any(m.lower().endswith(".jed") for m in release.members)
                                 else "no CPLD file in this zip")
    else:
        # Read back on every flash; programmed only if the fuses differ.
        result["cpld"] = True
        result["cpld_reason"] = ("recorded as installed" if release.cpld_sha == state["cpld"]["sha"]
                                 else "new image")
        if state["cpld"].get("fault"):
            result["note"] = ("The last CPLD flash failed (" + state["cpld"]["fault"] + "). If it "
                              "keeps failing, ask on Discord before trying many more times.")
    return result


# JEDEC ID (manufacturer, type, capacity byte) -> (name, bytes) for parts whose
# capacity byte isn't log2(size). Others fall back to 1 << capacity byte.
SPI_FLASH_PARTS = {
    (0xbf, 0x26, 0x41): ("Microchip SST26VF016B", 2 * 1024 * 1024),
    (0xbf, 0x26, 0x42): ("Microchip SST26VF032B", 4 * 1024 * 1024),
    (0xbf, 0x26, 0x43): ("Microchip SST26VF064B", 8 * 1024 * 1024),
    (0x1f, 0x86, 0x01): ("Adesto AT25SF161", 2 * 1024 * 1024),
}
MANUFACTURERS = {0xbf: "Microchip/SST", 0xef: "Winbond", 0xc8: "GigaDevice", 0xc2: "Macronix",
                 0x20: "Micron/ST", 0x9d: "ISSI", 0x1f: "Adesto", 0x01: "Spansion/Cypress"}


def parse_spi_id(text):
    """Parse `fomu-flash -i` output into (manufacturer, type, capacity) or None."""
    def field(pattern):
        found = re.search(pattern, text, re.M)
        return int(found.group(1), 16) if found else None
    maker = field(r"^!! JEDEC Manufacturer ID:\s*([0-9a-f]{2})")
    if maker is None:
        maker = field(r"^Manufacturer ID:.*\(([0-9a-f]{2})\)")
    kind = field(r"^Memory model:.*\(([0-9a-f]{2})\)")
    size = field(r"^Memory size:.*\(([0-9a-f]{2})\)")
    if None in (maker, kind, size) or maker in (0x00, 0xff):
        return None
    return maker, kind, size


def lookup_spi_flash(jedec):
    """(name, bytes) for a JEDEC ID; bytes is None when the size can't be known."""
    if jedec in SPI_FLASH_PARTS:
        return SPI_FLASH_PARTS[jedec]
    maker, kind, size = jedec
    name = f"{MANUFACTURERS.get(maker, 'unknown')} {maker:02x} {kind:02x} {size:02x}"
    return name, (1 << size) if 0x10 <= size <= 0x1a else None


def identify_spi_flash():
    rc, output = run([config.FOMU_FLASH, "-i"])
    jedec = parse_spi_id(output) if rc == 0 else None
    if jedec is None:
        raise FlashError("could not read the SPI flash JEDEC ID; is the board powered?")
    return lookup_spi_flash(jedec)


def wait_for_cdone(seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        out = subprocess.run([config.PINCTRL, "get", str(config.CDONE_GPIO)],
                             capture_output=True, text=True).stdout
        if re.search(r"\|\s*hi(\s|$)", out):
            return True
        time.sleep(0.1)
    return False


def flash_fpga(release, state, work):
    image = work / "fpga.bin"
    readback = work / "readback.bin"
    image.write_bytes(release.fpga_image)
    fpga = state["fpga"]
    name, size = identify_spi_flash()
    log(f"FPGA: SPI flash is {name}, " + (f"{size // 1024} KB" if size else "size unknown"))
    if size and len(release.fpga_image) > size:
        raise FlashError(f"image is {len(release.fpga_image)} bytes, the flash holds {size}")
    fpga["flashes"] += 1
    fpga["time"] = config.now()
    fpga["sha"] = None          # unknown until verified
    config.save_state(state)

    log(f"FPGA: writing {len(release.fpga_image)} bytes ({release.fpga_source})")
    rc, _ = run([config.FOMU_FLASH, "-q", "-u", "-w", image])
    if rc:
        fpga["last_result"] = "write-failed"
        raise FlashError(f"fomu-flash write failed (status {rc})")

    log("FPGA: reading the SPI flash back to verify")
    readback.unlink(missing_ok=True)
    # -b: fomu-flash only knows a few flash IDs; read exactly the image length.
    rc, _ = run([config.FOMU_FLASH, "-b", len(release.fpga_image), "-s", readback])
    data = readback.read_bytes() if readback.exists() else b""
    if rc or len(data) < len(release.fpga_image):
        fpga["last_result"] = "readback-failed"
        raise FlashError("could not read the SPI flash back")
    mismatches = sum(1 for a, b in zip(data, release.fpga_image) if a != b)
    if mismatches:
        fpga["last_result"] = "verify-failed"
        raise FlashError(f"SPI flash verify failed: {mismatches} bytes differ")
    fpga["sha"] = release.fpga_sha
    fpga["last_result"] = "ok"
    fpga["a314"] = release.a314_commit or None      # for the compatibility banner (compat.py)
    config.save_state(state)
    log("FPGA: verify OK")

    # Boot it now: CDONE high also shows the board is powered before a CPLD attempt.
    if not boot_fpga():
        fpga["last_result"] = "cdone-low"
        raise FlashError("flash verified, but the FPGA did not configure (CDONE low). "
                         "Check the board is powered.")


def boot_fpga():
    """Reset the FPGA so it configures from its SPI flash, hand the pins back
    (setgpio.sh), as flash_ram.sh does; True once CDONE is high. Never raises:
    it runs at the end of every flash job, including failed ones."""
    log("FPGA: reset (fomu-flash -r), booting from the SPI flash")
    try:
        run([config.FOMU_FLASH, "-r"])
        run(["bash", config.SETGPIO])
        ok = wait_for_cdone()
    except Exception as error:          # a missing tool must not hide the real result
        log(f"FPGA: reset failed: {error}")
        return False
    log("FPGA: CDONE high, FPGA configured from SPI flash" if ok else "FPGA: CDONE stayed low")
    return ok


def find_cpld():
    """Read the JTAG chain with xc3sprog -j, which only reads IDCODEs and erases or
    writes nothing, up to CHAIN_TRIES times. FlashError (no lock) if none answers."""
    for attempt in range(1, CHAIN_TRIES + 1):
        _, output = run([config.XC3SPROG, "-c", config.XC3SPROG_CABLE, "-j"])
        if CPLD_CHAIN_ENTRY.search(output):
            return
        if attempt < CHAIN_TRIES:
            log("CPLD: no JTAG chain found, reading it again")
            time.sleep(CHAIN_RETRY_SECONDS)
    raise FlashError(NOT_REACHED)


def cpld_matches(jed):
    """Read the CPLD's fuses back and compare them with the image (xc3sprog FILE:v
    only verifies: nothing is erased or written). True when every fuse matches."""
    log("CPLD: reading its fuses back to compare with this image")
    rc, output = run([config.XC3SPROG, "-c", config.XC3SPROG_CABLE, "-p", "0", "-v", f"{jed}:v"])
    if rc and CPLD_NOT_REACHED.search(output):
        raise FlashError(NOT_REACHED)
    if rc == 0 and CPLD_SUCCESS.search(output):
        return True
    differ = next((line.strip() for line in output.splitlines() if CPLD_PROBLEM.search(line)), None)
    log("CPLD: " + (f"differs from this image ({differ})" if differ else
                    f"could not be compared (xc3sprog exit status {rc})"))
    return False


def flash_cpld(release, state, work):
    """Read the CPLD back and program it only if its fuses differ from the image.
    Returns what happened, for the history."""
    jed = work / "cpld.jed"
    jed.write_bytes(release.cpld_jed)
    cpld = state["cpld"]
    try:
        find_cpld()
        same = cpld_matches(jed)
    except FlashError:
        if release.cpld_sha != cpld["sha"]:
            raise
        # Not reached, but tfpi's record says it holds this image: carry on, as
        # before the readback existed, rather than fail an FPGA update.
        log("WARNING: CPLD not found on the JTAG chain, so it wasn't checked. tfpi's "
            "record says it already holds this image.")
        return "not found, not checked"
    if same:
        cpld.update(sha=release.cpld_sha, device=release.cpld_device, file=release.cpld_file,
                    time=config.now(), last_result="ok", fault=None)
        config.save_state(state)
        log("CPLD: its fuses already match this image; not erased or programmed")
        return "fuses match, not erased"
    before = dict(cpld)
    cpld["flashes"] += 1
    cpld["time"] = config.now()
    cpld["sha"] = None
    cpld["file"] = None
    config.save_state(state)

    log(f"CPLD: programming {release.cpld_device or 'CPLD'} (one attempt, no retries)")
    rc, output = run([config.XC3SPROG, "-c", config.XC3SPROG_CABLE, "-p", "0", "-v", jed])
    if rc and CPLD_NOT_REACHED.search(output):
        # The chain went away after find_cpld(): the CPLD still holds what it had.
        state["cpld"] = dict(before, time=config.now(), last_result="not-found")
        config.save_state(state)
        raise FlashError(NOT_REACHED)
    if rc or not CPLD_SUCCESS.search(output):
        problem = next((line.strip() for line in output.splitlines() if CPLD_PROBLEM.search(line)), None)
        reason = f"xc3sprog exit status {rc}" + (f" ({problem})" if problem else
                                                  "" if rc else ", no verify success line")
        cpld["last_result"] = "failed"
        cpld["fault"] = f"{config.now()}: {reason}"
        cpld["fault_release"] = release.name           # what Retry flashes again
        config.save_state(state)
        raise CpldFailed(f"CPLD programming failed: {reason}.")
    cpld["sha"] = release.cpld_sha
    cpld["device"] = release.cpld_device
    cpld["file"] = release.cpld_file
    cpld["last_result"] = "ok"
    cpld["fault"] = None
    config.save_state(state)
    log("CPLD: programmed and verified")
    return "flash"


def store_release(zip_path, name, state):
    config.FIRMWARE_DIR.mkdir(parents=True, exist_ok=True)
    target = config.FIRMWARE_DIR / name
    if Path(zip_path).resolve() != target.resolve():
        shutil.copyfile(zip_path, target)
        with open(target, "rb") as handle:
            os.fsync(handle.fileno())
    target.touch()
    keep = {state.get("release")}
    stored = sorted(config.FIRMWARE_DIR.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in stored[config.KEEP_RELEASES:]:
        if old.name not in keep:
            old.unlink()


def failed_release():
    """The stored zip of the release whose CPLD flash failed last (for Retry),
    or None if none failed or it isn't kept any more."""
    name = config.load_state()["cpld"].get("fault_release")
    path = config.FIRMWARE_DIR / Path(name).name if name else None
    return path if path and path.is_file() else None


def forget_cpld_failure():
    state = config.load_state()
    state["cpld"]["fault"] = None
    state["cpld"]["fault_release"] = None
    config.save_state(state)


def a314d_active():
    return subprocess.run([config.SYSTEMCTL, "is-active", "--quiet", config.A314_SERVICE],
                          stdin=subprocess.DEVNULL).returncode == 0


def flash(zip_path, name=None, force_fpga=False):
    name = name or Path(zip_path).name
    release = load_release(zip_path, name=name, board=config.board(), spirom=config.spirom(),
                           ntsc=config.ntsc())
    log(f"release {name}: board {release.board}, seed {release.seed or '-'}")
    if release.cpld_file:
        log(f"BUS CPLD file: {release.cpld_file}" + (" (NTSC)" if release.cpld_ntsc else ""))
    for warning in release.warnings:
        log("WARNING: " + warning)
    with flash_lock():
        state = config.load_state()
        todo = plan(release, state, force_fpga)
        log(f"FPGA: {'flash' if todo['fpga'] else 'skip'} ({todo['fpga_reason']})")
        log(f"CPLD: {'read back, program if it differs' if todo['cpld'] else 'skip'} "
            f"({todo['cpld_reason']})")
        if todo["note"]:
            log("NOTE: " + todo["note"])
        entry = {"time": config.now(), "release": name, "seed": release.seed,
                 "fpga": "flash" if todo["fpga"] else "skip",
                 "cpld": "flash" if todo["cpld"] else "skip", "result": None}
        if not todo["fpga"] and not todo["cpld"]:
            entry["result"] = "nothing to do"
            log("nothing to flash: this release is already installed")
        else:
            work = config.STATE_DIR / "work"
            work.mkdir(parents=True, exist_ok=True)
            failure = None
            a314d_was_active = a314d_active()
            if a314d_was_active:
                log("a314d: stopped for the flash (it drives the shared SPI pins)")
                run([config.SYSTEMCTL, "stop", config.A314_SERVICE])
            try:
                if todo["fpga"]:
                    flash_fpga(release, state, work)
                if todo["cpld"]:
                    entry["cpld"] = flash_cpld(release, state, work)
            except Exception as error:
                failure = error
            finally:
                shutil.rmtree(work, ignore_errors=True)
                # After the CPLD, or after any failure (the FPGA may be held in reset),
                # reset the FPGA last so the board starts on what is in the flash.
                if todo["cpld"] or failure is not None:
                    booted = boot_fpga()
                    if failure is None and not booted:
                        failure = FlashError("After the CPLD step the FPGA did not configure "
                                             "(CDONE low). Power-cycle the Amiga.")
                if a314d_was_active:
                    log("a314d: started again")
                    run([config.SYSTEMCTL, "start", config.A314_SERVICE])
                entry["result"] = f"FAILED: {failure}" if failure else "ok"
                state["history"] = ([entry] + state["history"])[:50]
                if entry["result"] == "ok":
                    state["release"] = name
                    state["seed"] = release.seed
                config.save_state(state)
            if failure is not None:
                if isinstance(failure, CpldFailed):
                    try:
                        store_release(zip_path, name, state)     # kept for Retry
                    except OSError as error:
                        log(f"WARNING: couldn't keep {name} for Retry: {error}")
                raise failure
        if entry["result"] == "nothing to do":
            state["history"] = ([entry] + state["history"])[:50]
            config.save_state(state)
        store_release(zip_path, name, state)
    log("done: " + entry["result"])
    return entry


def main(argv=None):
    parser = argparse.ArgumentParser(prog="tfpi-flash", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    inspect = sub.add_parser("inspect")
    inspect.add_argument("zip", type=Path)
    run_flash = sub.add_parser("flash")
    run_flash.add_argument("zip", type=Path)
    run_flash.add_argument("--name")
    run_flash.add_argument("--force-fpga", action="store_true")
    run_flash.add_argument("--result", type=Path, help="write the outcome here as JSON")
    sub.add_parser("clear-cpld-fault")
    args = parser.parse_args(argv)

    if args.cmd == "status":
        print(json.dumps(config.load_state(), indent=2))
        return 0
    if args.cmd == "clear-cpld-fault":
        forget_cpld_failure()
        print("The record of the last failed CPLD flash is cleared.")
        return 0
    if args.cmd == "inspect":
        try:
            release = load_release(args.zip, board=config.board(), spirom=config.spirom(),
                                   ntsc=config.ntsc())
        except FirmwareError as error:
            print(f"rejected: {error}", file=sys.stderr)
            return 1
        print(json.dumps({"release": release.summary(),
                          "plan": plan(release, config.load_state())}, indent=2))
        return 0

    outcome = {"ok": False, "result": None}
    try:
        entry = flash(args.zip, args.name, args.force_fpga)
        outcome = {"ok": True, "result": entry["result"]}
    except (FirmwareError, FlashError) as error:
        log(f"FAILED: {error}")
        outcome = {"ok": False, "result": str(error), "cpld_failed": isinstance(error, CpldFailed)}
    except Exception as error:  # keep a result file even on a bug
        log(f"FAILED (internal error): {error!r}")
        outcome = {"ok": False, "result": f"internal error: {error!r}"}
    if args.result:
        config.write_json(args.result, outcome)
    return 0 if outcome["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
