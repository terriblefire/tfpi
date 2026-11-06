"""Safety sequence around every diagnostic.

snapshot: read the "before" state, write the marker (fsync), stop a314d, read the
          SPI flash id and checksum, boot the flash image again.
restore:  JTAG test-logic reset, re-read flash id/checksum and CPLD ids,
          fomu-flash -r, setgpio.sh, wait for CDONE, start a314d if it was
          running, compare with "before"; the marker is deleted only if it matches.
recover:  restore at boot (systemd) or after a power loss, from the marker.
run:      snapshot + board + timing + restore in one process, restore guaranteed by
          try/finally and SIGTERM/SIGINT/SIGHUP handlers.

Ownership: the marker (cfg.marker, default /var/lib/tfpi/diag-in-progress) is
written by `snapshot` (and `run`) and removed only by a restore/recover whose
check passes. The tfpi tab only calls the CLI.
"""

import fcntl
import json
import os
import signal
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from . import fpga as F
from . import jtag as J
from . import tools
from .hooks import checkpoint

MARKER_VERSION = 1


class Interrupted(BaseException):
    """A signal asked us to stop: unwinds through every finally."""


class LockError(RuntimeError):
    pass


class MarkerExists(RuntimeError):
    """A previous diagnostic was not restored: its baseline must not be overwritten."""


