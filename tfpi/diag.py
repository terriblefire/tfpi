"""Diagnostics tab: run a tfdiag capture as a job, always restore, offer the zip.

The hardware work is done by the `tfdiag` package, which lives in this repo
next to `tfpi/` and is installed with it. The diagnostic FPGA images live in
tfpi/diag_images/ with their diag_manifest.json (tfdiag/manifest.py):

  diag_timing.bin  diag_pins.bin  diagex_body.bin  diag_manifest.json

No pass/fail on the captured data: good or bad is decided at home base from
the zip. The board test and the exerciser list "findings" (observations); the
restore check ("Restored: OK" / what differs) is the one real check.

tfdiag CLI used (docs/DIAGNOSTICS.md in the tf4060 repo; JSON lines on stdout;
exit 0 capture complete and restored, 1 capture incomplete or restore differs,
2 could not run):
  tfdiag run --images IMG --out RUN [--no-board] [--no-timing] [--presets 0,2]
             [--tests 0x..] [--expect-build H]
      snapshot (stops a314d, reads the flash with the FPGA held in reset: the
      Amiga crashes or resets here), board test, timing capture, report and the
      restore check in one process holding tfdiag's diag.lock and tfpi's
      flash.lock. SIGTERM stops the capture and still restores.
  tfdiag recover [--before F] --out F    restore + check after a power loss
tfdiag owns the power-loss marker /var/lib/tfpi/diag-in-progress (written by
the snapshot, deleted by a passing restore/recover). tfpi keeps its own record
of the job in diag/current.json; tfpi-diag-recover.service runs
`tfpi.diag recover` at boot when either exists.

Run: python3 -m tfpi.diag run --job J --actions board,timing [--presets ..] [--tests ..] --result R
     python3 -m tfpi.diag recover [--job J --result R]
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import zipfile
from contextlib import ExitStack
from pathlib import Path

from . import config

LIB = Path(__file__).resolve().parent.parent          # holds tfpi/ and tfdiag/

WARNING = ("This page is for diagnostic capture only. Tests temporarily load diagnostic firmware "
           "and will reset your Amiga. Save your work first. When the tests finish, the machine "
           "is returned to its original state.")
WARNING_POWER = "Do not power off during a test."
WARNING_SCREEN = ("During the tests the Amiga's screen will go blank and may change colour. "
                  "This is normal.")
WARNING_FIRST_STEP = ("The first step stops a314d (the Amiga loses PI0: and the other a314 services) and "
                      "reads the FPGA's flash with the FPGA held in reset: the Amiga crashes or resets at "
                      "that moment, before any test runs.")
DONE_MESSAGE = "Capture complete - download the zip and send it to us"
WHAT_TO_DO = ("Power-cycle the Amiga (the Pi restarts with it). If the main TF Pi page still shows "
              "a problem afterwards, reflash the firmware from the main TF Pi page.")
ACTIONS = ("board", "timing")
ACTION_NAMES = {"board": "Board test (experimental)", "timing": "Timing capture"}
# the JTAG soldering check can report spurious faults (tfdiag/board.py)
BOARD_EXPERIMENTAL = ("The board test (JTAG soldering check) is experimental: it can report spurious "
                      "faults, and its findings should not be taken as an indication of a real fault.")
DIAG_IMAGES = ("diag_timing.bin", "diag_pins.bin", "diagex_body.bin")
KEEP_RUNS = 10
HANDLE_MAX, NOTE_MAX = 64, 2000
CPUINFO = Path(os.environ.get("TFPI_CPUINFO", "/proc/cpuinfo"))

# CPU clock presets (scripts/clk_presets.py in the tf4060 repo); the manifest's "presets" overrides.
DEFAULT_PRESETS = [{"id": i, "name": f"{mhz:g} MHz" + (" (half bus)" if half else "")}
                   for i, (mhz, half) in enumerate([
                       (50.0, 0), (53.125, 0), (62.5, 1), (65.625, 1), (68.75, 1), (75.0, 1),
                       (78.125, 1), (81.25, 1), (87.5, 1), (90.625, 1), (93.75, 1), (96.875, 1),
                       (100.0, 1), (103.125, 1), (106.25, 1), (109.375, 1)])]
ALL_TESTS = 0xFFFFFFFF
BOARD_STEPS = {"board", "jtag", "sample", "pins", "extest"}


class DiagError(RuntimeError):
    pass


# --- paths (functions: tests reload config) ----------------------------------

def images_dir():
    return Path(os.environ.get("TFPI_DIAG_IMAGES") or Path(__file__).resolve().parent / "diag_images")


def diag_dir():
    return config.STATE_DIR / "diag"


def marker_file():
    """tfdiag's power-loss marker: from the snapshot until a passing restore."""
    return config.STATE_DIR / "diag-in-progress"


