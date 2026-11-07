"""Firmware identification: flash image ids, JED fuse maps, release matching, and the
CPLD readback in the snapshot (only with the Amiga held, only the read action)."""

import hashlib
import json
import struct
import unittest
import zipfile
from pathlib import Path

from tfdiag.tests import helpers, fakes
from tfdiag import bundle, fwid

PREAMBLE = bytes.fromhex("FF0000FF7EAA997E")


def make_image(git="abc1234", design=b"\x5a", romfs=b"rom"):
    bitstream = PREAMBLE + design * 300 + b"\x01\x06\x00\x00"
    ssbl_sectors, romfs_sectors = 2, 3
    mbr = bytearray(512)
    for i, (kind, start, n) in enumerate(((0x68, 1, ssbl_sectors), (0x69, 1 + ssbl_sectors, romfs_sectors))):
        e = 446 + 16 * i
        mbr[e + 4] = kind
        struct.pack_into("<II", mbr, e + 8, start, n)
    mbr[510:512] = b"\x55\xaa"
    ssbl = (b"SSBL" + b"\0" * 508) * ssbl_sectors
    stamp = b"\x1cSPI ROM:  2026-11-04 21:41 UTC, git " + git.encode() + b"\x00\x01"
    rom = (b"-rom1fs-" + romfs + stamp).ljust(512 * romfs_sectors, b"\0")
    spirom = bytes(mbr) + ssbl + rom
    return bitstream + b"\xff" * (fwid.SPIROM_OFFSET - len(bitstream)) + spirom


ISE_JED = ("\x02Programmer Jedec Bit Map\nDate Extracted: Wed Nov  4 21:41:40 2026\n\n"
           "QF16*\nQP144*\nQV0*\nF0*\nX0*\nJ0 0*\nN VERSION K.31*\nN DEVICE XC95288XL-10-TQ144*\n"
           "L0000 1010 0000*\nL0008 0000\n 0011*\nC0000*\n\x030000\n")


class FlashIdent(unittest.TestCase):
    def test_image(self):
        img = make_image()
        data = img + b"\xff" * 4096                     # rest of the flash
        f = fwid.flash_ident(data)
        self.assertEqual(f["image_bytes"], len(img))
        self.assertEqual(f["image_sha256"], hashlib.sha256(img).hexdigest())
        self.assertEqual(f["spirom_git"], "abc1234")
        self.assertEqual(f["spirom_built"], "2026-11-04 21:41 UTC")
        self.assertEqual(f["bitstream_bytes"], 8 + 300 + 4)
        self.assertTrue(f["bitstream"])

    def test_raw_bitstream_release_same_design_id(self):
        img = make_image()
        raw = img[:fwid.SPIROM_OFFSET].rstrip(b"\xff")
        self.assertEqual(hashlib.sha256(fwid.bitstream_of(raw)).hexdigest(),
                         fwid.flash_ident(img)["bitstream_sha256"])

    def test_short_read(self):
        img = make_image()
        f = fwid.flash_ident(img[:fwid.SPIROM_OFFSET + 600])
        self.assertTrue(f["image_truncated"])
        self.assertNotIn("image_sha256", f)

    def test_no_partition_table(self):
        f = fwid.flash_ident(PREAMBLE + b"\x11" * 100)
        self.assertNotIn("image_bytes", f)
        self.assertNotIn("spirom_git", f)

    def test_real_release_if_present(self):
        p = Path.home() / "git/tmp/tf4060v2/i86/2026-11-04_667a6e7/tf4060r5_ram_flash.bin"
        if not p.exists():
            self.skipTest("no local release")
        f = fwid.flash_ident(p.read_bytes() + b"\xff" * 1000)
        self.assertEqual(f["image_bytes"], p.stat().st_size)
        self.assertEqual(f["spirom_git"], "667a6e7")


