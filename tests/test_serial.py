"""The Serial page: a314d's serial.log and pty (tfpi/serial.py)."""
import configparser
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import Sandbox
from tfpi import serial

ROOT = Path(__file__).resolve().parent.parent
DROPIN = ROOT / "systemd" / "a314d.service.d" / "tfpi-serial.conf"


class SerialFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.run_dir = self.tmp / "run"
        self.fallback = self.tmp / "tmp"
        patcher = mock.patch.object(serial, "DIRS", (self.run_dir, self.fallback))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_no_a314d(self):
        self.assertIsNone(serial.directory())
        self.assertEqual(serial.read(-1), (None, 0))
        with self.assertRaises(OSError):
            serial.send_raw("x")

    def test_tail_then_new_bytes(self):
        self.fallback.mkdir()
        log = self.fallback / "serial.log"
        log.write_bytes(b"a" * (serial.TAIL + 10) + b"\r\nend\n")
        text, offset = serial.read(-1)
        self.assertEqual(len(text), serial.TAIL)                # as sent, CR included
        self.assertTrue(text.endswith("\r\nend\n"))
        self.assertEqual(offset, log.stat().st_size)
        with open(log, "ab") as handle:
            handle.write(b"more \xe4\n")
        self.assertEqual(serial.read(offset), ("more \xe4\n", offset + 7))

    def test_new_log_starts_again(self):
        self.run_dir.mkdir()
        (self.run_dir / "serial.log").write_bytes(b"new\n")
        self.assertEqual(serial.read(5000), ("new\n", 4))
        # the rest of the old log first
        (self.run_dir / "serial.log.1").write_bytes(b"x" * 5000 + b"old end\n")
        self.assertEqual(serial.read(5000), ("old end\nnew\n", 4))
        self.assertEqual(serial.log_bytes(), b"x" * 5000 + b"old end\nnew\n")

    def test_send_raw(self):
        self.run_dir.mkdir()
        tty = self.run_dir / "tty"
        tty.write_bytes(b"")                     # a file in place of the pty
        serial.send_raw("\x03\x1b[A\r\xe4")
        self.assertEqual(tty.read_bytes(), b"\x03\x1b[A\r\xe4")

    def test_dropin(self):
        unit = configparser.ConfigParser()
        unit.read_string(DROPIN.read_text())
        self.assertEqual(unit["Service"]["RuntimeDirectory"], "a314d")
        self.assertEqual(unit["Service"]["RuntimeDirectoryPreserve"], "yes")
        text = (ROOT / "install.sh").read_text()
        self.assertIn('"$self_dir/systemd/a314d.service.d/tfpi-serial.conf" '
                      "/etc/systemd/system/a314d.service.d/tfpi-serial.conf", text)
        self.assertLess(text.index("a314d.service.d/tfpi-serial.conf"), text.index("systemctl daemon-reload"))


class SerialPage(Sandbox):
    def setUp(self):
        super().setUp()
        self.dir = self.tmp / "a314d"
        patcher = mock.patch.object(serial, "DIRS", (self.dir,))
        patcher.start()
        self.addCleanup(patcher.stop)
        from tfpi.web import create_app
        self.client = create_app().test_client()

    def csrf(self, path):
        page = self.client.get(path).get_data(as_text=True)
        return re.search(r'name="csrf" value="([0-9a-f]+)"', page).group(1)

    def login(self):
        token = self.csrf("/setup")
        self.client.post("/setup", data={"csrf": token, "password": "secret1", "confirm": "secret1"})

    def test_needs_login(self):
        self.assertIn("/setup", self.client.get("/serial").headers["Location"])

    def test_page_token_download(self):
        from tfpi import serialws
        self.login()
        page = self.client.get("/serial").get_data(as_text=True)
        self.assertIn("No serial port yet", page)
        token = re.search(r'token = "([^"]+)"', page).group(1)
        secret = self.client.application.config["SECRET_KEY"]
        self.assertTrue(serialws.check_token(secret, token))
        self.assertIn(":{}/serial?t=".format(serialws.PORT), page.replace('" + location.hostname + "', ""))
        fresh = self.client.get("/serial/token").get_json()["token"]
        self.assertTrue(serialws.check_token(secret, fresh))
        self.dir.mkdir()
        (self.dir / "serial.log").write_bytes(b"~ SSBL ~\n")
        r = self.client.get("/serial/download")
        self.assertEqual((r.data, r.headers["Content-Disposition"]), (b"~ SSBL ~\n", "attachment; filename=amiga-serial.log"))
        self.assertEqual(self.client.get("/serial/log").status_code, 404)        # no polling routes


if __name__ == "__main__":
    unittest.main()