def current_file():
    """tfpi's record of the job in progress (job, run folder, pid)."""
    return diag_dir() / "current.json"


def lock_file():
    """tfpi's job lock (tfdiag's own is /var/lib/tfpi/diag.lock)."""
    return diag_dir() / "job.lock"


def runs_dir():
    return diag_dir() / "runs"


def recovery_file():
    return diag_dir() / "recovery.json"


# --- state -------------------------------------------------------------------

def running():
    """A diag job or recovery holds the job lock."""
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


def current():
    return config.read_json(current_file(), None)


def marker_before():
    """The before.json file tfdiag's marker names, if it is on disk. (The marker
    also carries the record itself, which `tfdiag recover` uses without --before.)"""
    data = config.read_json(marker_file(), None)
    if not isinstance(data, dict):
        return None
    for key in ("before_file", "before"):
        if isinstance(data.get(key), str) and Path(data[key]).is_file():
            return Path(data[key])
    return None


def pending_restore():
    """A job left the machine unrestored and nothing is running now."""
    return (marker_file().exists() or current_file().exists()) and not running()


def forget():
    """The user power-cycled/reflashed and wants to run tests again."""
    marker_file().unlink(missing_ok=True)
    current_file().unlink(missing_ok=True)


def recovery():
    """The last power-loss recovery, until dismissed."""
    data = config.read_json(recovery_file(), None)
    return data if data and not data.get("dismissed") else None


def dismiss_recovery():
    data = config.read_json(recovery_file(), None)
    if data:
        data["dismissed"] = True
        config.write_json(recovery_file(), data)


# --- the diagnostic images (tfpi/diag_images) --------------------------------------

def images_status():
    """{"ok", "reason", "info"}: are the diagnostic images installed and intact?"""
    folder = images_dir()
    out = {"ok": False, "reason": None, "info": {}, "dir": str(folder)}
    missing = [n for n in DIAG_IMAGES if not (folder / n).is_file()]
    if missing or not (folder / "diag_manifest.json").is_file():
        out["reason"] = ("Diagnostic images not installed on this Pi (" +
                         ", ".join(missing + ([] if (folder / "diag_manifest.json").is_file()
                                              else ["diag_manifest.json"])) +
                         " missing). They come with tfpi: update the Pi software from the main page.")
        return out
    from tfdiag import manifest
    info = manifest.load(folder)
    for name in DIAG_IMAGES:
        want = (info.get("images") or {}).get(name)
        if not want:
            out["reason"] = f"diag_manifest.json has no checksum for {name}."
            return out
        if hashlib.sha256((folder / name).read_bytes()).hexdigest() != want:
            out["reason"] = (f"{name} doesn't match its checksum in diag_manifest.json: the diagnostic "
                             "images are damaged. Update or reinstall the Pi software.")
            return out
    out.update(ok=True, info=info)
    return out


def presets(info=None):
    """[{"id", "name"}]: the manifest's "presets" (ids or {"id", "name"}), else the r5 table."""
    names = {p["id"]: p["name"] for p in DEFAULT_PRESETS}
    listed = (info or {}).get("presets")
    if not listed:
        return DEFAULT_PRESETS
    out = []
    for entry in listed:
        if isinstance(entry, dict) and "id" in entry:
            out.append({"id": int(entry["id"]), "name": str(entry.get("name") or names.get(entry["id"], ""))})
        elif isinstance(entry, int):
            out.append({"id": entry, "name": names.get(entry, f"preset {entry}")})
    return out or DEFAULT_PRESETS


def tests(info=None):
    """[{"bit", "name"}]: the manifest's "tests", else tfdiag's exerciser test bits.
    All of them ticked = no --tests (the exerciser's own default set)."""
    listed = [t for t in (info or {}).get("tests") or [] if isinstance(t, dict) and "bit" in t]
    if listed:
        return listed
    from tfdiag import protocol
    return [{"bit": bit, "name": name} for bit, name in sorted(protocol.TEST_BITS.items())]


# --- contact details the user may add to the zip --------------------------------------

