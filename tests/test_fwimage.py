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

    def test_raw_bitstream_needs_spirom(self):
        z = make_zip(self.tmp / "r.zip", {"tf4060r5_ram.bin": bitstream(), "tf4060r5_bus_top.jed": jed()})
        with self.assertRaisesRegex(FirmwareError, "spirom.img"):
            load_release(z)
        release = load_release(z, spirom=spi_image())
        self.assertEqual(release.fpga_image, combine(bitstream(), spi_image()))
        self.assertEqual(release.fpga_image[SPIROM_OFFSET:], spi_image())

    def test_spirom_in_zip_wins(self):
        z = make_zip(self.tmp / "r.zip", {"tf4060r5_ram.bin": bitstream(), "spirom.img": spi_image()})
        self.assertIn("from zip", load_release(z).fpga_source)

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

    def test_2024_raw_bitstream_with_spirom(self):
        spirom = (RELEASES.parents[1] / "spirom" / "spirom.img").read_bytes()
        release = load_release(RELEASES / "tf4060r5_18013_2024_06_12_f29479b_alpha.zip",
                               board="tf4060r5", spirom=spirom)
        self.assertEqual(len(release.fpga_image), 1008128)   # matches the memory note
        self.assertEqual(release.seed, "18013")
        # Same image build_flash_image.py produced for the bench (SHA-1 322a1334).
        self.assertTrue(hashlib.sha1(release.fpga_image).hexdigest().startswith("322a1334"))