# ---- durable files ------------------------------------------------------------------
def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as fh:
        json.dump(data, fh, indent=1, sort_keys=True, default=str)
        fh.flush()
        os.fsync(fh.fileno())
        tmp = Path(fh.name)
    tmp.replace(path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def remove_durable(path):
    path = Path(path)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def locks(cfg):
    handles = []
    try:
        for p, what in ((cfg.lock_file, "another diagnostic is running"),
                        (cfg.flash_lock, "a firmware flash is running")):
            Path(p).parent.mkdir(parents=True, exist_ok=True)
            h = open(p, "a")
            try:
                fcntl.flock(h, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as e:
                h.close()
                raise LockError(what) from e
            handles.append(h)
        yield
    finally:
        for h in handles:
            h.close()


@contextmanager
def signals_raise():
    """SIGTERM/SIGINT/SIGHUP raise Interrupted (once); restore code runs with them ignored."""
    old = {}

    def handler(sig, frame):
        for s in old:
            signal.signal(s, signal.SIG_IGN)
        raise Interrupted(signal.Signals(sig).name)

    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            old[s] = signal.signal(s, handler)
        except (ValueError, OSError):
            pass
    try:
        yield
    finally:
        for s, h in old.items():
            try:
                signal.signal(s, h)
            except (ValueError, OSError):
                pass


@contextmanager
def signals_ignored():
    old = {}
    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            old[s] = signal.signal(s, signal.SIG_IGN)
        except (ValueError, OSError):
            pass
    try:
        yield
    finally:
        for s, h in old.items():
            try:
                signal.signal(s, h)
            except (ValueError, OSError):
                pass


# ---- hardware access, overridable for tests ------------------------------------------
class Hw:
    """Factories for everything that touches the board."""

    def __init__(self, cfg, events, link_factory=None, spi_factory=None):
        self.cfg = cfg
        self.ev = events
        self.gpio = tools.Gpio(cfg)
        self.a314 = tools.Service(cfg)
        self.fpga = F.Fpga(cfg, events, self.gpio)
        self._link_factory = link_factory or (lambda: J.OpenocdLink(cfg, events))
        self._spi_factory = spi_factory
        from .timing import Clock
        self.clock = Clock()

    @contextmanager
    def cpld(self):
        link = self._link_factory()
        try:
            yield J.Cpld(link, events=self.ev)
        finally:
            link.close()

    @contextmanager
    def spi(self):
        from . import spi as S
        dev = self._spi_factory() if self._spi_factory else S.open_device(self.cfg)
        s = S.Spi(dev)
        try:
            yield s
        finally:
            s.close()

    def cpld_ids(self):
        try:
            with self.cpld() as c:
                return c.ids()
        except J.JtagError as e:
            self.ev.warn(f"JTAG: {e}")
            return None, None

    def tlr(self):
        try:
            with self.cpld() as c:
                c.tlr()
                return True
        except J.JtagError as e:
            self.ev.warn(f"JTAG TLR: {e}")
            return False

    def present(self):
        """Is the board powered? CDONE high, or the CPLD answers on JTAG."""
        if self.gpio.cdone():
            return True
        return self.cpld_ids()[0] is not None

    def wait_present(self, seconds, sleep=time.sleep):
        deadline = time.monotonic() + seconds
        warned = False
        while True:
            if self.present():
                return True
            if time.monotonic() >= deadline:
                return False
            if not warned:
                self.ev.warn("the board is not answering (powered off?): waiting for it")
                warned = True
            sleep(2.0)


# ---- before / after ---------------------------------------------------------------------
def read_flash(hw):
    jedec = hw.fpga.jedec_id()
    ck = hw.fpga.flash_checksum() if jedec else None
    return {"flash_jedec": jedec, "flash_sha256": ck and ck["sha256"], "flash_bytes": ck and ck["bytes"],
            "flash_bitstream": ck and ck["bitstream"]}


def load_held(hw, images, usercode=None, ev=None):
    """Load an image that keeps the Amiga in reset: diag_timing.bin + 0xA1 at once when the
    CPLD design is one the timing image supports, else diag_pins.bin (it holds RESET low by
    itself and works with any CPLD). Returns the image name, or None if neither is there (the
    caller then boots the flash image)."""
    from . import manifest
    if not images:
        return None
    m = manifest.load(images)
    shas = m.get("images") or {}
    timing_ok = manifest.cpld_compatible(m, usercode)[0]
    for name in (["diag_timing.bin"] if timing_ok else []) + ["diag_pins.bin"]:
        p = Path(images) / name
        if not p.exists():
            continue
        if name == "diag_pins.bin":
            hw.fpga.load_image(p, shas.get(name))
        else:
            with hw.spi() as spi:
                hw.fpga.load_image(p, shas.get(name), hold=spi.reset_assert)
        return name
    return None


def snapshot(cfg, hw, ev, out=None, images=None):
    """Baseline + marker. Leaves a314d stopped and diag_timing.bin loaded with the Amiga held
    in reset (or, without the image, the flash image running).

    Order (AmigaOS must not run between diagnostic steps): read-only JTAG ids and a314d state,
    marker, stop a314d, load diag_timing.bin + 0xA1 (the one unavoidable Amiga reset), then
    the flash id/checksum (fomu-flash holds the FPGA in reset while it reads), then
    diag_timing.bin + 0xA1 again before anything else."""
    ev.step("snapshot", "recording the state before the test")
    if read_json(cfg.marker) is not None:
        raise MarkerExists("a previous diagnostic has not been restored yet (marker present): "
                           "run `tfdiag restore` or `tfdiag recover` first")
    before = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "a314d_active": hw.a314.active(),
              "serial": pi_serial()}
    idc, uc = hw.cpld_ids()
    before.update(cpld_idcode=idc, cpld_usercode=uc, cdone=hw.gpio.cdone())
    marker = {"version": MARKER_VERSION, "pid": os.getpid(), "started": before["time"],
              "before": before, "before_file": str(out) if out else None, "stage": "snapshot"}
    write_json(cfg.marker, marker)
    checkpoint("snapshot.marker")
    ev.progress(20, "marker written; stopping a314d")
    if before["a314d_active"]:
        hw.a314.stop(ev)
    checkpoint("snapshot.a314d_stopped")
    images = images or cfg.images
    held = None
    try:
        held = load_held(hw, images, uc, ev)
    except (F.FpgaError, tools.ToolError) as e:
        ev.warn(f"holding the Amiga in reset: {e}")
    if held:
        ev.progress(30, f"{held} loaded, Amiga held in reset")
        checkpoint("snapshot.held")
    ev.progress(40, "reading the SPI flash id and checksum (read only)")
    before.update(read_flash(hw))
    checkpoint("snapshot.flash_read")
    if held:
        ev.progress(80, f"{held} again, Amiga held in reset")
        held = load_held(hw, images, uc, ev)
        before["cdone_after_snapshot"] = hw.gpio.cdone()
    if not held:
        ev.warn("no diag_timing.bin or diag_pins.bin: booting the flash image again (the Amiga restarts)")
        r = hw.fpga.restore()
        before["cdone_after_snapshot"] = r["cdone"]
    before["held_after_snapshot"] = held
    marker["before"] = before
    marker["stage"] = "snapshot-done"
    write_json(cfg.marker, marker)
    checkpoint("snapshot.done")
    if out:
        write_json(out, before)
    missing = [k for k in ("flash_jedec", "flash_sha256", "cpld_idcode") if not before.get(k)]
    for k in missing:
        ev.result(f"before_{k}", "incomplete", f"could not read {k} before the test", step="snapshot")
    if not missing:
        ev.result("snapshot", "info", f"flash {before['flash_jedec']} sha256 {before['flash_sha256'][:12]}, "
                  f"CPLD {before['cpld_idcode']} usercode {before['cpld_usercode']}", step="snapshot")
    return before


def set_stage(cfg, stage):
    m = read_json(cfg.marker)
    if m:
        m["stage"] = stage
        write_json(cfg.marker, m)


def compare(before, after):
    diffs = []
    for key, label in (("flash_jedec", "SPI flash JEDEC id"), ("flash_sha256", "SPI flash image checksum"),
                       ("cpld_idcode", "CPLD IDCODE"), ("cpld_usercode", "CPLD usercode")):
        b, a = before.get(key), after.get(key)
        if b is None:
            continue
        if a != b:
            diffs.append(f"{label}: before {b}, after {a}")
    if not after.get("cdone"):
        diffs.append("CDONE low: the FPGA did not configure from the SPI flash")
    if before.get("a314d_active") and after.get("a314d_active") is False:
        diffs.append("a314d is not running")
    return diffs


def advice_for(diffs):
    if not diffs:
        return "Everything is as it was before the test."
    a = []
    t = " ".join(diffs)
    if "not answering" in t:
        a.append("The board did not come back: power the Amiga on, then the Pi restores it on its own "
                 "(or press Restore on the Diagnostics tab).")
    if "checksum" in t or "JEDEC" in t:
        a.append("The SPI flash reads differently from before. tfdiag never writes it; read it again "
                 "after a power cycle, and if it still differs re-flash the release from the Firmware tab.")
    if "CDONE" in t:
        a.append("The FPGA did not start from its flash: power-cycle the Amiga. If it stays down, "
                 "re-flash the release from the Firmware tab.")
    if "CPLD" in t:
        a.append("The CPLD answers differently: power-cycle the Amiga and run the snapshot again. "
                 "The CPLD flash is never written by the diagnostics.")
    if "a314d" in t:
        a.append("Start a314d again: sudo systemctl start a314d (or reboot the Pi).")
    return " ".join(a)


def restore(cfg, hw, ev, before=None, out=None, recovering=False, sleep=time.sleep):
    """Bring the board back and check it. Never raises for board problems."""
    with signals_ignored():
        ev.step("restore", "restoring the normal firmware")
        marker = read_json(cfg.marker)
        if before is None and marker:
            before = marker.get("before")
        before = before or {}
        res = {"ok": False, "differences": [], "advice": "", "before": before, "after": {},
               "recovering": recovering, "marker": bool(marker)}
        if not hw.wait_present(cfg.board_wait, sleep):
            res["differences"] = ["the board is not answering (no CDONE, no CPLD IDCODE)"]
            res["advice"] = advice_for(res["differences"])
            ev.result("restore", "fail", res["advice"], step="restore")
            if out:
                write_json(out, res)
            return res
        ev.progress(10, "JTAG test-logic reset")
        res["tlr"] = hw.tlr()
        checkpoint("restore.tlr")
        after = {}
        idc, uc = hw.cpld_ids()
        after.update(cpld_idcode=idc, cpld_usercode=uc)
        ev.progress(30, "re-reading the SPI flash (read only)")
        try:
            after.update(read_flash(hw))
        except (tools.ToolError, F.FpgaError) as e:
            ev.warn(f"flash read: {e}")
        checkpoint("restore.flash_read")
        ev.progress(60, "fomu-flash -r, setgpio.sh, CDONE")
        try:
            r = hw.fpga.restore()
        except tools.ToolError as e:
            ev.error(f"FPGA restore: {e}")
            r = {"cdone": hw.gpio.cdone()}
        after["cdone"] = r["cdone"]
        checkpoint("restore.fpga")
        if before.get("a314d_active"):
            ev.progress(80, "starting a314d")
            if recovering:
                # boot-time recovery runs Before=a314d: queue the start; it runs once we exit
                hw.a314.start(ev, wait=False)
            else:
                hw.a314.start(ev)
        after["a314d_active"] = None if recovering else hw.a314.active()
        checkpoint("restore.a314d")
        res["after"] = after
        res["differences"] = compare(before, after)
        res["ok"] = not res["differences"] and bool(before)
        if not before:
            res["differences"].append("no 'before' record: nothing to compare against")
        res["advice"] = advice_for(res["differences"]) if before else \
            "No baseline was recorded; the normal firmware was booted from the SPI flash."
        if res["ok"]:
            remove_durable(cfg.marker)
            ev.result("restore", "pass", "normal firmware running; flash, CPLD and a314d as before",
                      step="restore")
        else:
            ev.result("restore", "fail", "; ".join(res["differences"]) + ". " + res["advice"], step="restore")
        if out:
            write_json(out, res)
        return res


def recover(cfg, hw, ev, before=None, out=None, sleep=time.sleep):
    marker = read_json(cfg.marker)
    if before is None and marker is None:
        ev.info("no diagnostic in progress: nothing to recover")
        res = {"ok": True, "differences": [], "advice": "Nothing to do.", "nothing_to_do": True}
        if out:
            write_json(out, res)
        return res
    if marker and marker.get("before") and before is None:
        before = marker["before"]
    return restore(cfg, hw, ev, before=before, out=out, recovering=True, sleep=sleep)


def pi_serial():
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("Serial"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return None
