import json
import os
import re

from helpers import Sandbox
from tfpi import disks

SCSI_PY_CD = "# a314 scsi.py\ncdrom = e.get('cdrom', False)\n"


class DisksBase(Sandbox):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "home" / "a314hdfs"
        self.root.mkdir(parents=True)
        self.conf = self.tmp / "scsi.conf"
        self.scsi_py = self.tmp / "scsi.py"
        self.scsi_py.write_text(SCSI_PY_CD)
        self.pkill_log = self.tmp / "pkill.log"
        pkill = self.tmp / "bin" / "pkill"
        pkill.write_text(f'#!/bin/sh\necho "$@" >> "{self.pkill_log}"\nexit 1\n')   # 1 = nothing matched
        pkill.chmod(0o755)
        self._saved_mod = (disks.SCSI_CONF, disks.SCSI_PY, disks.PKILL)
        disks.SCSI_CONF, disks.SCSI_PY, disks.PKILL = self.conf, self.scsi_py, str(pkill)
        os.environ["TFPI_A314HDFS"] = str(self.root)
        from tfpi.web import create_app
        self.client = create_app().test_client()
        token = self.csrf("/setup")
        self.client.post("/setup", data={"csrf": token, "password": "secret1", "confirm": "secret1"})
        self.token = self.csrf("/disks")

    def tearDown(self):
        disks.SCSI_CONF, disks.SCSI_PY, disks.PKILL = self._saved_mod
        os.environ.pop("TFPI_A314HDFS", None)
        super().tearDown()

    def csrf(self, path):
        page = self.client.get(path).get_data(as_text=True)
        return re.search(r'name="csrf" value="([0-9a-f]+)"', page).group(1)

    def image(self, name, size):
        (self.root / name).write_bytes(b"\0" * size)

    def put(self, name, data, overwrite=False):
        query = {"name": name}
        if overwrite:
            query["overwrite"] = "1"
        return self.client.put("/disks/upload", query_string=query, data=data,
                               headers={"X-CSRF": self.token}, content_type="application/octet-stream")

    def post(self, action, **form):
        return self.client.post(f"/disks/{action}", data={"csrf": self.token, **form}, follow_redirects=True)

    def units(self, **form):
        return self.post("units", **form)

    def written(self):
        return json.loads(self.conf.read_text())["units"]


