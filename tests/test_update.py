import fcntl
import hashlib
import json
import re
import sys
import time

from helpers import Sandbox
from tfpi import config, update

OLD, NEW = "a" * 40, "b" * 40
PINS = {"FOMU_FLASH_REF": "f1", "XC3SPROG_REF": "x2", "XC3SPROG_REF_GPIOD1": "x1", "A314_REF": "a1"}


class UpdateBase(Sandbox):
    def setUp(self):
        super().setUp()
        self.a314d = self.tmp / "a314d"
        self.a314d.write_bytes(b"built")
        self.boot = self.tmp / "config.txt"
        self.boot.write_text("dtparam=spi=on\n")
        self.saved = {k: getattr(update, k) for k in ("A314D", "BOOT_FILES", "UPDATE_CMD", "SYSTEMD_RUN",
                                                      "remote_head", "remote_pins", "remote_changes")}
        update.A314D, update.BOOT_FILES = self.a314d, [self.boot]
        update.SYSTEMD_RUN = str(self.tmp / "no-systemd-run")
        update.remote_head = lambda: NEW
        update.remote_pins = lambda commit: dict(PINS)
        update.remote_changes = lambda old, new: ["Files tab"]
        self.installed(a314="a1")

    def tearDown(self):
        for key, value in self.saved.items():
            setattr(update, key, value)
        super().tearDown()

    def installed(self, a314="a1", xc3="x1", a314d_sha=None):
        sha = a314d_sha if a314d_sha is not None else hashlib.sha256(b"built").hexdigest()
        config.write_json(config.VERSIONS_FILE, {
            "tfpi": {"ref": OLD}, "fomu-flash": {"ref": "f1"}, "xc3sprog": {"ref": xc3},
            "a314": {"ref": a314, "a314d_sha256": sha}})


