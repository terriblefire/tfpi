"""Shared test fixtures: temp dirs, stub hardware tools, synthetic releases."""

import importlib
import os
import struct
import sys
import tempfile
import textwrap
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tfpi import config  # noqa: E402
from tfpi.fwimage import PREAMBLE, SPIROM_OFFSET  # noqa: E402


def spi_image():
    image = bytearray(1536)
    image[510:512] = b"\x55\xaa"
    for index, kind in enumerate((0x68, 0x69)):
        entry = 446 + 16 * index
        image[entry + 4] = kind
        struct.pack_into("<II", image, entry + 8, index + 1, 1)
    image[1024:1032] = b"-rom1fs-"
    struct.pack_into(">I", image, 1032, 512)
    checksum = -sum(struct.unpack(">128I", image[1024:1536])) & 0xffffffff
    struct.pack_into(">I", image, 1036, checksum)
    return bytes(image)


def bitstream(tag=0):
    return PREAMBLE + bytes([tag]) * 300


def full_image(tag=0):
    raw = bitstream(tag)
    return raw + b"\xff" * (SPIROM_OFFSET - len(raw)) + spi_image()


def jed(tag=0):
    return (f"\x02QF186624*\nN DEVICE XC95288XL-10-TQ144*\nL0 {tag:08b}*\n\x030000").encode()


def make_zip(path, files):
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return Path(path)


STUBS = {
    "fomu-flash": r'''
        import os, sys, shutil
        flash = os.environ["FAKE_FLASH"]
        args = sys.argv[1:]
        if "-w" in args:
            data = open(args[args.index("-w") + 1], "rb").read()
            if os.environ.get("FAKE_FOMU_CORRUPT"):
                data = data[:-1] + bytes([data[-1] ^ 1])
            open(flash, "wb").write(data + b"\xff" * 4096)
            print("wrote", len(data))
        elif "-s" in args:
            shutil.copyfile(flash, args[args.index("-s") + 1])
        elif "-r" in args:
            print("reset")
    ''',
    "xc3sprog": r'''
        import os, sys
        open(os.environ["FAKE_XC3_LOG"], "a").write(" ".join(sys.argv[1:]) + "\n")
        if os.environ.get("FAKE_XC3_FAIL"):
            print("Verify: Failure at fuse 22/6"); sys.exit(1)
        print("JTAG chainpos: 0 Device IDCODE = 0x29618093 Desc: XC95288XL"); print("Verify: Success")
    ''',
    "pinctrl": r'''
        import os
        print("23: ip    pn | " + ("lo" if os.environ.get("FAKE_CDONE_LOW") else "hi") + " // GPIO23 = input")
    ''',
    "systemctl": r'''
        import os, sys
        state = os.environ["FAKE_A314"]
        cmd = sys.argv[1]
        open(state + ".log", "a").write(" ".join(sys.argv[1:]) + "\n")
        if cmd == "is-active":
            active = os.path.exists(state) and open(state).read() == "active"
            if "--quiet" not in sys.argv: print("active" if active else "inactive")
            sys.exit(0 if active else 3)
        if cmd in ("stop", "start"):
            open(state, "w").write("inactive" if cmd == "stop" else "active")
    ''',
}


class Sandbox(unittest.TestCase):
    """Temp etc/state/share dirs and stub tools, wired in via env + config reload."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        bindir = self.tmp / "bin"
        bindir.mkdir()
        for name, body in STUBS.items():
            path = bindir / name
            path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
            path.chmod(0o755)
        setgpio = self.tmp / "setgpio.sh"
        setgpio.write_text("true\n")
        self.env = {
            "TFPI_ETC": str(self.tmp / "etc"), "TFPI_STATE_DIR": str(self.tmp / "state"),
            "TFPI_SHARE": str(self.tmp / "share"),
            "TFPI_FOMU_FLASH": str(bindir / "fomu-flash"), "TFPI_XC3SPROG": str(bindir / "xc3sprog"),
            "TFPI_PINCTRL": str(bindir / "pinctrl"), "TFPI_SYSTEMCTL": str(bindir / "systemctl"),
            "TFPI_SETGPIO": str(setgpio),
            "FAKE_FLASH": str(self.tmp / "spiflash.bin"), "FAKE_XC3_LOG": str(self.tmp / "xc3.log"),
            "FAKE_A314": str(self.tmp / "a314"),
        }
        self._saved = {key: os.environ.get(key) for key in
                       list(self.env) + ["FAKE_FOMU_CORRUPT", "FAKE_XC3_FAIL", "FAKE_CDONE_LOW"]}
        os.environ.update(self.env)
        (self.tmp / "etc").mkdir()
        (self.tmp / "a314").write_text("active")
        importlib.reload(config)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        importlib.reload(config)
        self._tmp.cleanup()

    def cpld_flashes(self):
        log = self.tmp / "xc3.log"
        return len(log.read_text().splitlines()) if log.exists() else 0

    def systemctl_log(self):
        return (self.tmp / "a314.log").read_text()
