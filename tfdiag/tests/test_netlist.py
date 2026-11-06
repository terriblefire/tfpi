import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tfdiag.tests import helpers  # noqa: F401
from tfdiag import netlist

ROOT = Path(__file__).resolve().parents[3]
GEN = ROOT / "scripts" / "gen_r5_netlist.py"          # in the tf4060v2 tree only
BRD = Path.home() / "git/tf4060/eagle/tf4060.brd"
BSD = netlist.DEFAULT.parent / "xc95288xl_tq144.bsd"


class Committed(unittest.TestCase):
    def setUp(self):
        self.nl = netlist.load()

    def test_known_nets(self):
        ts = self.nl.nets["TS"]
        self.assertIn(["IC1", "_TS"], ts["members"])
        self.assertEqual(ts["cpld_pins"], ["54"])
        self.assertEqual(ts["fpga_pins"], ["44"])
        self.assertEqual(ts["ucf"], ["TS"])
        self.assertEqual(self.nl.fpga_pins["117"]["net"], "RESET")
        self.assertEqual(self.nl.net_of("IC10", "4"), "CPU_RSTI")

    def test_bsdl_cells(self):
        c = self.nl.d["cpld"]
        self.assertEqual(c["boundary_length"], 864)
        e = c["pins"]["111"]
        self.assertEqual((e["net"], e["in"], e["out"], e["ctl"]), ("CPU_RSTI", 263, 262, 261))
        named = [p for p in c["pins"].values() if p.get("ucf")]
        self.assertGreater(len(named), 100)
        self.assertTrue(all("in" in p for p in named))
        self.assertTrue(c["idcode_bits"].endswith("1001011000010110000010010011"))

    def test_neighbours(self):
        self.assertEqual(self.nl.neighbours("BUSCPLD", "54"), ["53", "55"])
        self.assertEqual(self.nl.neighbours("BUSCPLD", "36"), ["35"])     # QFP corner
        self.assertEqual(len(self.nl.neighbours("RAMFPGA", "100")), 2)
        a = self.nl.cpld_pins["53"]["net"]
        self.assertTrue(any(p == "BUSCPLD" for p, _, _ in self.nl.adjacent("TS", a)))


@unittest.skipUnless(GEN.exists() and BRD.exists() and BSD.exists(), "generator / board sources not here")
class Generate(unittest.TestCase):
    def test_regenerates_identically(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "n.json"
            subprocess.run([sys.executable, str(GEN), "--brd", str(BRD), "--bsdl", str(BSD), "-o", str(out)],
                           check=True, capture_output=True)
            new = json.loads(out.read_text())
            old = json.loads(netlist.DEFAULT.read_text())
            self.assertEqual(new["nets"], old["nets"])
            self.assertEqual(new["cpld"], old["cpld"])
            self.assertEqual(new["neighbours"], old["neighbours"])


if __name__ == "__main__":
    unittest.main()