class Jed(unittest.TestCase):
    def test_ise_header_and_fields(self):
        qf, bits = fwid.parse_jed(ISE_JED)
        self.assertEqual(qf, 16)
        self.assertEqual(bits, "1010000000000011")

    def test_default_fill(self):
        qf, bits = fwid.parse_jed("\x02QF8*F1*L2 00*\x03")
        self.assertEqual(bits, "11001111")

    def test_errors(self):
        with self.assertRaises(fwid.JedError):
            fwid.parse_jed("\x02QF8*F0*\x03")
        with self.assertRaises(fwid.JedError):
            fwid.parse_jed("\x02QF4*L2 000*\x03")

    def test_readback_matches_its_source(self):
        a, _ = fwid.fuse_ident(fakes.cpld_jed(3))
        b, _ = fwid.fuse_ident(fakes.cpld_jed(3).replace("N DEVICE XC95288XL-TQ144*\n", ""))
        self.assertEqual(a["fuse_sha256"], b["fuse_sha256"])
        self.assertNotEqual(a["fuse_sha256"], fwid.fuse_ident(fakes.cpld_jed(4))[0]["fuse_sha256"])


def write_release(d, name, image, jed):
    d.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(d / name, "w") as z:
        z.writestr("tf4060r5_ram_flash.bin", image)
        z.writestr("tf4060r5_bus_prod.jed", jed)
    return d / name


class Matching(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="tfdiag-fwid-"))

    def test_identify(self):
        img_a, img_b = make_image("aaaaaaa", b"\x11"), make_image("bbbbbbb", b"\x22")
        jed_a, jed_b = fakes.cpld_jed(1), fakes.cpld_jed(2)
        write_release(self.tmp, "rel_a.zip", img_a, jed_a)
        write_release(self.tmp, "rel_b.zip", img_b, jed_b)
        rels = fwid.known_releases(self.tmp)
        self.assertEqual([r["name"] for r in rels], ["rel_a.zip", "rel_b.zip"])
        state = {"fpga_sha256": hashlib.sha256(img_a).hexdigest(),
                 "cpld_sha256": hashlib.sha256(jed_a.encode()).hexdigest()}
        cpld, bits = fwid.fuse_ident(jed_b)                 # the board has B's CPLD
        res = fwid.identify(fwid.flash_ident(img_a), cpld, bits, rels, state)
        self.assertEqual(res["fpga"]["matches"], ["rel_a.zip"])
        self.assertEqual(res["fpga"]["tfpi_record"], "matches")
        self.assertEqual(res["cpld"]["matches"], ["rel_b.zip:tf4060r5_bus_prod.jed"])
        self.assertEqual(res["cpld"]["tfpi_record"], "differs")
        fs, cs = fwid.summary(res)
        self.assertIn("aaaaaaa", fs)
        self.assertIn("rel_b.zip", cs)
        self.assertIn("differs", cs)
        json.dumps(res)                                      # no raw fuse strings, serialisable
        self.assertNotIn("_bits", json.dumps(res))

    def test_unknown_cpld(self):
        write_release(self.tmp, "rel_a.zip", make_image(), fakes.cpld_jed(1))
        cpld, bits = fwid.fuse_ident(fakes.cpld_jed(9))
        res = fwid.identify(None, cpld, bits, fwid.known_releases(self.tmp), None)
        self.assertEqual(res["cpld"]["matches"], [])
        self.assertTrue(res["cpld"]["nearest"][0]["fuses_differ"] > 0)
        self.assertIn("no stored release matches", fwid.summary(res)[1])

    def test_bad_zip_is_reported_not_fatal(self):
        (self.tmp / "junk.zip").write_bytes(b"not a zip")
        rels = fwid.known_releases(self.tmp)
        self.assertIn("error", rels[0])


