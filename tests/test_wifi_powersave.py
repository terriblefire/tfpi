"""install.sh turns WiFi power save off for good: NetworkManager's default,
every saved WiFi connection, a udev rule, and now. The section runs here
against a scratch root with stand-ins for nmcli and iw."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
START = "# --- WiFi power save off"
END = "# --- Diagnostics tab"

NMCLI = """#!/bin/sh
echo "nmcli $*" >> "$CALLS"
case "$*" in
"-t general status") exit 0 ;;
"-t -f UUID,TYPE connection show")
	printf 'u-wifi1:802-11-wireless\\nu-eth:802-3-ethernet\\nu-wifi2:802-11-wireless\\n' ;;
esac
"""
IW = """#!/bin/sh
echo "iw $*" >> "$CALLS"
"""


def section():
    text = (ROOT / "install.sh").read_text()
    return text[text.index(START):text.index(END)]


class WifiPowerSave(unittest.TestCase):
    def run_section(self, network_manager=True):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(tmp)])
        root, bin_ = tmp / "root", tmp / "bin"
        (root / "etc/udev/rules.d").mkdir(parents=True)
        if network_manager:
            (root / "etc/NetworkManager").mkdir(parents=True)
        for dev in ("wlan0", "eth0"):
            (root / "sys/class/net" / dev).mkdir(parents=True)
        bin_.mkdir()
        for name, body in (("nmcli", NMCLI), ("iw", IW)):
            (bin_ / name).write_text(body)
            (bin_ / name).chmod(0o755)
        script = section().replace("/etc/", "%s/etc/" % root).replace("/sys/class/net", "%s/sys/class/net" % root)
        calls = tmp / "calls"
        env = dict(os.environ, PATH="%s:%s" % (bin_, os.environ["PATH"]), CALLS=str(calls))
        r = subprocess.run(["bash", "-c", "set -euo pipefail\n" + script + "\necho done"],
                           env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.endswith("done\n"))
        return root, calls.read_text().splitlines() if calls.exists() else []

    def test_network_manager(self):
        root, calls = self.run_section()
        conf = (root / "etc/NetworkManager/conf.d/tfpi-wifi-powersave.conf").read_text()
        self.assertEqual(conf, "[connection]\nwifi.powersave = 2\n")       # 2 = disable
        self.assertIn("nmcli connection modify u-wifi1 802-11-wireless.powersave 2", calls)
        self.assertIn("nmcli connection modify u-wifi2 802-11-wireless.powersave 2", calls)
        self.assertFalse([c for c in calls if "u-eth" in c and "modify" in c])
        self.assertIn("iw dev wlan0 set power_save off", calls)
        self.assertFalse([c for c in calls if c.startswith("iw dev eth0")])

    def test_without_network_manager(self):
        root, calls = self.run_section(network_manager=False)
        self.assertFalse((root / "etc/NetworkManager").exists())
        self.assertFalse([c for c in calls if c.startswith("nmcli")])
        rule = (root / "etc/udev/rules.d/70-tfpi-wifi-powersave.rules").read_text()
        self.assertIn('KERNEL=="wlan*"', rule)
        self.assertIn('RUN+="/sbin/iw dev %k set power_save off"', rule)
        self.assertIn("iw dev wlan0 set power_save off", calls)

    def test_iw_installed(self):
        text = (ROOT / "install.sh").read_text()
        pkgs = text[text.index('PKGS="'):].split('"')[1].split()
        self.assertIn("iw", pkgs)


if __name__ == "__main__":
    unittest.main()
