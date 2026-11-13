import hashlib
import unittest
from pathlib import Path

from helpers import Sandbox, bitstream, full_image, jed, make_zip, spi_image
from tfpi.fwimage import SPIROM_OFFSET, FirmwareError, combine, load_release

RELEASES = Path(__file__).resolve().parents[2] / "tf4060" / "boards" / "tf4060r5"


class ReleaseTests(Sandbox):
    def test_full_image_release(self):
        z = make_zip(self.tmp / "r.zip", {"tf4060r5_ram.bin": full_image(),
                                          "tf4060r5_bus_top.jed": jed(), "tf4060r5_bus.jed": jed(9),
                                          "seed.txt": "12656\n"})
        release = load_release(z, board="tf4060r5")
        self.assertEqual(release.fpga_source, "full image")
        self.assertEqual(release.seed, "12656")
        self.assertEqual(release.cpld_jed, jed())          # _bus_top preferred over _bus
        self.assertEqual(release.cpld_device, "XC95288XL-10-TQ144")

    def test_raw_bitstream_without_spirom_is_flashed_with_a_warning(self):
        # Layout of tf4060r5_2025_02_10_1c48879_27399_RC1_1.zip
        z = make_zip(self.tmp / "r.zip", {"tf4060r5_ram.bin": bitstream(), "tf4060r5_bus.jed": jed()})
        release = load_release(z, board="tf4060r5")
        self.assertEqual(release.fpga_image, bitstream())
        self.assertEqual(release.fpga_source, "raw bitstream")
        self.assertEqual(len(release.warnings), 1)
        self.assertIn("spirom.img", release.warnings[0])

    def test_raw_bitstream_with_etc_spirom(self):
        z = make_zip(self.tmp / "r.zip", {"tf4060r5_ram.bin": bitstream(), "tf4060r5_bus_top.jed": jed()})
        release = load_release(z, spirom=spi_image())
        self.assertEqual(release.warnings, [])
        self.assertIn("from /etc/tfpi", release.fpga_source)
        self.assertEqual(release.fpga_image, combine(bitstream(), spi_image()))
        self.assertEqual(release.fpga_image[SPIROM_OFFSET:], spi_image())

    def test_spirom_in_zip_wins(self):
        z = make_zip(self.tmp / "r.zip", {"tf4060r5_ram.bin": bitstream(), "spirom.img": spi_image()})
        self.assertIn("from zip", load_release(z).fpga_source)

    def test_prod_jed_in_subfolder_with_reports(self):
        # Layout of tf4060r5_2026-11-03_22e0593.zip
        z = make_zip(self.tmp / "r.zip", {
            "2026-11-03_22e0593/tf4060r5_ram_flash.bin": full_image(),
            "2026-11-03_22e0593/tf4060r5_bus_prod.jed": jed(),
            "2026-11-03_22e0593/README.md": "- nextpnr seed 180, the best of 256\n",
            "2026-11-03_22e0593/reports/tf4060r5_bus_prod.rpt": "report",
        })
        release = load_release(z, board="tf4060r5")
        self.assertEqual(release.fpga_file, "tf4060r5_ram_flash.bin")
        self.assertEqual(release.cpld_file, "tf4060r5_bus_prod.jed")
        self.assertEqual(release.cpld_jed, jed())
        self.assertEqual(release.seed, "180")

    def test_jed_choice(self):
        def pick(*names):
            files = {"tf4060r5_ram.bin": full_image(), **{n: jed() for n in names}}
            return load_release(make_zip(self.tmp / "j.zip", files)).cpld_file
        self.assertEqual(pick("anything.jed"), "anything.jed")
        self.assertEqual(pick("tf4060r5_bus.jed", "tf4060r5_bus_top.jed"), "tf4060r5_bus_top.jed")
        self.assertEqual(pick("tf4060r5_bus_top.jed", "tf4060r5_bus_prod.jed"), "tf4060r5_bus_prod.jed")
        with self.assertRaisesRegex(FirmwareError, "which CPLD"):
            pick("a.jed", "b.jed")

    def test_ram_flash_preferred_over_raw(self):
        z = make_zip(self.tmp / "r.zip", {"tf4060r5_ram.bin": bitstream(), "tf4060r5_ram_flash.bin": full_image()})
        self.assertEqual(load_release(z).fpga_file, "tf4060r5_ram_flash.bin")

    def test_rejections(self):
        cases = {
            "is for board": {"tf4060r4_ram.bin": full_image()},
            "no .*_ram.bin": {"tf4060r5_bus_top.jed": jed()},
            "iCE40": {"tf4060r5_ram.bin": b"\x00" * 200000},
            "ROMFS|MBR": {"tf4060r5_ram.bin": full_image()[:SPIROM_OFFSET] + b"\x00" * 2048},
            "JEDEC": {"tf4060r5_ram.bin": full_image(), "tf4060r5_bus_top.jed": b"hello"},
        }
        for message, files in cases.items():
            with self.subTest(message):
                z = make_zip(self.tmp / "bad.zip", files)
                with self.assertRaisesRegex(FirmwareError, message):
                    load_release(z, board="tf4060r5")
        (self.tmp / "junk.zip").write_bytes(b"not a zip")
        with self.assertRaisesRegex(FirmwareError, "zip"):
            load_release(self.tmp / "junk.zip")


@unittest.skipUnless(RELEASES.is_dir(), "real release zips not present")
class RealReleaseTests(unittest.TestCase):
    def test_2026_full_image(self):
        release = load_release(RELEASES / "tf4060r5_2026_04_23_0aac415_12656.zip", board="tf4060r5")
        self.assertEqual(release.fpga_source, "full image")
        self.assertEqual(len(release.fpga_image), 1022464)
        self.assertEqual(release.cpld_device, "XC95288XL-10-TQ144")

    def test_2026_11_prod_release(self):
        path = RELEASES.parents[2] / "tf4060r5_2026-11-03_22e0593.zip"
        if not path.exists():
            self.skipTest("zip not present")
        release = load_release(path, board="tf4060r5")
        self.assertEqual(release.cpld_file, "tf4060r5_bus_prod.jed")
        self.assertTrue(hashlib.sha1(release.cpld_jed).hexdigest().startswith("7abe89e2"))
        self.assertTrue(hashlib.sha1(release.fpga_image).hexdigest().startswith("0202d7b5"))
        self.assertEqual(release.seed, "180")

    def test_2024_raw_bitstream_with_spirom(self):
        spirom = (RELEASES.parents[1] / "spirom" / "spirom.img").read_bytes()
        release = load_release(RELEASES / "tf4060r5_18013_2024_06_12_f29479b_alpha.zip",
                               board="tf4060r5", spirom=spirom)
        self.assertEqual(len(release.fpga_image), 1008128)   # matches the memory note
        self.assertEqual(release.seed, "18013")
        # Same image build_flash_image.py produced for the bench (SHA-1 322a1334).
        self.assertTrue(hashlib.sha1(release.fpga_image).hexdigest().startswith("322a1334"))