class Snapshot(helpers.Base):
    def snap(self):
        env = self.env
        rc, evs = helpers.run_cli(env, ["snapshot", "--out", str(env.tmp / "run" / "before.json")])
        (env.tmp / "run").mkdir(exist_ok=True)
        return rc, evs, json.loads((env.tmp / "run" / "before.json").read_text())

    def test_cpld_read_only_while_held(self):
        rc, evs, b = self.snap()
        self.assertEqual(rc, 0, evs)
        s = self.board()
        self.assertEqual(s["forbidden"], [])
        self.assertEqual(len(s["cpld_reads"]), 1)
        self.assertTrue(s["cpld_reads"][0]["amiga_reset"])
        self.assertEqual(s["cpld_reads"][0]["image"], "diag_timing")
        reads = [x for x in s["log"] if x.startswith("xc3sprog")]
        self.assertEqual(len(reads), 1)
        self.assertRegex(reads[0], r"^xc3sprog -c gpiod_creator -p 0 \S+\.jed:r$")
        fw = b["firmware"]
        want, _ = fwid.fuse_ident(fakes.cpld_jed(1))
        self.assertEqual(fw["cpld"]["fuse_sha256"], want["fuse_sha256"])
        self.assertEqual(fw["cpld"]["idcode_after"], "0x59616093")
        self.assertTrue((self.env.tmp / "run" / "firmware" / "cpld_readback.jed").exists())
        self.assertTrue((self.env.tmp / "run" / "firmware" / "fpga_bitstream.bin").exists())
        self.assertIn("bitstream_sha256", fw["fpga"])
        self.assertTrue(s["amiga_reset"])                 # still held afterwards
        self.assertEqual(s.get("exposures", []), [])
        names = [e.get("name") for e in evs if e.get("event") == "result"]
        self.assertIn("firmware_cpld", names)
        self.assertIn("firmware_fpga", names)
        z = self.env.tmp / "out.zip"
        contents = {c["name"] for c in bundle.make(self.env.tmp / "run", z, hostnames=set())}
        self.assertIn("firmware/cpld_readback.jed", contents)
        self.assertIn("firmware/fpga_bitstream.bin", contents)

    def test_readback_failure_is_incomplete_not_fatal(self):
        self.env.board.update(lambda s: s.update(cpld_read_fails=True))
        rc, evs, b = self.snap()
        self.assertEqual(rc, 0, evs)
        self.assertIn("error", b["firmware"]["cpld"])
        r = [e for e in evs if e.get("name") == "firmware_cpld"][0]
        self.assertEqual(r["status"], "incomplete")

    def test_readback_can_be_switched_off(self):
        self.env.cfg.cpld_readback = False
        rc, evs, b = self.snap()
        self.assertEqual(rc, 0, evs)
        self.assertNotIn("cpld_reads", self.board())
        self.assertIn("switched off", b["firmware"]["cpld"]["error"])

    def test_not_read_when_amiga_not_held(self):
        for name in ("diag_timing.bin", "diag_pins.bin"):
            (self.env.images / name).unlink()
        rc, evs, b = self.snap()
        self.assertNotIn("cpld_reads", self.board())
        self.assertIn("not read", b["firmware"]["cpld"]["error"])


if __name__ == "__main__":
    unittest.main()


def encode_asc(rom, extra_blocks=3):
    """Inverse of fwid's BRAM decode: a 1 KB ROM -> .asc text with the two nibble BRAMs
    (in a shuffled position among blank ones)."""
    def block(nib):
        words = [0] * 256
        for c in range(16):
            for r in range(256):
                if (nib[r * 4 + fwid.BRAM_SEL[c & 3]] >> fwid.BRAM_NB[c]) & 1:
                    words[r] |= 1 << c
        lines = []
        for i in range(16):
            lines.append("".join(f"{words[i * 16 + j]:04x}" for j in reversed(range(16))))
        return lines
    hi = [b >> 4 for b in rom]
    lo = [b & 15 for b in rom]
    blank = [0] * 1024
    order = [blank] * extra_blocks + [lo, blank, hi]
    out = []
    for k, nib in enumerate(order):
        out.append(f".ram_data 8 {k * 2 + 1}")
        out += block(nib)
    return "\n".join(out) + "\n"


class BootRom(unittest.TestCase):
    def rom(self, stamp):
        r = bytearray(b"\x4e\x71" * 512)
        r[0x51:0x51 + len(stamp)] = stamp
        return bytes(r)

    def test_git_stamp(self):
        asc = encode_asc(self.rom(b"$VER:tf4060v2r5_2026-11-05_f555529\0"))
        self.assertEqual(fwid.boot_rom_version(asc),
                         {"name": "tf4060v2r5", "built": "2026-11-05", "tag": "f555529", "git": "f555529"})

    def test_old_tag_stamp(self):
        v = fwid.boot_rom_version(encode_asc(self.rom(b"$VER:tf4060r5_2026-10-23_v2\0")))
        self.assertEqual((v["built"], v["tag"], v["git"]), ("2026-10-23", "v2", None))

    def test_none(self):
        self.assertIsNone(fwid.boot_rom_version(encode_asc(self.rom(b"no stamp here"))))

    def test_real_bitstream_if_present(self):
        p = Path.home() / "git/tmp/tf4060v2/bramdec/t.asc"
        if not p.exists():
            self.skipTest("no local bitstream")
        self.assertEqual(fwid.boot_rom_version(p.read_text())["git"], "f555529")


