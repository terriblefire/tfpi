"""a314d restarts after it exits: tfpi's systemd drop-in."""
import configparser
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DROPIN = ROOT / "systemd" / "a314d.service.d" / "tfpi-restart.conf"


class A314dRestart(unittest.TestCase):
    def test_dropin(self):
        unit = configparser.ConfigParser()
        unit.read_string(DROPIN.read_text())
        self.assertEqual(unit["Service"]["Restart"], "always")
        self.assertGreater(int(unit["Service"]["RestartSec"]), 0)
        self.assertEqual(unit["Unit"]["StartLimitIntervalSec"], "0")   # never gives up

    def test_installed(self):
        text = (ROOT / "install.sh").read_text()
        self.assertIn('"$self_dir/systemd/a314d.service.d/tfpi-restart.conf" '
                      "/etc/systemd/system/a314d.service.d/tfpi-restart.conf", text)
        # installed before the daemon-reload that follows
        self.assertLess(text.index("a314d.service.d/tfpi-restart.conf"), text.index("systemctl daemon-reload"))


if __name__ == "__main__":
    unittest.main()
