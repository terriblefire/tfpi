import unittest
import zipfile
from pathlib import Path
import tempfile

from helpers import ROOT

from tfpi import romfs, spirom

BUILD = ROOT.parent / "tf4060v2" / "ssbl" / "build" / "spirom.img"
FLASH = ROOT.parent / "tf4060v2" / "boards" / "tf4060r5" / "tf4060r5_ram_flash.bin"


@unittest.skipUnless(BUILD.exists() and FLASH.exists(), "needs a tf4060v2 checkout with a build")
class RomfsToolTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def run_tool(self, *args):
        self.assertEqual(romfs.main([str(a) for a in args]), 0)

    def test_round_trip_spirom(self):
        self.run_tool("unpack", BUILD, self.tmp / "d")
        self.run_tool("pack", self.tmp / "d", self.tmp / "out.img")
        self.assertEqual((self.tmp / "out.img").read_bytes(), BUILD.read_bytes())

    def test_round_trip_flash_image_and_zip(self):
        release = self.tmp / "rel.zip"
        with zipfile.ZipFile(release, "w") as z:
            z.write(FLASH, "tf4060r5_ram_flash.bin")
        self.run_tool("unpack", release, self.tmp / "d")
        self.run_tool("pack", self.tmp / "d", self.tmp / "out.bin", "--bitstream", FLASH)
        self.assertEqual((self.tmp / "out.bin").read_bytes(), FLASH.read_bytes())

    def test_edit_add_remove_executable(self):
        self.run_tool("unpack", BUILD, self.tmp / "d")
        tree = self.tmp / "d" / "romfs"
        (tree / "README.txt").unlink()
        (tree / "S").mkdir(exist_ok=True)
        (tree / "S" / "tool").write_bytes(b"\0\0\x03\xf3")
        (tree / "S" / "tool").chmod(0o755)
        (tree / ".DS_Store").write_bytes(b"junk")
        self.run_tool("pack", self.tmp / "d", self.tmp / "out.img")
        _, _, volume, root = romfs.read((self.tmp / "out.img").read_bytes())
        self.assertEqual(volume, "TF4060")
        self.assertIsNone(spirom.find(root, "README.txt"))
        self.assertIsNone(spirom.find(root, ".DS_Store"))
        tool = spirom.find(root, "S/tool")
        self.assertTrue(tool.executable)
        self.assertEqual(tool.data, b"\0\0\x03\xf3")

    def test_unpack_refuses_non_empty_dir(self):
        (self.tmp / "d").mkdir()
        (self.tmp / "d" / "x").write_text("x")
        self.assertEqual(romfs.main(["unpack", str(BUILD), str(self.tmp / "d")]), 1)

    def test_bad_input(self):
        (self.tmp / "junk.bin").write_bytes(b"\0" * 4096)
        self.assertEqual(romfs.main(["list", str(self.tmp / "junk.bin")]), 1)
