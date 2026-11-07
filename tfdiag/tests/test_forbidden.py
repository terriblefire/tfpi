"""tfdiag must never write the FPGA SPI flash or program the CPLD.

The one xc3sprog use is cpldread.py's fuse readback (filespec action ':r', read into a
new file); it is checked here both in the source and by what reaches the tool."""

import re
import unittest
from pathlib import Path

from tfdiag.tests import helpers  # noqa: F401  (sys.path)
from tfdiag import cpldread, fpga, protocol as P

PKG = Path(__file__).resolve().parents[1]
SOURCES = [p for p in PKG.rglob("*.py") if "tests" not in p.parts]

# (pattern, what, files allowed to contain it)
XC3SPROG_OK = {"cpldread.py", "config.py"}
FORBIDDEN = [
    (r"""["']-w["']""", "fomu-flash -w (write flash)", set()),
    (r"""["']-u["']""", "fomu-flash -u (unprotect)", set()),
    (r"""["']-e["']""", "fomu-flash -e / xc3sprog -e (erase)", set()),
    (r"""["']-l["']""", "xc3sprog -l (lock bits)", set()),
    (r"xc3sprog", "xc3sprog outside the readback", XC3SPROG_OK),
    (r"XC3SPROG", "xc3sprog outside the readback", XC3SPROG_OK),
    (r"\b(ISPEN|ISPENC|FERASE|FBULK|FPGM|FPGMI|FBLANK|FVFY|FVFYI|ISPEX)\b", "XC9500 ISP instruction", set()),
    (r"svf|xsvf|program_jed", "CPLD programming file", set()),
    (r"flashrom|mtd_debug|spiflash.*write", "flash writer", set()),
]


class Forbidden(unittest.TestCase):
    def test_sources_exist(self):
        names = {p.name for p in SOURCES}
        self.assertTrue({"fpga.py", "jtag.py", "session.py"} <= names)

    def test_no_forbidden_calls(self):
        hits = []
        for p in SOURCES:
            for n, line in enumerate(p.read_text().splitlines(), 1):
                code = line.split("#", 1)[0]
                for pat, what, ok in FORBIDDEN:
                    if p.name in ok:
                        continue
                    if re.search(pat, code, re.I if "svf" in pat else 0):
                        hits.append(f"{p.name}:{n}: {what}: {line.strip()}")
        self.assertEqual(hits, [])

    def test_fomu_allowed_set(self):
        self.assertEqual(fpga.ALLOWED, {"-f", "-r", "-i", "-b", "-s", "-q"})
        for bad in (["-w", "x.bin"], ["-u", "-w", "x.bin"], ["-e"], ["-v", "x.bin"], ["x.bin"], ["-k"]):
            with self.assertRaises(fpga.FpgaError, msg=bad):
                fpga.check_args(bad)
        for good in (["-f", "a.bin"], ["-r"], ["-i"], ["-b", 4096, "-s", "/tmp/x"]):
            fpga.check_args(good)
        with self.assertRaises(fpga.FpgaError):
            fpga.check_args(["-s", "/tmp/x"])     # a read without a length

    def test_xc3sprog_read_only(self):
        good = ["-c", "gpiod_creator", "-p", "0", "/tmp/tfdiag-none/x.jed:r"]
        self.assertEqual(cpldread.check_args(good, "gpiod_creator"), good)
        bad = [
            ["-c", "gpiod_creator", "-p", "0", "/tmp/tfdiag-none/x.jed"],          # default 'w'
            ["-c", "gpiod_creator", "-p", "0", "/tmp/tfdiag-none/x.jed:w"],
            ["-c", "gpiod_creator", "-p", "0", "/tmp/tfdiag-none/x.jed:W"],
            ["-c", "gpiod_creator", "-p", "0", "/tmp/tfdiag-none/x.jed:v"],
            ["-c", "gpiod_creator", "-p", "0", "/tmp/tfdiag-none/x.jed:R"],         # overwrite
            ["-c", "gpiod_creator", "-p", "0", "/tmp/x.jed:r:0:JED"],
            ["-c", "gpiod_creator", "-e", "-p", "0", "/tmp/tfdiag-none/x.jed:r"],
            ["-c", "gpiod_creator", "-p", "0", "-l", "/tmp/tfdiag-none/x.jed:r"],
            ["-c", "gpiod_creator", "-p", "1", "/tmp/tfdiag-none/x.jed:r"],
            ["-c", "other", "-p", "0", "/tmp/tfdiag-none/x.jed:r"],
            ["-c", "gpiod_creator", "-p", "0", "-x:r"],
            ["-j"],
        ]
        for b in bad:
            with self.assertRaises(cpldread.CpldReadError, msg=b):
                cpldread.check_args(b, "gpiod_creator")
        with self.assertRaises(cpldread.CpldReadError):
            cpldread.check_args(good, "gpiod creator; rm")
        existing = Path(__file__)
        with self.assertRaises(cpldread.CpldReadError):
            cpldread.check_args(["-c", "gpiod_creator", "-p", "0", f"{existing}:r"], "gpiod_creator")

    def test_xc3sprog_called_once_through_guard(self):
        src = (PKG / "cpldread.py").read_text()
        self.assertEqual(src.count("tools.run("), 1)
        self.assertIn("args = check_args(", src)
        self.assertIn("tools.run([cfg.xc3sprog, *args]", src)
        for p in SOURCES:
            if p.name != "cpldread.py":
                self.assertNotIn("cfg.xc3sprog", p.read_text(), p.name)

    def test_only_safe_ir(self):
        self.assertEqual(set(P.IR), {"EXTEST", "SAMPLE", "USERCODE", "IDCODE", "BYPASS"})
        isp = {0xE8, 0xE9, 0xEA, 0xEB, 0xEC, 0xED, 0xEE, 0xEF, 0xE5, 0xF0}
        self.assertFalse(set(P.IR.values()) & isp)

    def test_subprocess_only_in_tools_and_jtag(self):
        users = [p.name for p in SOURCES if re.search(r"\bsubprocess\b", p.read_text())]
        self.assertEqual(sorted(users), ["jtag.py", "tools.py"])

    def test_fomu_calls_go_through_guard(self):
        for p in SOURCES:
            if p.name == "fpga.py":
                continue
            self.assertNotIn("fomu_flash,", p.read_text().replace(" ", ""), p.name)


if __name__ == "__main__":
    unittest.main()