def clean_text(text, limit, lines=False):
    """Strip control characters (keep newlines and tabs in a note) and limit the length."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    keep = "\n\t" if lines else ""
    text = "".join(c for c in text if c in keep or (c >= " " and not 0x7f <= ord(c) < 0xa0))
    return text.strip()[:limit]


def write_contact(rundir, handle, note):
    """contact.txt in the run folder (and so in the zip); removed when both are empty."""
    handle, note = clean_text(handle, HANDLE_MAX), clean_text(note, NOTE_MAX, lines=True)
    path = Path(rundir) / "contact.txt"
    if not handle and not note:
        path.unlink(missing_ok=True)
        return None
    path.write_text("Discord / forum handle: " + (handle or "-") + "\n\nWhat's the problem?\n"
                    + (note or "-") + "\n")
    return path


def contact(rundir):
    path = Path(rundir) / "contact.txt"
    try:
        text = path.read_text()
    except OSError:
        return {"handle": "", "note": ""}
    found = re.match(r"Discord / forum handle: (.*)\n\nWhat's the problem\?\n(.*)\Z", text, re.S)
    if not found:
        return {"handle": "", "note": ""}
    handle, note = found.group(1), found.group(2).rstrip("\n")
    return {"handle": "" if handle == "-" else handle, "note": "" if note == "-" else note}


# --- running tfdiag ----------------------------------------------------------------

def tfdiag_cmd():
    override = os.environ.get("TFPI_TFDIAG")
    return shlex.split(override) if override else [sys.executable, "-m", "tfdiag"]


def tfdiag_env():
    env = dict(os.environ)
    env["PYTHONPATH"] = str(LIB) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def stamp():
    return time.strftime("%H:%M:%S")


def say(message):
    print(f"[{stamp()}] {message}", flush=True)


SHOWN = {"info": "", "finding": "FINDING", "incomplete": "INCOMPLETE", "pass": "OK", "fail": "DIFFERS"}


def render(line):
    """One tfdiag output line, readable. Non-JSON lines pass through."""
    line = line.rstrip("\n")
    try:
        event = json.loads(line)
    except ValueError:
        return "    " + line
    if not isinstance(event, dict):
        return "    " + line
    kind = event.get("event", "")
    msg = str(event.get("msg") or event.get("summary") or "")
    step = event.get("step")
    if kind == "step":
        return f"[{stamp()}] == {step or ''}{': ' + msg if msg else ''}"
    if kind == "progress":
        pct = event.get("pct")
        return f"    {'%3d%% ' % pct if isinstance(pct, (int, float)) else ''}{msg}"
    if kind == "result":
        status = str(event.get("status", ""))
        mark = SHOWN.get(status, status.upper())
        return f"    {mark + ' ' if mark else ''}{event.get('name') or step or ''}: {msg}"
    if kind == "log":
        level = event.get("level", "info")
        return ("    " if level == "info" else f"    {level.upper()}: ") + msg
    if kind == "done":
        return f"    done: {msg}"
    return "    " + json.dumps(event, sort_keys=True)


class Runner:
    """Runs tfdiag; SIGTERM (Abort) is passed on to it (tfdiag then restores)."""

    def __init__(self, rundir):
        self.rundir = rundir
        self.child = None
        self.aborted = False
        self.protect = False
        self.results = []
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, self.abort)

    def abort(self, _signum, _frame):
        if self.protect:
            say("abort requested: the restore is running and is not interrupted")
            return
        self.aborted = True
        if self.child and self.child.poll() is None:
            self.child.terminate()

    def tool(self, *args):
        """Run one tfdiag command. Returns (exit status, final done event or None)."""
        cmd = tfdiag_cmd() + [str(a) for a in args]
        done = None
        try:
            self.child = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                          stdin=subprocess.DEVNULL, text=True, errors="replace",
                                          env=tfdiag_env(), cwd=str(self.rundir))
        except OSError as error:
            say(f"cannot run tfdiag: {error}")
            return 127, None
        with self.child, open(self.rundir / "progress.jsonl", "a") as raw:
            for line in self.child.stdout:
                raw.write(line)
                print(render(line), flush=True)
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get("event") == "done":
                    done = event
                elif isinstance(event, dict) and event.get("event") == "result":
                    self.results.append(event)
        rc = self.child.returncode
        self.child = None
        return rc, done


def restore_outcome(path, rc):
    data = config.read_json(path, None)
    if not isinstance(data, dict):
        data = {"ok": False, "differences": [f"the restore check did not run to the end (tfdiag exit status {rc})"]}
    data.setdefault("differences", [])
    data.setdefault("advice", "")
    return data


def step_status(results, actions):
    """{"board": {"state": "captured|incomplete", "findings": n}, ...} from tfdiag's results."""
    out = {a: {"state": "captured", "findings": 0} for a in actions}
    for event in results:
        step = event.get("step") or ""
        if step in ("snapshot", "restore"):
            continue
        action = "board" if step in BOARD_STEPS or "timing" not in actions else "timing"
        if action not in out:
            continue
        if event.get("status") == "finding":
            out[action]["findings"] += 1
        elif event.get("status") == "incomplete":
            out[action]["state"] = "incomplete"
    return out


