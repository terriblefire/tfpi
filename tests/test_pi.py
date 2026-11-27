"""Reboot the Pi: the reboot record, the button, the banner (issue #37)."""
import re
import unittest
from pathlib import Path

from helpers import Sandbox
from tfpi import config, pi


class PiBase(Sandbox):
    def setUp(self):
        super().setUp()
        self.saved_pi = (pi.BOOT_ID, pi.DELAY)
        pi.BOOT_ID = self.tmp / "boot_id"
        pi.BOOT_ID.write_text("1111-aaaa\n")
        pi.DELAY = 0

    def tearDown(self):
        pi.BOOT_ID, pi.DELAY = self.saved_pi
        super().tearDown()


class PiTests(PiBase):
    def test_needed_until_the_next_boot(self):
        self.assertIsNone(pi.reboot_needed())
        pi.mark_reboot("the software update changed config.txt")
        self.assertEqual(pi.reboot_needed()["why"], "the software update changed config.txt")
        pi.BOOT_ID.write_text("2222-bbbb\n")            # rebooted
        self.assertIsNone(pi.reboot_needed())

    def test_no_boot_id_no_banner(self):
        pi.mark_reboot("x")
        pi.BOOT_ID = self.tmp / "missing"
        self.assertIsNone(pi.reboot_needed())

    def test_reboot_runs_systemctl(self):
        pi.reboot().join()
        self.assertIn("reboot", self.systemctl_log().splitlines())
        self.assertIsNone(pi.reboot_error())

    def test_reboot_failure_is_kept(self):
        config.SYSTEMCTL = str(self.tmp / "no-systemctl")
        pi.reboot().join()
        self.assertIn("systemctl reboot", pi.reboot_error())
        config.SYSTEMCTL = str(self.tmp / "bin" / "systemctl")
        pi.reboot().join()                              # a new try clears the old error
        self.assertIsNone(pi.reboot_error())


class PiWebTests(PiBase):
    def setUp(self):
        super().setUp()
        from tfpi import web
        self.web = web
        self.saved_busy = web.busy_reason
        self.reboots = []
        self.saved_reboot = pi.reboot
        pi.reboot = lambda: self.reboots.append(1)
        self.client = web.create_app().test_client()
        token = self.csrf("/setup")
        self.client.post("/setup", data={"csrf": token, "password": "secret1", "confirm": "secret1"})

    def tearDown(self):
        pi.reboot = self.saved_reboot
        self.web.busy_reason = self.saved_busy
        super().tearDown()

    def csrf(self, path="/"):
        page = self.client.get(path).get_data(as_text=True)
        return re.search(r'name="csrf" value="([0-9a-f]+)"', page).group(1)

    def test_home_has_the_button(self):
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn('action="/pi/reboot"', page)
        self.assertIn("No reboot needed.", page)
        self.assertNotIn("to finish:", self.client.get("/firmware").get_data(as_text=True))

    def test_every_page_says_why(self):
        pi.mark_reboot("the software update changed config.txt")
        for path in ("/firmware", "/updates", "/disks"):
            page = self.client.get(path).get_data(as_text=True)
            self.assertIn("<b>Reboot the Pi</b> to finish: the software update changed config.txt.", page)
            self.assertIn('action="/pi/reboot"', page)
        self.assertIn("Needs a reboot:", self.client.get("/").get_data(as_text=True))

    def test_reboot(self):
        pi.mark_reboot("the software update changed config.txt")
        reply = self.client.post("/pi/reboot", data={"csrf": self.csrf()})
        self.assertEqual(self.reboots, [1])
        self.assertTrue(reply.headers["Location"].endswith("/pi/rebooting?boot=1111-aaaa"))
        page = self.client.get(reply.headers["Location"]).get_data(as_text=True)
        self.assertIn("Rebooting the Pi", page)
        self.assertNotIn("to finish:", page)               # no banner while it reboots
        self.assertEqual(self.client.get("/pi/status").get_json(), {"boot_id": "1111-aaaa", "error": None})
        self.assertEqual(self.client.get("/pi/rebooting?boot=%3Cx%3E").status_code, 400)

    def test_refused_while_busy(self):
        self.web.busy_reason = lambda: "A flash, software update or diagnostic test is already running."
        reply = self.client.post("/pi/reboot", data={"csrf": self.csrf()}, follow_redirects=True)
        self.assertEqual(self.reboots, [])
        self.assertIn("already running", reply.get_data(as_text=True))

    def test_needs_csrf(self):
        self.assertEqual(self.client.post("/pi/reboot").status_code, 400)
        self.assertEqual(self.reboots, [])


class InstallerTests(unittest.TestCase):
    def test_no_maybe_reboot_text(self):
        text = (Path(__file__).resolve().parent.parent / "install.sh").read_text()
        self.assertNotIn("Reboot if config.txt changed", text)
        self.assertIn("so the Pi needs no reboot", text)
        self.assertIn("> /var/lib/tfpi/reboot.json", text)