class DisksTests(DisksBase):
    def test_page_lists_units_and_images(self):
        self.image("Work.hdf", 512 * 100)
        self.image("Game.iso", 2048 * 10)
        page = self.client.get("/disks").get_data(as_text=True)
        for text in ("Unit 0", "Unit 3", "Work.hdf", "Game.iso", "CD-ROM", "Disks"):
            self.assertIn(text, page)

    def test_save_writes_conf_and_reloads(self):
        self.image("Work.hdf", 512 * 100)
        self.image("Game.iso", 2048 * 10)
        page = self.units(image0="Work.hdf", kind0="hd", image1="Game.iso", kind1="cd").get_data(as_text=True)
        self.assertIn("Saved", page)
        self.assertEqual(self.written(), [
            {"unit": 0, "filename": str(self.root / "Work.hdf"), "rw": True, "block_size": 512},
            {"unit": 1, "filename": str(self.root / "Game.iso"), "rw": False, "block_size": 2048, "cdrom": True},
        ])
        self.assertEqual(oct(self.conf.stat().st_mode & 0o777), "0o644")
        self.assertIn(f"-HUP -f {self.scsi_py}", self.pkill_log.read_text())
        units = disks.units()
        self.assertEqual([(u.name, u.kind) for u in units[:2]], [("Work.hdf", "hd"), ("Game.iso", "cd")])

    def test_read_only_hard_disk(self):
        self.image("Work.hdf", 512 * 4)
        self.units(image2="Work.hdf", kind2="hdro")
        self.assertEqual(self.written(), [{"unit": 2, "filename": str(self.root / "Work.hdf"), "rw": False,
                                           "block_size": 512}])

    def test_cd_needs_cd_image_and_whole_blocks(self):
        self.image("Work.hdf", 512 * 4)
        self.image("Odd.iso", 2048 * 3 + 512)
        self.image("Good.iso", 2048 * 3)
        self.assertIn("is a hard disk image", self.units(image0="Work.hdf", kind0="cd").get_data(as_text=True))
        self.assertIn("2048-byte blocks", self.units(image0="Odd.iso", kind0="cd").get_data(as_text=True))
        self.assertIn("use the CD-ROM type", self.units(image0="Good.iso", kind0="hd").get_data(as_text=True))
        self.assertFalse(self.conf.exists())

    def test_cd_refused_without_cd_support(self):
        self.scsi_py.write_text("# old scsi.py\n")
        self.image("Game.iso", 2048)
        page = self.units(image1="Game.iso", kind1="cd").get_data(as_text=True)
        self.assertIn("no CD support", page)
        self.assertFalse(self.conf.exists())

    def test_rw_image_in_one_unit_only(self):
        self.image("Work.hdf", 512 * 4)
        self.image("Game.iso", 2048)
        page = self.units(image0="Work.hdf", kind0="hd", image1="Work.hdf", kind1="hdro").get_data(as_text=True)
        self.assertIn("one unit only", page)
        self.assertFalse(self.conf.exists())
        self.units(image0="Game.iso", kind0="cd", image1="Game.iso", kind1="cd")   # read-only twice is fine
        self.assertEqual(len(self.written()), 2)

    def test_hand_set_unit_outside_folder_is_kept(self):
        outside = self.tmp / "hdfs" / "Harddisk.hdf"
        outside.parent.mkdir()
        outside.write_bytes(b"\0" * 1024)
        self.conf.write_text(json.dumps({"units": [
            {"unit": 0, "filename": str(outside), "rw": True, "block_size": 512},
            {"unit": 7, "filename": "/x/y.hdf", "rw": False}]}))
        self.image("Game.iso", 2048)
        page = self.client.get("/disks").get_data(as_text=True)
        self.assertIn("set by hand, kept", page)
        self.units(image0=disks.KEEP, kind0="hd", image1="Game.iso", kind1="cd")
        self.assertEqual([e["unit"] for e in self.written()], [0, 1, 7])
        self.assertEqual(self.written()[0]["filename"], str(outside))
        self.units(image0="", kind0="hd")                      # emptied on purpose
        self.assertEqual([e["unit"] for e in self.written()], [7])

    def test_upload_rename_delete(self):
        self.assertEqual(self.put("New.hdf", b"\0" * 2048).get_json(), {"saved": "New.hdf"})
        self.assertEqual((self.root / "New.hdf").stat().st_size, 2048)
        self.assertEqual(self.put("New.hdf", b"\0" * 512).status_code, 409)
        self.assertEqual(self.put("New.hdf", b"\0" * 512, overwrite=True).status_code, 200)
        self.assertIn("hard disk images end in", self.put("notes.txt", b"x").get_json()["error"])
        self.assertEqual(self.put("sub/x.hdf", b"\0" * 512).status_code, 400)
        self.post("rename", name="New.hdf", new="Old.hdf")
        self.assertTrue((self.root / "Old.hdf").exists())
        self.assertIn("images end in", self.post("rename", name="Old.hdf", new="Old.txt").get_data(as_text=True))
        self.post("delete", name="Old.hdf")
        self.assertEqual(os.listdir(self.root), [])

    def test_attached_image_is_protected(self):
        self.image("Work.hdf", 512 * 4)
        self.units(image0="Work.hdf", kind0="hd")
        self.assertIn("take it out of the unit", self.post("delete", name="Work.hdf").get_data(as_text=True))
        self.assertIn("take it out of the unit", self.post("rename", name="Work.hdf", new="W.hdf").get_data(as_text=True))
        reply = self.put("Work.hdf", b"\0" * 512, overwrite=True)
        self.assertEqual(reply.status_code, 400)
        self.assertEqual((self.root / "Work.hdf").stat().st_size, 512 * 4)

    def test_upload_not_capped_like_forms(self):
        big = self.put("big.iso", b"\0" * (9 << 20))
        self.assertEqual(big.status_code, 200)

    def test_missing_folder_is_created_on_upload(self):
        (self.root).rmdir()
        self.assertIn("No images yet", self.client.get("/disks").get_data(as_text=True))
        self.assertEqual(self.put("a.hdf", b"\0" * 512).status_code, 200)
        self.assertTrue((self.root / "a.hdf").exists())

    def test_needs_login(self):
        self.client.post("/logout", data={"csrf": self.token})
        self.assertIn("/login", self.client.get("/disks").headers["Location"])


def raw_sectors(count, mode=1, first=0):
    """Raw 2352-byte sectors; the 2048 data bytes of sector n are all (n % 251)."""
    out = bytearray()
    for n in range(first, first + count):
        header = bytes([0, 2, n % 75, mode])
        data = bytes([n % 251]) * 2048
        lead = data if mode == 1 else bytes(8) + data
        out += disks.SYNC + header + lead + bytes(2352 - 16 - len(lead))
    return bytes(out)