# --- download --------------------------------------------------------------------------

def pi_serial():
    try:
        found = re.search(r"^Serial\s*:\s*([0-9a-fA-F]+)", CPUINFO.read_text(), re.M)
    except OSError:
        return None
    return found.group(1)[-8:] if found else None


def zip_name(rundir):
    before = config.read_json(rundir / "before.json", {})
    serial = re.sub(r"[^A-Za-z0-9]", "", str(before.get("serial") or pi_serial() or ""))[-16:] or "unknown"
    started = config.read_json(rundir / "run.json", {}).get("date") or time.strftime("%Y%m%d")
    return f"tf4060-diag-{serial}-{started}.zip"


def build_zip(rundir, job_logs=()):
    """The run folder plus the job logs, as one zip with IPs and host names removed
    (tfdiag.bundle). contact.txt is in it when the user filled it in."""
    from tfdiag import bundle
    logs = rundir / "logs"
    for log in job_logs:
        if Path(log).is_file():
            logs.mkdir(exist_ok=True)
            shutil.copyfile(log, logs / Path(log).name)
    for old in rundir.glob("tf4060-diag-*.zip"):
        old.unlink()
    target = rundir / zip_name(rundir)
    temporary = rundir / (target.name + ".tmp")
    bundle.make(rundir, temporary)
    with open(temporary, "rb") as handle:
        os.fsync(handle.fileno())
    temporary.replace(target)
    os.sync()
    return target


def rebuild_zip(rundir):
    job = rundir.name
    return build_zip(rundir, [config.JOB_DIR / f"{job}.log"] if (config.JOB_DIR / f"{job}.log").exists() else [])


VIEWABLE = {".html": "text/html", ".svg": "image/svg+xml", ".txt": "text/plain"}


def viewable(rundir, name):
    """A report/SVG/text file inside this run's folder, or None (nothing outside it)."""
    root = Path(rundir).resolve()
    if not name or "\\" in name or name.startswith("/"):
        return None
    try:
        path = (root / name).resolve()
    except (OSError, ValueError):
        return None
    if not path.is_relative_to(root) or not path.is_file() or path.suffix.lower() not in VIEWABLE:
        return None
    return path


def wave_svgs(rundir):
    """Waveform SVGs (timing/waves/*.svg) as paths relative to the run folder."""
    folder = Path(rundir)
    return sorted(p.relative_to(folder).as_posix() for p in folder.glob("timing/waves/*.svg") if p.is_file())


def render_waves(rundir):
    """Draw every captured VCD as an SVG for the result page (tfdiag.vcdsvg, stdlib only)."""
    try:
        from tfdiag import vcdsvg
    except ImportError:
        return 0
    done = 0
    for vcd in sorted(Path(rundir).glob("timing/waves/*.vcd")):
        if vcd.with_suffix(".svg").exists():
            continue
        try:
            vcdsvg.convert(vcd)
            done += 1
        except Exception as error:      # a drawing problem must not cost the capture
            say(f"waveform {vcd.name}: no SVG ({error})")
    return done


# --- the result page's own summary (numbers from tfdiag's helpers, as in the report) ---

BUS_MEAS = ((0, "DS20 -> DSACK, reads"), (1, "DS20 -> DSACK, writes"), (2, "AS20 -> STERM"))


def _read(path):
    data = config.read_json(Path(path), None)
    return data if isinstance(data, dict) else {}


def bus_cycles(rundir, info=None):
    """{"columns": [...], "rows": [...]} from timing/histograms.json (or timing.json), or None.
    Measurements only: median, min..max and sample count per region and preset."""
    from tfdiag import amiga
    rundir = Path(rundir)
    tm = _read(rundir / "timing" / "timing.json")
    hj = _read(rundir / "timing" / "histograms.json")
    hist = hj.get("histograms") or tm.get("histograms") or {}
    fab = hj.get("fabric_mhz") or tm.get("fabric_mhz") or {}
    try:
        ids = sorted(int(p) for p in hist)
    except ValueError:
        return None
    if not ids:
        return None
    names = {p["id"]: p["name"] for p in presets(info)}
    columns = [{"preset": p, "name": names.get(p, f"preset {p}"), "fabric_mhz": fab.get(str(p))} for p in ids]
    rows = []
    for reg in amiga.REGION_ORDER:
        title, chips, _ = amiga.REGIONS[reg]
        cells = []
        for p in ids:
            v = (hist.get(str(p)) or {}).get(reg)
            if not isinstance(v, dict) or not v.get("bins"):
                cells.append({"seen": False})
                continue
            meas = []
            for m, label in BUS_MEAS:
                sm = amiga.hist_summary(m, v["bins"][m], v.get("shift", 0), fab.get(str(p)))
                meas.append({"label": label, "n": sm["n"], "median": sm.get("median"), "min": sm.get("min"),
                             "max": sm.get("max"), "open": sm.get("open")} if sm["n"] else None)
            cycles = v.get("as", 0)
            cells.append({"seen": bool(cycles) or any(meas), "cycles": cycles, "meas": meas,
                          "held": v.get("held"), "lost": v.get("lost", 0)})
        rows.append({"region": reg, "title": title, "chips": chips, "cells": cells})
    return {"columns": columns, "rows": rows, "measurements": [label for _, label in BUS_MEAS]}


