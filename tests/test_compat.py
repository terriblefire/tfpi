import re
import subprocess

from helpers import Sandbox, full_image, jed, make_zip
from tfpi import compat, config, flasher, update
from tfpi.fwimage import load_release

A314_LINE = ("- a314 (a314.device, a314fs, the C: tools) built in terriblefire78/spirom from "
             "https://github.com/erique/a314 tf4060 at {}\n")


class CompatBase(Sandbox):
    """An a314 checkout where the installer keeps it: old -> handshake -> new."""

    def setUp(self):
        super().setUp()
        src = config.SHARE / "src" / "a314"
        src.mkdir(parents=True)

        def git(*args):
            return subprocess.run(["git", "-C", str(src), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                                  check=True, capture_output=True, text=True).stdout.strip()
        git("init", "-q")
        commits = []
        for subject in ("old", "handshake", "new"):
            git("commit", "-q", "--allow-empty", "-m", subject)
            commits.append(git("rev-parse", "HEAD"))
        self.old, self.hs, self.new = commits
        self.saved_handshake = compat.A314_HANDSHAKE
        compat.A314_HANDSHAKE = self.hs
        compat._contains_handshake.cache_clear()

    def tearDown(self):
        compat.A314_HANDSHAKE = self.saved_handshake
        compat._contains_handshake.cache_clear()
        super().tearDown()

    def pi(self, a314):
        config.write_json(config.VERSIONS_FILE, {"tfpi": {"ref": "t" * 40, "version": "1.0"},
                                                 "a314": {"ref": a314}})

    def flashed(self, a314=None, sha="f" * 64, release="tf4060r5_x.zip"):
        state = config.load_state()
        state["fpga"]["sha"] = sha
        state["fpga"]["a314"] = a314
        state["release"] = release
        config.save_state(state)


class CompatTests(CompatBase):
    def test_a314_side(self):
        self.assertTrue(compat.a314_has_handshake(self.new))
        self.assertTrue(compat.a314_has_handshake(self.hs))
        self.assertTrue(compat.a314_has_handshake(self.hs[:7]))
        self.assertFalse(compat.a314_has_handshake(self.old))
        self.assertIsNone(compat.a314_has_handshake("0" * 40))     # not in the checkout
        self.assertIsNone(compat.a314_has_handshake(""))
        self.assertIsNone(compat.a314_has_handshake("tf4060"))     # a branch name, not a commit

    def test_new_a314d_with_old_firmware(self):
        self.pi(self.new)
        self.flashed(a314=self.old, release="tf4060r5_v0.8.1_2026-11-10_3f7716a.zip")
        text = compat.banner(builds=[])
        self.assertIn("needs TF4060 firmware v0.8.2 or later", text)
        self.assertIn("v0.8.1", text)
        self.assertIn("Firmware page", text)

    def test_old_a314d_with_new_firmware(self):
        self.pi(self.old)
        self.flashed(a314=self.new)
        text = compat.banner(builds=[])
        self.assertIn("needs a newer a314d", text)
        self.assertIn("Updates page", text)

    def test_matching_or_unknown_shows_nothing(self):
        for pi, fw in ((self.new, self.new), (self.old, self.old)):
            self.pi(pi)
            self.flashed(a314=fw)
            self.assertIsNone(compat.banner(builds=[]))
        self.pi(self.new)
        self.flashed(a314=None)                                    # flashed before tfpi recorded it
        self.assertIsNone(compat.banner(builds=[]))
        state = config.load_state()
        state["fpga"]["sha"] = None                                # never flashed by tfpi
        config.save_state(state)
        self.assertIsNone(compat.banner(builds=[]))
        config.write_json(config.VERSIONS_FILE, {})                # installer never ran
        self.assertIsNone(compat.banner(builds=[]))

    def test_published_build_version_when_a314_unknown(self):
        self.pi(self.new)
        self.flashed(a314=None, sha="s" * 64)
        old = [{"name": "v0.8.1_2026-11-10_3f7716a", "fpga_sha": "s" * 64}]
        self.assertIn("v0.8.1_2026-11-10_3f7716a", compat.banner(builds=old))
        new = [{"name": "v0.8.2_2026-11-20_abcdef1", "fpga_sha": "s" * 64}]
        self.assertIsNone(compat.banner(builds=new))
        dev = [{"name": "2026-11-11_f1795a2", "fpga_sha": "s" * 64}]  # a dev build: no version
        self.assertIsNone(compat.banner(builds=dev))

    def test_release_readme_names_the_a314_commit(self):
        z = make_zip(self.tmp / "r.zip", {"v0.8.1_x/tf4060r5_ram_flash.bin": full_image(),
                                          "v0.8.1_x/README.md": "- nextpnr seed 244\n" + A314_LINE.format(self.old)})
        self.assertEqual(load_release(z).a314_commit, self.old)
        z = make_zip(self.tmp / "s.zip", {"tf4060r5_ram_flash.bin": full_image()})
        self.assertEqual(load_release(z).a314_commit, "")

    def test_flash_records_the_firmware_a314(self):
        z = make_zip(self.tmp / "r.zip", {"tf4060r5_ram.bin": full_image(), "tf4060r5_bus_top.jed": jed(),
                                          "README.md": A314_LINE.format(self.new)})
        self.assertEqual(flasher.main(["flash", str(z)]), 0)
        self.assertEqual(config.load_state()["fpga"]["a314"], self.new)
        self.pi(self.old)
        self.assertIn("needs a newer a314d", compat.banner(builds=[]))


class CompatWebTests(CompatBase):
    def setUp(self):
        super().setUp()
        from tfpi.web import create_app
        self.client = create_app().test_client()
        page = self.client.get("/setup").get_data(as_text=True)
        token = re.search(r'name="csrf" value="([0-9a-f]+)"', page).group(1)
        self.client.post("/setup", data={"csrf": token, "password": "secret1", "confirm": "secret1"})

    def test_banner_on_every_page(self):
        self.pi(self.new)
        self.flashed(a314=self.old)
        for path in ("/", "/firmware", "/updates", "/diag"):
            self.assertIn("Incompatible versions", self.client.get(path).get_data(as_text=True), path)
        self.flashed(a314=self.new)
        self.assertNotIn("Incompatible versions", self.client.get("/").get_data(as_text=True))

    def test_version_shown(self):
        from tfpi import __version__
        self.assertIn(f"<td class=\"mono\">{__version__}</td>", self.client.get("/updates").get_data(as_text=True))


class VersionCheckTests(Sandbox):
    def test_remote_version(self):
        saved = update._get
        try:
            update._get = lambda url: b'"""tfpi"""\n\n__version__ = "1.2"\n'
            self.assertEqual(update.remote_version("c" * 40), "1.2")
            update._get = lambda url: b"# no version yet\n"
            self.assertIsNone(update.remote_version("c" * 40))

            def offline(url):
                raise OSError("offline")
            update._get = offline
            self.assertIsNone(update.remote_version("c" * 40))
        finally:
            update._get = saved
