"""tfdiag must never write the FPGA SPI flash or program the CPLD."""

import re
import unittest
from pathlib import Path

from tfdiag.tests import helpers  # noqa: F401  (sys.path)
from tfdiag import fpga, protocol as P

PKG = Path(__file__).resolve().parents[1]
SOURCES = [p for p in PKG.rglob("*.py") if "tests" not in p.parts]

FORBIDDEN = [
    (r"""["']-w["']""", "fomu-flash -w (write flash)"),
    (r"""["']-u["']""", "fomu-flash -u (unprotect)"),
    (r"""["']-e["']""", "fomu-flash -e (erase)"),
    (r"xc3sprog", "xc3sprog (CPLD programming)"),
    (r"XC3SPROG", "xc3sprog (CPLD programming)"),
    (r"\b(ISPEN|ISPENC|FERASE|FBULK|FPGM|FPGMI|FBLANK|FVFY|FVFYI|ISPEX)\b", "XC9500 ISP instruction"),
    (r"svf|xsvf|program_jed|jed\b", "CPLD programming file"),
    (r"flashrom|mtd_debug|spiflash.*write", "flash writer"),
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
                for pat, what in FORBIDDEN:
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