def waves_by_region(rundir):
    """{region: [{"preset", "svg", "trigger"}]} for the captures that have an SVG, preset order."""
    from tfdiag import amiga
    rundir = Path(rundir)
    out = {}
    for w in _read(rundir / "timing" / "timing.json").get("waves") or []:
        if not isinstance(w, dict) or not str(w.get("file", "")).endswith(".vcd"):
            continue
        svg = "timing/" + w["file"][:-4] + ".svg"
        if viewable(rundir, svg) is None:
            continue
        code, kind = w.get("region"), w.get("kind")
        reg = ("ramsey" if kind == 4 else amiga.REGION_BY_CODE.get(code) if isinstance(code, int)
               else code if code in amiga.REGIONS else None)
        if reg is None:
            continue
        out.setdefault(reg, []).append({"preset": w.get("preset"), "svg": svg, "trigger": w.get("trigger", "")})
    for caps in out.values():
        caps.sort(key=lambda c: (c["preset"] is None, c["preset"]))
    return out


def clock_checks(rundir):
    """{"clocks": [...], "fabric": {preset: MHz}, "switches": [...]} from timing.json."""
    tm = _read(Path(rundir) / "timing" / "timing.json")
    clocks = [c for c in (tm.get("clocks") or {}).get("clocks") or [] if isinstance(c, dict)]
    fabric = {int(p): v for p, v in (tm.get("fabric_mhz") or {}).items() if str(p).isdigit() and v}
    switches = [s for s in tm.get("clock_switches") or [] if isinstance(s, dict)]
    if not clocks and not fabric and not switches:
        return None
    return {"clocks": clocks, "fabric": dict(sorted(fabric.items())), "switches": switches}


def board_findings(rundir, limit=5):
    """{"count", "first": [...]} from tfdiag's boardview findings (what the board view shows)."""
    try:
        from tfdiag import boardview, report
        found = boardview.findings(report.collect(Path(rundir)))
    except Exception as error:          # the page must still render
        return {"count": 0, "first": [], "error": str(error)}
    found = [f for f in found if f.get("kind") != "note"]
    return {"count": len(found), "first": [{"title": f.get("title", ""), "detail": f.get("detail", ""),
                                            "where": f.get("where", "")} for f in found[:limit]]}


def firmware_on_board(rundir):
    """{"fpga": text, "cpld": text} from before.json's firmware ids (tfdiag fwid), or None."""
    from tfdiag import fwid
    fw = _read(Path(rundir) / "before.json").get("firmware")
    if not isinstance(fw, dict):
        return None
    fpga, cpld = fwid.summary(fw)
    return {"fpga": fpga, "cpld": cpld}


def expansion_boards(rundir):
    """{"boards": [text], "end": text, "complete": bool} from timing.json, or None."""
    from tfdiag import zorro
    zb = _read(Path(rundir) / "timing" / "timing.json").get("expansion_boards")
    if not isinstance(zb, dict):
        return None
    end = zb.get("end")
    return {"boards": [zorro.describe(b) for b in zb.get("boards") or []],
            "end": end["reason_text"] if end else "the walk did not finish", "complete": bool(end)}


def page_summary(rundir, info=None):
    """Everything the result page shows above the report; each part fails on its own."""
    out = {}
    for key, part in (("bus", lambda: bus_cycles(rundir, info)), ("waves", lambda: waves_by_region(rundir)),
                      ("clocks", lambda: clock_checks(rundir)), ("findings", lambda: board_findings(rundir)),
                      ("firmware", lambda: firmware_on_board(rundir)),
                      ("boards", lambda: expansion_boards(rundir))):
        try:
            out[key] = part()
        except Exception as error:      # odd or partial results: show the rest
            out[key] = None
            out[key + "_error"] = f"{type(error).__name__}: {error}"
    return out


