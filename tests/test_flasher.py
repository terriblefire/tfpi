import os

from helpers import Sandbox, full_image, jed, make_zip
from tfpi import config, flasher


class FlasherTests(Sandbox):
    def release(self, name="r1.zip", fpga=0, cpld=0):
        return make_zip(self.tmp / name, {"tf4060r5_ram.bin": full_image(fpga),
                                          "tf4060r5_bus_top.jed": jed(cpld), "seed.txt": "5"})

    def flash(self, *args):
        return flasher.main(["flash", *map(str, args)])

    def test_first_flash_then_nothing_to_do(self):
        z = self.release()
        self.assertEqual(self.flash(z), 0)
        state = config.load_state()
        self.assertEqual(state["release"], "r1.zip")
        self.assertIsNotNone(state["fpga"]["sha"])
        self.assertEqual(state["cpld"]["device"], "XC95288XL-10-TQ144")
        self.assertEqual(self.cpld_flashes(), 1)
        self.assertFalse((self.tmp / "a314.log").exists())               # a314d left alone
        self.assertTrue((config.FIRMWARE_DIR / "r1.zip").exists())

        self.assertEqual(self.flash(z), 0)
        self.assertEqual(self.cpld_flashes(), 1)                          # no extra erase cycle
        self.assertEqual(config.load_state()["history"][0]["result"], "nothing to do")

    def test_new_fpga_same_cpld_skips_cpld(self):
        self.flash(self.release("a.zip"))
        self.assertEqual(self.flash(self.release("b.zip", fpga=1)), 0)
        self.assertEqual(self.cpld_flashes(), 1)
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
        self.assertIn("Verify: Failure", state["cpld"]["fault"])
        self.assertEqual(self.cpld_flashes(), 1)                          # exactly one attempt

        del os.environ["FAKE_XC3_FAIL"]
        self.assertEqual(self.flash(self.release("b.zip", cpld=1), "--force-cpld"), 0)
        self.assertEqual(self.cpld_flashes(), 1)                          # still locked
        self.assertEqual(flasher.main(["clear-cpld-fault"]), 0)
        self.assertEqual(self.flash(self.release("b.zip", cpld=1)), 0)
        self.assertEqual(self.cpld_flashes(), 2)

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

    def test_force_flags(self):
        z = self.release()
        self.flash(z)
        self.flash(z, "--force-fpga")
        self.assertEqual(config.load_state()["fpga"]["flashes"], 2)
        self.assertEqual(self.cpld_flashes(), 1)
        self.flash(z, "--force-cpld")
        self.assertEqual(self.cpld_flashes(), 2)

    def test_keeps_last_releases(self):
        for index in range(5):
            self.flash(self.release(f"r{index}.zip", fpga=index))
        stored = sorted(p.name for p in config.FIRMWARE_DIR.glob("*.zip"))
        self.assertEqual(len(stored), config.KEEP_RELEASES)
        self.assertIn("r4.zip", stored)
