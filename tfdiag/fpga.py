"""RAM FPGA: load a diag image into FPGA SRAM, bring the flash image back, and
read-only checks of the SPI flash.

NEVER WRITES FLASH. fomu-flash is only ever run with the options in ALLOWED
(-f load SRAM, -r reset/boot from flash, -i JEDEC id, -b/-s read). Every call
goes through fomu(), which refuses anything else; tests/test_forbidden.py greps
the package for flash-writing calls.
"""

import hashlib
import os
import re
import tempfile
import time
from pathlib import Path

from . import tools

ALLOWED = {"-f", "-r", "-i", "-b", "-s", "-q"}
# options that take a value
_VALUED = {"-f", "-b", "-s"}
ICE40_PREAMBLE = bytes.fromhex("7EAA997E")


class FpgaError(RuntimeError):
    pass


def check_args(args):
    """Raise unless every option is in ALLOWED (values of -f/-b/-s skipped)."""
    args = [str(a) for a in args]
    i = 0
    while i < len(args):
        a = args[i]
        if not a.startswith("-") or a not in ALLOWED:
            raise FpgaError(f"refusing fomu-flash option {a!r}: tfdiag never writes the SPI flash")
        i += 2 if a in _VALUED else 1
    if "-s" in args and "-b" not in args:
        raise FpgaError("fomu-flash -s needs -b (read length)")


def parse_jedec(text):
    """`fomu-flash -i` output -> 'bf2641' style string or None (same patterns as tfpi)."""
    def field(pattern):
        m = re.search(pattern, text, re.M)
        return int(m.group(1), 16) if m else None
    maker = field(r"^!! JEDEC Manufacturer ID:\s*([0-9a-fA-F]{2})")
    if maker is None:
        maker = field(r"^Manufacturer ID:.*\(([0-9a-fA-F]{2})\)")
    kind = field(r"^Memory model:.*\(([0-9a-fA-F]{2})\)")
    size = field(r"^Memory size:.*\(([0-9a-fA-F]{2})\)")
    if None in (maker, kind, size) or maker in (0x00, 0xFF):
        return None
    return f"{maker:02x}{kind:02x}{size:02x}"


class Fpga:
    def __init__(self, cfg, events=None, gpio=None):
        self.cfg = cfg
        self.events = events
        self.gpio = gpio or tools.Gpio(cfg)

    def fomu(self, *args, timeout=None):
        check_args(args)
        return tools.run([self.cfg.fomu_flash, *args], timeout=timeout or self.cfg.tool_timeout,
                         events=self.events)

    # ---- read-only flash checks -----------------------------------------------
    def jedec_id(self):
        rc, out = self.fomu("-i")
        return parse_jedec(out) if rc == 0 else None

    def flash_checksum(self, length=None):
        """sha256 of the first `length` bytes of the SPI flash, plus a look at the image."""
        length = int(length or self.cfg.flash_check_bytes)
        fd, tmp = tempfile.mkstemp(prefix="tfdiag-flash-", dir=_tmpdir(self.cfg))
        os.close(fd)
        try:
            rc, _ = self.fomu("-b", length, "-s", tmp, timeout=max(self.cfg.tool_timeout, 300))
            data = Path(tmp).read_bytes() if Path(tmp).exists() else b""
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        if rc or len(data) < length:
            return None
        return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": length,
                "bitstream": ICE40_PREAMBLE in data[:64]}

    # ---- configuration ------------------------------------------------------
    def load_image(self, path, expect_sha256=None, hold=None):
        """Load a bitstream into FPGA SRAM (the SPI flash is not touched). hold: called as
        soon as the SPI pins are back (0xA1 for diag_timing.bin), so the Amiga is released
        only for the reconfiguration itself, and again once CDONE is high."""
        path = Path(path)
        data = path.read_bytes()
        if ICE40_PREAMBLE not in data[:64]:
            raise FpgaError(f"{path.name} is not an iCE40 bitstream")
        if expect_sha256 and hashlib.sha256(data).hexdigest() != expect_sha256:
            raise FpgaError(f"{path.name}: sha256 does not match diag_manifest.json")
        rc, _ = self.fomu("-f", path)
        if rc:
            raise FpgaError(f"fomu-flash -f {path.name} failed (status {rc})")
        self.gpio.setgpio(self.events)
        if hold:
            hold()
        if not self.gpio.wait_cdone():
            raise FpgaError(f"{path.name} loaded but CDONE stayed low")
        if hold:
            hold()
        return True

    def restore(self):
        """Boot the normal image from the SPI flash, hand the pins back, wait for CDONE."""
        rc, _ = self.fomu("-r")
        self.gpio.setgpio(self.events)
        ok = self.gpio.wait_cdone()
        return {"fomu_rc": rc, "cdone": bool(ok)}


def _tmpdir(cfg):
    d = Path(cfg.state_dir) / "diag-tmp"
    try:
        d.mkdir(parents=True, exist_ok=True)
        return str(d)
    except OSError:
        return None


def sleep(s):
    time.sleep(s)