def find_zip(rundir):
    found = sorted(rundir.glob("tf4060-diag-*.zip")) if rundir.is_dir() else []
    return found[0] if found else None


def zip_contents(path):
    with zipfile.ZipFile(path) as archive:
        return [(info.filename, info.file_size) for info in archive.infolist()]


def prune_runs(keep=KEEP_RUNS):
    if not runs_dir().is_dir():
        return
    current_job = (current() or {}).get("job")
    runs = sorted(p for p in runs_dir().iterdir() if p.is_dir())
    for old in runs[:-keep]:
        if old.name != current_job:
            shutil.rmtree(old, ignore_errors=True)


# --- the job ---------------------------------------------------------------------

def _locks(stack):
    """tfpi's job lock for the whole job. tfdiag takes the flash lock itself, so
    here it is only checked: a flash or update running means no start."""
    diag_dir().mkdir(parents=True, exist_ok=True)
    job_lock = stack.enter_context(open(lock_file(), "w"))
    try:
        fcntl.flock(job_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        raise DiagError("another diagnostic test or restore is running") from error
    with open(config.LOCK_FILE, "a") as flash_lock:
        try:
            fcntl.flock(flash_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise DiagError("a flash or a software update is running") from error
        fcntl.flock(flash_lock, fcntl.LOCK_UN)


def finish(result_path, outcome):
    if result_path:
        config.write_json(Path(result_path), outcome)


def contact_request(job):
    """Handle/note the web page left for this job (JOB_DIR/<job>.contact.json)."""
    path = config.JOB_DIR / f"{job}.contact.json"
    data = config.read_json(path, {})
    path.unlink(missing_ok=True)
    return data if isinstance(data, dict) else {}


def summary(outcome):
    restored = "restored OK" if outcome.get("restored") else "NOT restored"
    if outcome.get("aborted"):
        return f"Capture stopped early (aborted) - download the zip and send it to us; {restored}"
    if not outcome.get("complete"):
        return f"Capture incomplete - download the zip and send it to us; {restored}"
    return f"{DONE_MESSAGE}; {restored}"


def run(job, actions, preset_ids=None, test_mask=None, result_path=None):
    outcome = {"ok": False, "kind": "diag", "job": job, "result": None, "restored": None,
               "complete": False, "steps": {}, "aborted": False}
    asked = contact_request(job)
    with ExitStack() as stack:
        try:
            _locks(stack)
            if marker_file().exists() or current_file().exists():
                raise DiagError("an earlier test was not restored; run the restore check first")
            status = images_status()
            if not status["ok"]:
                raise DiagError(status["reason"])
        except DiagError as error:
            say(f"not started: {error}")
            outcome["result"] = f"not started: {error}"
            outcome["job"] = None                     # nothing to show on a result page
            finish(result_path, outcome)
            return 2

        rundir = runs_dir() / job
        rundir.mkdir(parents=True, exist_ok=True)
        info = status["info"]
        if not preset_ids:                            # default: every preset
            preset_ids = [p["id"] for p in presets(info)]
        config.write_json(rundir / "run.json", {
            "job": job, "date": time.strftime("%Y%m%d"), "started": config.now(),
            "images_build": info.get("build"), "actions": list(actions), "presets": preset_ids,
            "tests": test_mask})
        write_contact(rundir, asked.get("handle"), asked.get("note"))
        config.write_json(current_file(), {"job": job, "rundir": str(rundir), "pid": os.getpid(),
                                           "started": config.now()})
        os.sync()
        runner = Runner(rundir)
        say(f"diagnostics {job}: {', '.join(ACTION_NAMES[a] for a in actions)}, then restore and check")
        say(WARNING_FIRST_STEP)
        args = ["run", "--images", images_dir(), "--out", rundir]
        if "board" not in actions:
            args.append("--no-board")
        if "timing" not in actions:
            args.append("--no-timing")
        else:
            args += ["--presets", ",".join(str(p) for p in preset_ids)]
            if test_mask is not None:
                args += ["--tests", f"0x{test_mask:08x}"]
            if info.get("build"):
                args += ["--expect-build", info["build"]]
        (rundir / "restore.json").unlink(missing_ok=True)
        rc, done = runner.tool(*args)
        runner.protect = True
        outcome["aborted"] = runner.aborted
        outcome["steps"] = step_status(runner.results, actions)
        if rc >= 2 and not runner.results:
            outcome["steps"] = {a: {"state": "not run", "findings": 0} for a in actions}
        outcome["findings"] = sum(s["findings"] for s in outcome["steps"].values())
        outcome["complete"] = (rc in (0, 1) and not runner.aborted and
                               all(s["state"] == "captured" for s in outcome["steps"].values()))
        restore = restore_outcome(rundir / "restore.json", rc)
        outcome["restored"] = bool(restore.get("ok"))
        outcome["restore"] = restore
        if outcome["restored"]:
            say("Restored: OK")
        else:
            say("Restored: NOT OK: " + "; ".join(restore["differences"]))
            if restore["advice"]:
                say(restore["advice"])
            say("What to do: " + WHAT_TO_DO)
        render_waves(rundir)
        say("building the download")
        try:
            outcome["zip"] = build_zip(rundir, [config.JOB_DIR / f"{job}.log"]).name
        except OSError as error:
            say(f"could not build the download: {error}")
        outcome["ok"] = outcome["restored"] and outcome["complete"]
        outcome["result"] = summary(outcome)
        config.write_json(rundir / "outcome.json", outcome)
        current_file().unlink(missing_ok=True)      # the marker is tfdiag's
        os.sync()
        prune_runs()
    # After the lock is released: "done" on the page means free for the next job.
    finish(result_path, outcome)
    say(outcome["result"])
    return 0 if outcome["ok"] else 1


# --- Verify (main page): which firmware is on the board -------------------------------------

VERIFY_WARNING = ("Verify reads the FPGA's flash and the CPLD back from the chips. a314d is stopped "
                  "and the Amiga is held in reset while it reads (about half a minute), then restarted: "
                  "save your work first. Nothing is written.")


def verify_file():
    return config.STATE_DIR / "verify.json"


def last_verify():
    data = config.read_json(verify_file(), None)
    return data if isinstance(data, dict) else None


def verify(job, result_path=None):
    """tfdiag identify, under the same locks and power-loss marker as a diagnostic."""
    outcome = {"ok": False, "kind": "verify", "job": None, "result": None}
    with ExitStack() as stack:
        try:
            _locks(stack)
            if marker_file().exists() or current_file().exists():
                raise DiagError("an earlier test was not restored; run the restore check on the Diagnostics tab first")
            status = images_status()
            if not status["ok"]:
                raise DiagError(status["reason"])
        except DiagError as error:
            say(f"not started: {error}")
            outcome["result"] = f"not started: {error}"
            finish(result_path, outcome)
            return 2
        rundir = config.STATE_DIR / "verify" / job
        rundir.mkdir(parents=True, exist_ok=True)
        config.write_json(current_file(), {"job": job, "rundir": str(rundir), "pid": os.getpid(),
                                           "started": config.now(), "kind": "verify"})
        os.sync()
        runner = Runner(rundir)
        say("reading the installed firmware back from the board")
        rc, _ = runner.tool("identify", "--images", images_dir(), "--out", rundir)
        runner.protect = True
        fw = config.read_json(rundir / "firmware.json", None)
        current_file().unlink(missing_ok=True)
        if not isinstance(fw, dict):
            outcome["result"] = f"the firmware could not be read back (tfdiag exit status {rc})"
        else:
            verdict = fw.get("verdict") or {}
            record = {"time": fw.get("time") or config.now(), "job": job, "verdict": verdict,
                      "restore_ok": bool(fw.get("restore_ok")), "advice": fw.get("restore_advice")}
            config.write_json(verify_file(), record)
            for chip, label in (("fpga", "RAM FPGA"), ("cpld", "BUS CPLD")):
                say(f"{label}: {(verdict.get(chip) or {}).get('detail', 'not read')}")
            if record["restore_ok"]:
                say("Restored: OK")
            else:
                say("Restored: NOT OK" + (f": {record['advice']}" if record["advice"] else ""))
                say("What to do: " + WHAT_TO_DO)
            states = [(verdict.get(c) or {}).get("state") for c in ("fpga", "cpld")]
            outcome["ok"] = record["restore_ok"] and "not read" not in states
            outcome["result"] = ("; ".join(f"{n}: {s}" for n, s in zip(("RAM FPGA", "BUS CPLD"), states))
                                 + ("" if record["restore_ok"] else "; NOT restored"))
            outcome["verdict"] = verdict
        prune_verify()
        os.sync()
    finish(result_path, outcome)
    say(outcome["result"])
    return 0 if outcome["ok"] else 1


def prune_verify(keep=5):
    d = config.STATE_DIR / "verify"
    runs = sorted((p for p in d.iterdir() if p.is_dir()), key=lambda p: p.name) if d.is_dir() else []
    for old in runs[:-keep]:
        shutil.rmtree(old, ignore_errors=True)


def recover(result_path=None, job=None):
    """After a power loss, or a job that died: restore + check, keep partial results."""
    record_ = current() or {}
    if not marker_file().exists() and not record_:
        say("nothing to recover")
        return 0
    with ExitStack() as stack:
        try:
            _locks(stack)
        except DiagError as error:
            say(f"not recovering: {error}")
            return 2
        old_job = record_.get("job")
        rundir = Path(record_["rundir"]) if record_.get("rundir") else runs_dir() / (
            old_job or "recovered-" + time.strftime("%Y%m%d-%H%M%S"))
        rundir.mkdir(parents=True, exist_ok=True)
        say(f"recovering diagnostic run {old_job or '(unknown)'}"
            + (f", started {record_['started']}" if record_.get("started") else ""))
        runner = Runner(rundir)
        runner.protect = True
        if marker_file().exists():
            before = marker_before() or (rundir / "before.json" if (rundir / "before.json").is_file() else None)
            (rundir / "restore.json").unlink(missing_ok=True)
            rc, _ = runner.tool("recover", *(["--before", before] if before else []),
                                "--out", rundir / "restore.json")
            restore = restore_outcome(rundir / "restore.json", rc)
            if marker_file().exists() and restore.get("ok"):
                restore["ok"] = False
                restore["differences"].append("tfdiag reported OK but left its marker")
        elif (rundir / "restore.json").exists():
            restore = restore_outcome(rundir / "restore.json", 0)       # restored before the power cut
        else:
            # No marker: tfdiag had not started touching the hardware, and the
            # power cycle reloaded the normal firmware anyway.
            restore = {"ok": True, "differences": [], "advice": "", "note": "interrupted before any hardware step"}
        logs = [config.JOB_DIR / f"{j}.log" for j in (old_job, job) if j]
        record = {"time": config.now(), "job": old_job, "ok": bool(restore.get("ok")),
                  "differences": restore["differences"], "advice": restore.get("advice"), "zip": None,
                  "rundir": rundir.name}
        try:
            record["zip"] = build_zip(rundir, logs).name
        except OSError as error:
            say(f"could not build the download: {error}")
        outcome = {"ok": False, "kind": "diag", "job": old_job, "interrupted": True, "complete": False,
                   "restored": record["ok"], "restore": restore, "zip": record["zip"], "steps": {},
                   "result": "Capture interrupted - download the partial zip and send it to us; "
                             + ("recovered OK" if record["ok"] else "NOT restored")}
        config.write_json(rundir / "outcome.json", outcome)
        if old_job and not (config.JOB_DIR / f"{old_job}.json").exists():
            finish(config.JOB_DIR / f"{old_job}.json", outcome)
        config.write_json(recovery_file(), record)
        current_file().unlink(missing_ok=True)
        os.sync()
    if job:
        finish(result_path, dict(outcome, kind="diag-recover"))
    say("Recovered: OK" if record["ok"] else "Recovery: NOT OK: " + "; ".join(record["differences"]))
    return 0 if record["ok"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="tfpi.diag")
    sub = parser.add_subparsers(dest="cmd", required=True)
    runner = sub.add_parser("run")
    runner.add_argument("--job", required=True)
    runner.add_argument("--actions", default="board,timing")
    runner.add_argument("--presets", default="", help="comma-separated CLKSEL presets; empty = all")
    runner.add_argument("--tests", default="", help="test mask; empty = all")
    runner.add_argument("--result")
    rec = sub.add_parser("recover")
    rec.add_argument("--job")
    rec.add_argument("--result")
    ver = sub.add_parser("verify")
    ver.add_argument("--job", required=True)
    ver.add_argument("--result")
    sub.add_parser("status")
    args = parser.parse_args(argv)
    if args.cmd == "status":
        print(json.dumps({"images": images_status(), "running": running(), "current": current(),
                          "marker": marker_file().exists(), "recovery": recovery()}, indent=2,
                         default=str))
        return 0
    if args.cmd == "recover":
        return recover(args.result, args.job)
    if args.cmd == "verify":
        return verify(args.job, args.result)
    actions = [a for a in args.actions.split(",") if a]
    if not actions or any(a not in ACTIONS for a in actions):
        parser.error(f"--actions: choose from {', '.join(ACTIONS)}")
    preset_ids = [int(p) for p in args.presets.split(",") if p] or None
    test_mask = int(args.tests, 0) if args.tests else None
    return run(args.job, actions, preset_ids, test_mask, args.result)


if __name__ == "__main__":
    sys.exit(main())