class Catalog(unittest.TestCase):
    def test_known_and_unknown(self):
        img = make_image("aaaaaaa")
        cpld, bits = fwid.fuse_ident(fakes.cpld_jed(5))
        cat = [fwid.catalog_entry("rel_a", img, [("bus.jed", fakes.cpld_jed(5))], date="2026-11-01")]
        f = fwid.flash_ident(img)
        res = fwid.identify(f, cpld, bits, (), None, cat)
        v = fwid.verdict(res)
        self.assertEqual((v["fpga"]["state"], v["fpga"]["release"]), ("known", "rel_a"))
        self.assertEqual((v["cpld"]["state"], v["cpld"]["release"]), ("known", "rel_a"))
        self.assertEqual(cat[0]["git"], "aaaaaaa")
        # unknown: boot ROM stamp gives the commit; the CPLD only its fuse checksum
        f2 = fwid.flash_ident(make_image("bbbbbbb", b"\x33"))
        f2["boot_rom"] = {"name": "tf4060v2r5", "built": "2026-11-06", "tag": "bbbbbbb", "git": "bbbbbbb"}
        cpld2, bits2 = fwid.fuse_ident(fakes.cpld_jed(6))
        v2 = fwid.verdict(fwid.identify(f2, cpld2, bits2, (), None, cat))
        self.assertEqual(v2["fpga"]["state"], "unknown")
        self.assertIn("built from git bbbbbbb on 2026-11-06", v2["fpga"]["detail"])
        self.assertEqual(v2["cpld"]["state"], "unknown")
        self.assertIn("fuse checksum", v2["cpld"]["detail"])

    def test_spirom_differs(self):
        img = make_image("aaaaaaa")
        cat = [fwid.catalog_entry("rel_a", img)]
        other = make_image("aaaaaaa", romfs=b"custom")          # same bitstream, customised SPI ROM
        v = fwid.verdict(fwid.identify(fwid.flash_ident(other), None, None, (), None, cat))
        self.assertEqual(v["fpga"]["state"], "known")
        self.assertIn("SPI ROM (SSBL/ROMFS) differs", v["fpga"]["detail"])

    def test_shipped_catalog_loads(self):
        p = Path(__file__).resolve().parents[2] / "tfpi" / "known_firmware.json"
        cat = fwid.load_catalog(p)
        self.assertTrue(any(r["name"].endswith("f555529") for r in cat))
        for r in cat:
            self.assertEqual(len(r["fpga"]["bitstream_sha256"]), 64)
            self.assertTrue(all(len(j["fuse_sha256"]) == 64 for j in r["cpld"]))


class Identify(helpers.Base):
    def test_identify_reads_and_restores(self):
        env = self.env
        jed = fakes.cpld_jed(1)
        Path(env.cfg.known_firmware).write_text(json.dumps(
            {"releases": [{"name": "rel_x", "fpga": None, "cpld": [
                {"file": "bus.jed", "fuse_sha256": fwid.fuse_ident(jed)[0]["fuse_sha256"]}]}]}))
        out = env.tmp / "verify"
        rc, evs = helpers.run_cli(env, ["identify", "--images", str(env.images), "--out", str(out)])
        self.assertEqual(rc, 0, evs[-1])
        fw = json.loads((out / "firmware.json").read_text())
        self.assertTrue(fw["restore_ok"])
        self.assertEqual(fw["verdict"]["cpld"]["release"], "rel_x")
        self.assertEqual(fw["verdict"]["fpga"]["state"], "unknown")
        s = self.board()
        self.assertEqual(s["forbidden"], [])
        self.assertEqual(s["image"], "flash")             # back on the normal firmware
        self.assertEqual(s["a314d"], "active")
        self.assertTrue(s["cpld_reads"][0]["amiga_reset"])
        self.assertFalse(Path(env.cfg.marker).exists())