class DiskTypesTests(DisksBase):
    def cue(self, name, binary, mode, extra=""):
        (self.root / name).write_text(f'FILE "{binary}" BINARY\n  TRACK 01 {mode}\n    INDEX 01 00:00:00\n' + extra)

    def test_img_and_fixed_vhd_are_hard_disks(self):
        self.image("Raw.img", 512 * 8)
        footer = bytearray(512)
        footer[:8] = b"conectix"
        footer[60:64] = (2).to_bytes(4, "big")
        (self.root / "Fixed.vhd").write_bytes(b"\0" * 512 * 8 + bytes(footer))
        self.units(image0="Raw.img", kind0="hd", image1="Fixed.vhd", kind1="hdro")
        self.assertEqual([(e["unit"], e["block_size"], e["rw"]) for e in self.written()], [(0, 512, True), (1, 512, False)])

    def test_dynamic_vhd_refused(self):
        footer = bytearray(512)
        footer[:8] = b"conectix"
        footer[60:64] = (3).to_bytes(4, "big")
        (self.root / "Dyn.vhd").write_bytes(bytes(footer) + b"\0" * 1024 + bytes(footer))
        self.assertIn("dynamic VHD", self.client.get("/disks").get_data(as_text=True))
        self.assertIn("only fixed VHDs", self.units(image0="Dyn.vhd", kind0="hd").get_data(as_text=True))
        self.assertFalse(self.conf.exists())

    def test_cooked_bin_cue_used_directly(self):
        self.image("Game.bin", 2048 * 20)
        self.cue("Game.cue", "Game.bin", "MODE1/2048", '  TRACK 02 AUDIO\n    INDEX 01 00:00:15\n')
        self.assertIn("put the .bin", self.units(image1="Game.cue", kind1="cd").get_data(as_text=True))
        self.units(image1="Game.bin", kind1="cd")
        self.assertEqual(self.written(), [{"unit": 1, "filename": str(self.root / "Game.bin"), "rw": False,
                                           "block_size": 2048, "cdrom": True}])

    def test_raw_bin_cue_needs_converting(self):
        (self.root / "Raw.bin").write_bytes(raw_sectors(40))
        self.cue("Raw.cue", "Raw.bin", "MODE1/2352")
        page = self.client.get("/disks").get_data(as_text=True)
        self.assertIn("MODE1/2352 data track in Raw.bin", page)
        self.assertIn("convert it to <span class=\"mono\">Raw.iso</span>?", page)
        self.assertIn("convert it to an .iso first", self.units(image0="Raw.bin", kind0="cd").get_data(as_text=True))
        self.assertFalse(self.conf.exists())

    def convert(self, name, keep):
        form = {"name": name}
        if keep:
            form["keep"] = "1"
        page = self.post("convert", **form).get_data(as_text=True)
        if disks.CONVERT_THREAD:
            disks.CONVERT_THREAD.join(10)
        return page

    def test_convert_keeping_the_originals(self):
        (self.root / "Raw.bin").write_bytes(raw_sectors(30) + bytes(2352 * 5))   # 5 sectors of audio after it
        self.cue("Raw.cue", "Raw.bin", "MODE1/2352", '  TRACK 02 AUDIO\n    INDEX 01 00:00:30\n')
        self.assertIn("Converting Raw.cue", self.convert("Raw.cue", keep=True))
        iso = (self.root / "Raw.iso").read_bytes()
        self.assertEqual(len(iso), 30 * 2048)
        self.assertEqual(iso[2048 * 7:2048 * 8], bytes([7]) * 2048)
        self.assertTrue((self.root / "Raw.bin").exists() and (self.root / "Raw.cue").exists())
        self.assertIn("Converted Raw.cue to Raw.iso", self.client.get("/disks").get_data(as_text=True))
        self.units(image2="Raw.iso", kind2="cd")
        self.assertEqual(self.written()[0]["filename"], str(self.root / "Raw.iso"))
        self.assertIn("already exists", self.convert("Raw.cue", keep=True))

    def test_convert_without_the_originals(self):
        (self.root / "Mode2.bin").write_bytes(raw_sectors(12, mode=2))      # no .cue: found by its sync bytes
        page = self.client.get("/disks").get_data(as_text=True)
        self.assertIn("raw CD image (MODE2/2352)", page)
        self.convert("Mode2.bin", keep=False)
        self.assertEqual(sorted(os.listdir(self.root)), ["Mode2.iso"])
        self.assertEqual((self.root / "Mode2.iso").read_bytes()[2048 * 11:], bytes([11]) * 2048)

    def test_bin_named_in_cue_keeps_its_name(self):
        self.image("Game.bin", 2048 * 4)
        self.cue("Game.cue", "Game.bin", "MODE1/2048")
        self.assertIn("would break", self.post("rename", name="Game.bin", new="X.bin").get_data(as_text=True))
        self.post("rename", name="Game.cue", new="Other.cue")
        self.assertTrue((self.root / "Other.cue").exists())
