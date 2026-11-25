import os

from helpers import Sandbox, full_image, jed, make_zip
from tfpi import config, flasher


class FlasherTests(Sandbox):
    def setUp(self):
        super().setUp()
        self._retry, flasher.CHAIN_RETRY_SECONDS = flasher.CHAIN_RETRY_SECONDS, 0

    def tearDown(self):
        flasher.CHAIN_RETRY_SECONDS = self._retry
        super().tearDown()

    def release(self, name="r1.zip", fpga=0, cpld=0):
        return make_zip(self.tmp / name, {"tf4060r5_ram.bin": full_image(fpga),
                                          "tf4060r5_bus_top.jed": jed(cpld), "seed.txt": "5"})

    def flash(self, *args):
        return flasher.main(["flash", *map(str, args)])

    def test_same_release_again_reads_the_cpld_back(self):
        z = self.release()
        self.assertEqual(self.flash(z), 0)
        state = config.load_state()
        self.assertEqual(state["release"], "r1.zip")
        self.assertIsNotNone(state["fpga"]["sha"])
        self.assertEqual(state["cpld"]["device"], "XC95288XL-10-TQ144")
        self.assertEqual(self.cpld_flashes(), 1)
        self.assertEqual(open(os.environ["FAKE_A314"]).read(), "active")  # a314d running again after
        self.assertTrue((config.FIRMWARE_DIR / "r1.zip").exists())

        (self.tmp / "calls.log").unlink()
        self.assertEqual(self.flash(z), 0)
        self.assertEqual(self.cpld_flashes(), 1)                          # no extra erase cycle
        self.assertEqual(self.calls(), ["xc3sprog -j", "xc3sprog :v", "fomu-flash -r", "setgpio"])
        last = config.load_state()["history"][0]
        self.assertEqual((last["fpga"], last["cpld"], last["result"]), ("skip", "fuses match, not erased", "ok"))

    def test_ntsc_setting_picks_the_ntsc_cpld(self):
        z = make_zip(self.tmp / "n.zip", {"tf4060r5_ram_flash.bin": full_image(),
                                          "tf4060r5_bus_prod.jed": jed(0), "tf4060r5_bus_ntsc.jed": jed(1)})
        self.assertEqual(self.flash(z), 0)
        state = config.load_state()
        self.assertEqual(state["cpld"]["file"], "tf4060r5_bus_prod.jed")
        self.assertEqual(self.cpld_flashes(), 1)

        config.write_json(config.WEB_CONF, {"ntsc": True})
        self.assertEqual(self.flash(z), 0)                     # same release: the CPLD changes, the FPGA doesn't
        state = config.load_state()
        self.assertEqual(state["cpld"]["file"], "tf4060r5_bus_ntsc.jed")
        self.assertEqual(self.cpld_flashes(), 2)
        self.assertEqual(state["fpga"]["flashes"], 1)
        self.assertEqual(self.flash(z), 0)
        self.assertEqual(self.cpld_flashes(), 2)               # NTSC CPLD installed: read back only

    def test_new_fpga_same_cpld_does_not_erase_the_cpld(self):
        self.flash(self.release("a.zip"))
        self.assertEqual(self.flash(self.release("b.zip", fpga=1)), 0)
        self.assertEqual(self.cpld_flashes(), 1)
        self.assertEqual(config.load_state()["history"][0]["cpld"], "fuses match, not erased")
        self.assertEqual(config.load_state()["release"], "b.zip")
        self.assertEqual(config.load_state()["fpga"]["flashes"], 2)

    def test_fpga_verify_failure_stops_before_cpld(self):
        os.environ["FAKE_FOMU_CORRUPT"] = "1"
        self.assertEqual(self.flash(self.release(), "--result", self.tmp / "res.json"), 1)
        state = config.load_state()
        self.assertIsNone(state["fpga"]["sha"])
        self.assertEqual(state["fpga"]["last_result"], "verify-failed")
        self.assertIsNone(state["release"])
        self.assertEqual(self.cpld_flashes(), 0)
        self.assertFalse(config.read_json(self.tmp / "res.json", {})["ok"])

    def test_cdone_low_fails(self):
        os.environ["FAKE_CDONE_LOW"] = "1"
        self.assertEqual(self.flash(self.release()), 1)
        self.assertEqual(config.load_state()["fpga"]["last_result"], "cdone-low")
        self.assertEqual(self.cpld_flashes(), 0)

    def test_cpld_failure_locks_out_cpld(self):
        os.environ["FAKE_XC3_FAIL"] = "1"
        self.assertEqual(self.flash(self.release()), 1)
        state = config.load_state()
        self.assertIn("Mismatch at fuse", state["cpld"]["fault"])
        self.assertEqual(self.cpld_flashes(), 1)                          # exactly one attempt

        del os.environ["FAKE_XC3_FAIL"]
        self.assertEqual(self.flash(self.release("b.zip", cpld=1)), 0)
        self.assertEqual(self.cpld_flashes(), 1)                          # still locked: not even read
        self.assertEqual(config.load_state()["history"][0]["result"], "nothing to do")
        self.assertEqual(flasher.main(["clear-cpld-fault"]), 0)
        self.assertEqual(self.flash(self.release("b.zip", cpld=1)), 0)
        self.assertEqual(self.cpld_flashes(), 2)

    def test_no_jtag_chain_does_not_lock_the_cpld(self):
        os.environ["FAKE_XC3_NOCHAIN"] = "3"                              # every chain read fails
        self.assertEqual(self.flash(self.release(), "--result", self.tmp / "res.json"), 1)
        state = config.load_state()
        self.assertIsNone(state["cpld"]["fault"])
        self.assertEqual(self.cpld_flashes(), 0)                          # never programmed
        self.assertEqual(self.calls()[-5:], ["xc3sprog -j"] * 3 + ["fomu-flash -r", "setgpio"])
        self.assertIn("safe to flash again", state["history"][0]["result"])

        del os.environ["FAKE_XC3_NOCHAIN"]
        self.assertEqual(self.flash(self.release()), 0)
        self.assertEqual(self.cpld_flashes(), 1)

    def test_jtag_chain_found_on_a_later_read(self):
        os.environ["FAKE_XC3_NOCHAIN"] = "2"
        self.assertEqual(self.flash(self.release()), 0)
        self.assertEqual(self.calls().count("xc3sprog -j"), 3)
        self.assertEqual(self.cpld_flashes(), 1)
        self.assertIsNotNone(config.load_state()["cpld"]["sha"])

    def test_chain_lost_while_programming_does_not_lock_the_cpld(self):
        self.assertEqual(self.flash(self.release("a.zip")), 0)
        installed = config.load_state()["cpld"]["sha"]
        os.environ["FAKE_XC3_PROG_NOCHAIN"] = "1"
        self.assertEqual(self.flash(self.release("b.zip", cpld=1)), 1)
        cpld = config.load_state()["cpld"]
        self.assertIsNone(cpld["fault"])
        self.assertEqual(cpld["sha"], installed)                          # the CPLD still holds a.zip's
        self.assertEqual(cpld["last_result"], "not-found")

        del os.environ["FAKE_XC3_PROG_NOCHAIN"]
        self.assertEqual(self.flash(self.release("b.zip", cpld=1)), 0)
        self.assertEqual(self.cpld_flashes(), 3)

    def test_old_no_chain_lock_is_ignored(self):
        state = config.load_state()
        state["cpld"]["fault"] = "2026-11-24 18:50:06: xc3sprog exit status 2 (No JTAG Chain found)"
        config.save_state(state)
        self.assertIsNone(config.load_state()["cpld"]["fault"])           # the pages don't show it locked
        self.assertEqual(self.flash(self.release()), 0)
        self.assertEqual(self.cpld_flashes(), 1)

    def test_cpld_already_holding_the_image_is_not_erased(self):
        self.assertEqual(self.flash(self.release("a.zip")), 0)
        config.STATE_FILE.unlink()                                        # e.g. a new Pi on this board
        self.assertEqual(self.flash(self.release("a.zip")), 0)
        self.assertEqual(self.cpld_flashes(), 1)                          # read back, no erase
        cpld = config.load_state()["cpld"]
        self.assertEqual(cpld["file"], "tf4060r5_bus_top.jed")
        self.assertIsNotNone(cpld["sha"])
        self.assertEqual(cpld["flashes"], 0)

    def test_cpld_that_differs_is_programmed(self):
        self.assertEqual(self.flash(self.release("a.zip")), 0)
        self.assertEqual(self.flash(self.release("b.zip", cpld=1)), 0)
        self.assertEqual(self.cpld_flashes(), 2)
        self.assertEqual(config.load_state()["history"][0]["cpld"], "flash")

    def test_spi_flash_id(self):
        sst = "Manufacturer ID: unknown (ff)\n!! JEDEC Manufacturer ID: bf\nMemory model: unknown (26)\nMemory size: unknown (41)\n"
        winbond = "Manufacturer ID: Winbond (ef)\nMemory model: W25Q128JV (70)\nMemory size: 128 Mbit (18)\n"
        self.assertEqual(flasher.lookup_spi_flash(flasher.parse_spi_id(sst)),
                         ("Microchip SST26VF016B", 2 * 1024 * 1024))
        self.assertEqual(flasher.lookup_spi_flash(flasher.parse_spi_id(winbond))[1], 16 * 1024 * 1024)
        self.assertIsNone(flasher.parse_spi_id("Manufacturer ID: unknown (ff)\nMemory model: unknown (ff)\nMemory size: unknown (ff)\n"))

    def test_no_flash_id_fails_before_writing(self):
        os.environ["FAKE_SPI_ID"] = "Manufacturer ID: unknown (ff)\nMemory model: unknown (ff)\nMemory size: unknown (ff)"
        self.assertEqual(self.flash(self.release()), 1)
        self.assertFalse((self.tmp / "spiflash.bin").exists())
        self.assertEqual(config.load_state()["fpga"]["flashes"], 0)
        self.assertEqual(self.cpld_flashes(), 0)

    def test_image_larger_than_flash_refused(self):
        os.environ["FAKE_SPI_ID"] = "Manufacturer ID: x (ef)\nMemory model: x (40)\nMemory size: x (10)"   # 64 KB
        self.assertEqual(self.flash(self.release()), 1)
        self.assertFalse((self.tmp / "spiflash.bin").exists())

    def test_force_fpga(self):
        z = self.release()
        self.flash(z)
        self.flash(z, "--force-fpga")
        self.assertEqual(config.load_state()["fpga"]["flashes"], 2)
        self.assertEqual(self.cpld_flashes(), 1)                          # read back: it already matches

    def test_cpld_recorded_as_installed_but_different_is_programmed(self):
        z = self.release()
        self.flash(z)
        open(os.environ["FAKE_CPLD"], "wb").write(b"programmed another way")
        self.assertEqual(self.flash(z), 0)
        self.assertEqual(self.cpld_flashes(), 2)
        self.assertEqual(config.load_state()["history"][0]["cpld"], "flash")
        self.assertEqual(open(os.environ["FAKE_CPLD"], "rb").read(), jed(0))

    def test_cpld_not_found_but_recorded_as_installed_carries_on(self):
        self.flash(self.release("a.zip"))
        os.environ["FAKE_XC3_NOCHAIN"] = "99"
        self.assertEqual(self.flash(self.release("b.zip", fpga=1)), 0)   # the FPGA update still succeeds
        state = config.load_state()
        self.assertEqual(state["history"][0]["cpld"], "not found, not checked")
        self.assertEqual(state["history"][0]["result"], "ok")
        self.assertEqual(state["release"], "b.zip")
        self.assertIsNone(state["cpld"]["fault"])
        self.assertEqual(self.cpld_flashes(), 1)
        self.assertEqual(self.calls()[-2:], ["fomu-flash -r", "setgpio"])
        self.assertEqual(self.flash(self.release("c.zip", cpld=1)), 1)    # a new CPLD image must be reached

    def test_keeps_last_releases(self):
        for index in range(5):
            self.flash(self.release(f"r{index}.zip", fpga=index))
        stored = sorted(p.name for p in config.FIRMWARE_DIR.glob("*.zip"))
        self.assertEqual(len(stored), config.KEEP_RELEASES)
        self.assertIn("r4.zip", stored)


    def calls(self):
        log = self.tmp / "calls.log"
        return [line for line in log.read_text().splitlines()
                if line.startswith(("fomu-flash -r", "xc3sprog", "setgpio", "fomu-flash -q"))] if log.exists() else []

    def test_fpga_reset_is_the_last_step(self):
        self.assertEqual(self.flash(self.release()), 0)
        # FPGA written, booted (the board-powered check), CPLD, then booted again.
        self.assertEqual(self.calls(), ["fomu-flash -q -u -w " + str(self.tmp / "state" / "work" / "fpga.bin"),
                                        "fomu-flash -r", "setgpio", "xc3sprog -j", "xc3sprog :v",
                                        "xc3sprog", "fomu-flash -r", "setgpio"])

    def test_cpld_only_flash_resets_fpga_after(self):
        self.flash(self.release("a.zip"))
        (self.tmp / "calls.log").unlink()
        self.assertEqual(self.flash(self.release("b.zip", cpld=1)), 0)
        self.assertEqual(self.calls(), ["xc3sprog -j", "xc3sprog :v", "xc3sprog", "fomu-flash -r", "setgpio"])

    def test_failed_fpga_write_still_releases_the_fpga(self):
        os.environ["FAKE_FOMU_CORRUPT"] = "1"
        self.assertEqual(self.flash(self.release()), 1)
        self.assertEqual(self.calls()[-2:], ["fomu-flash -r", "setgpio"])
        self.assertEqual(self.cpld_flashes(), 0)

    def test_cpld_failure_still_resets_fpga(self):
        os.environ["FAKE_XC3_FAIL"] = "1"
        self.assertEqual(self.flash(self.release()), 1)
        self.assertEqual(self.calls()[-3:], ["xc3sprog", "fomu-flash -r", "setgpio"])

    def test_a314d_stopped_for_the_flash_and_started_after(self):
        open(os.environ["FAKE_A314"], "w").write("active")
        self.assertEqual(self.flash(self.release()), 0)
        log = self.systemctl_log().splitlines()
        self.assertEqual([l for l in log if not l.startswith("is-active")], ["stop a314d", "start a314d"])
        self.assertEqual(open(os.environ["FAKE_A314"]).read(), "active")

    def test_a314d_started_again_after_a_failed_flash(self):
        open(os.environ["FAKE_A314"], "w").write("active")
        os.environ["FAKE_FOMU_CORRUPT"] = "1"
        self.assertEqual(self.flash(self.release()), 1)
        self.assertEqual(open(os.environ["FAKE_A314"]).read(), "active")
        self.assertTrue(self.systemctl_log().rstrip().endswith("start a314d"))