class UpdateTests(UpdateBase):
    def test_only_tfpi_changed_leaves_a314d(self):
        result = update.check()
        self.assertTrue(result["available"])
        self.assertEqual(result["rebuild"], [])            # x1 is the libgpiod 1.x pin
        self.assertFalse(result["a314d_interrupted"])
        self.assertEqual(result["changes"], ["Files tab"])

    def test_a314_pin_change_warns(self):
        self.installed(a314="a0")
        result = update.check()
        self.assertEqual(result["rebuild"], ["a314"])
        self.assertTrue(result["a314d_interrupted"])
        self.assertFalse(result["a314d_local"])
        self.a314d.write_bytes(b"hand built")
        self.assertTrue(update.check()["a314d_local"])

    def test_up_to_date_and_errors(self):
        update.remote_head = lambda: OLD
        self.assertFalse(update.check()["available"])

        def offline():
            raise update.UpdateError("can't reach github")
        update.remote_head = offline
        self.assertIn("can't reach", update.check()["error"])

    def test_run_records_result_and_reboot(self):
        script = self.tmp / "tfpi-update"
        script.write_text(f"#!{sys.executable}\nopen({str(self.boot)!r}, 'a').write('x\\n'); print('installing')\n")
        script.chmod(0o755)
        update.UPDATE_CMD = str(script)
        log, result = self.tmp / "u.log", self.tmp / "u.json"
        self.assertEqual(update.run(log, result), 0)
        data = json.loads(result.read_text())
        self.assertTrue(data["ok"])
        self.assertTrue(data["reboot"])
        self.assertIn("installing", log.read_text())

    def test_run_refuses_during_flash(self):
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(config.LOCK_FILE, "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            self.assertEqual(update.run(self.tmp / "u.log", self.tmp / "u.json"), 1)
        self.assertIn("running", json.loads((self.tmp / "u.json").read_text())["result"])


class UpdateWebTests(UpdateBase):
    def setUp(self):
        super().setUp()
        from tfpi.web import create_app
        self.client = create_app().test_client()
        token = self.csrf("/setup")
        self.client.post("/setup", data={"csrf": token, "password": "secret1", "confirm": "secret1"})

    def csrf(self, path):
        page = self.client.get(path).get_data(as_text=True)
        return re.search(r'name="csrf" value="([0-9a-f]+)"', page).group(1)

    def test_page_shows_update_and_runs_it(self):
        self.assertIn("Not checked for updates yet", self.client.get("/").get_data(as_text=True))
        self.installed(a314="a0")
        page = self.client.post("/update/check", data={"csrf": self.csrf("/")},
                                follow_redirects=True).get_data(as_text=True)
        self.assertIn("Update available", page)
        self.assertIn("a314d will be stopped", page)
        self.assertIn("Files tab", page)

        script = self.tmp / "tfpi-update"
        script.write_text(f"#!{sys.executable}\nprint('a314: unchanged, a314d left running')\n")
        script.chmod(0o755)
        update.UPDATE_CMD = str(script)
        # The detached runner is a new process: hand it the stub via the environment.
        import os
        os.environ.update(TFPI_UPDATE_CMD=str(script), TFPI_BOOT_FILES=str(self.boot),
                          TFPI_A314D=str(self.a314d), TFPI_UPDATE_REPO="file:///nonexistent")
        try:
            started = self.client.post("/update/start", data={"csrf": self.csrf("/")})
            job = started.headers["Location"].rsplit("/", 1)[1]
            self.assertTrue(job.startswith("update-"))
            self.assertIn("Updating the Pi software", self.client.get(f"/job/{job}").get_data(as_text=True))
            for _ in range(100):
                reply = self.client.get(f"/job/{job}/log").get_json()
                if reply["status"] == "done":
                    break
                time.sleep(0.2)
        finally:
            for key in ("TFPI_UPDATE_CMD", "TFPI_BOOT_FILES", "TFPI_A314D", "TFPI_UPDATE_REPO"):
                os.environ.pop(key, None)
        self.assertEqual(reply["status"], "done", reply)
        self.assertTrue(reply["result"]["ok"])
        self.assertIn("a314d left running", reply["text"])

    def test_page_says_a314d_untouched(self):
        page = self.client.post("/update/check", data={"csrf": self.csrf("/")},
                                follow_redirects=True).get_data(as_text=True)
        self.assertIn("a314d keeps running", page)


class FollowBranchTests(UpdateBase):
    """A314_REF=tf4060: the branch's head decides, not the word in sources.conf (tf4060v2 #123)."""
    def setUp(self):
        super().setUp()
        saved = update.ls_remote
        self.addCleanup(setattr, update, "ls_remote", saved)
        self.branch_at = "c" * 40
        update.ls_remote = lambda repo, branch: self.branch_at
        pins = dict(PINS, A314_REPO="https://github.com/erique/a314", A314_REF="tf4060")
        update.remote_pins = lambda commit: dict(pins)

    def test_branch_at_the_installed_commit_needs_no_rebuild(self):
        self.installed(a314="c" * 40)
        result = update.check()
        self.assertEqual(result["rebuild"], [])
        self.assertFalse(result["a314d_interrupted"])

    def test_branch_moved_rebuilds_a314(self):
        self.installed(a314="d" * 40)
        result = update.check()
        self.assertEqual(result["rebuild"], ["a314"])
        self.assertTrue(result["a314d_interrupted"])

    def test_branch_moved_is_offered_even_when_tfpi_has_not(self):
        update.remote_head = lambda: OLD                  # tfpi itself unchanged
        self.installed(a314="d" * 40)
        self.assertTrue(update.check()["available"])
        self.installed(a314="c" * 40)
        self.assertFalse(update.check()["available"])

    def test_commit_refs_are_left_alone(self):
        pins = {"A314_REPO": "r", "A314_REF": "e" * 40, "FOMU_FLASH_REF": "f1"}
        self.assertEqual(update.resolve_pins(pins), pins)
