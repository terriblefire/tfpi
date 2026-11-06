import unittest

from tfdiag.tests import helpers  # noqa: F401
from tfdiag import vcd, protocol as P


class Vcd(unittest.TestCase):
    def test_runs_to_changes(self):
        entries = [(0b11, 5), (0b10, 3), (0b10, 2), (0b01, 10)]
        text = vcd.to_vcd(entries, signals=["A", "B"], fabric_mhz=100.0, trigger_index=1)
        lines = text.splitlines()
        self.assertIn("$timescale 1ps $end", lines)
        self.assertIn("$var wire 1 ! A $end", lines)
        self.assertIn("$var wire 1 \" B $end", lines)
        body = lines[lines.index("$enddefinitions $end") + 1:]
        # t=0: A=1 B=1 TRIGGER=0; t=50 ns: A=0 + trigger; t=80 ns: trigger off, no A/B change;
        # t=100 ns: A=1 B=0; end at 200 ns
        self.assertEqual(body[:4], ["#0", "1!", "1\"", "0#"])
        self.assertEqual(body[4:7], ["#50000", "0!", "1#"])
        self.assertEqual(body[7:9], ["#80000", "0#"])
        self.assertEqual(body[9:12], ["#100000", "1!", "0\""])
        self.assertEqual(body[-1], "#200000")

    def test_default_signals(self):
        text = vcd.to_vcd([(0, 1)])
        for s in P.WAVE_SIGNALS:
            self.assertIn(f" {s} $end", text)


if __name__ == "__main__":
    unittest.main()
