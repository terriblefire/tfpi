import io
import os
import re
import struct
import sys
import textwrap
import zipfile
from pathlib import Path

from helpers import ROOT, Sandbox
from tfpi import config, spirom
from tfpi.fwimage import PREAMBLE

TF4060V2 = ROOT.parent / "tf4060v2"
REAL_BUILD = TF4060V2 / "ssbl" / "build"


def makefile_objects():
    """tf4060v2/ssbl/Makefile's OBJECTS without toc.o (what mksdk.sh writes to objects.txt)."""
    text = (TF4060V2 / "ssbl" / "Makefile").read_text()
    names = re.search(r"^OBJECTS\s*:=\s*\$\(addprefix \$\(BUILD\)/,(.*)\)\s*$", text, re.M).group(1).split()
    return tuple(n for n in names if n != "toc.o")


def hunk(name, idstring, romtag=True, version=1):
    """A one-hunk AmigaOS executable, optionally holding a Resident."""
    body = bytearray(64)
    if romtag:
        # struct Resident: match word/tag, end skip, flags, version, type, pri, name, id, init
        struct.pack_into(">HIIBBBbIII", body, 0, 0x4AFC, 0, 26, 0, version, 3, 0, 64, 64 + len(name) + 1, 0)
    body += name.encode() + b"\0" + idstring.encode() + b"\0"
    body += b"\0" * (-len(body) % 4)
    longs = len(body) // 4
    return (struct.pack(">IIIIII", 0x3F3, 0, 1, 0, 0, longs) + struct.pack(">II", 0x3E9, longs)
            + bytes(body) + struct.pack(">I", 0x3F2))


def ssbl(modules):
    """A synthetic SSBL: code, TOC (as toc.i lays it out), data, CRC word."""
    out = bytearray(b"\x4e\x75" * 64)
    out += b"\xff\xff\xff\xff" + b"\0" * 8
    for data in modules:
        start = len(out)
        out += struct.pack(">IIII", start + 16 + len(data) + 12, len(data), 0, 0) + data
        out += b"\xff\xff\xff\xff" + b"\0" * 8
    out += struct.pack(">II", 0, len(modules)) + b"\xde\xad\xbe\xef" + b"DATA" * 8
    return spirom.add_crc(bytes(out))


def tree():
    root = spirom.Node("", spirom.DIRECTORY, True)
    devs = spirom.Node("Devs", spirom.DIRECTORY, True, children=[
        spirom.Node("a.device", spirom.REGULAR, True, data=hunk("a.device", "a 1.0")),
        spirom.Node("MountList", spirom.REGULAR, False, data=b"PI0: x\n")])
    root.children = [spirom.Node("README.txt", spirom.REGULAR, False, data=b"hello" * 50),
                     devs, spirom.Node("Disk Info", spirom.REGULAR, False, data=b"x" * 17)]
    return root


class SpiromTests(Sandbox):
    def test_romfs_round_trip(self):
        image = spirom.build_romfs("TF4060", tree())
        self.assertEqual(len(image) % 1024, 0)
        volume, root = spirom.parse_romfs(image)
        self.assertEqual(volume, "TF4060")
        paths = {p: n for p, n in spirom.walk(root)}
        self.assertEqual(sorted(paths), ["Devs", "Devs/MountList", "Devs/a.device", "Disk Info", "README.txt"])
        self.assertTrue(paths["Devs/a.device"].executable)
        self.assertEqual(paths["Disk Info"].data, b"x" * 17)
        self.assertEqual(spirom.build_romfs(volume, root), image)

    def test_toc_and_image(self):
        mods = [hunk("first.device", "first 1.2 (1.1.26)"), hunk("plain", "x", romtag=False)]
        strap = ssbl(mods)
        image = spirom.make_image(strap, spirom.build_romfs("TF4060", tree()))
        part, romfs = spirom.split_image(image)
        self.assertEqual(spirom.ssbl_length(part), len(strap))      # despite the zero padding
        toc = spirom.parse_toc(part[:len(strap)])
        self.assertEqual([m.name for m in toc], ["first.device", ""])
        self.assertEqual(toc[0].idstring, "first 1.2 (1.1.26)")
        self.assertTrue(spirom.has_romtag(mods[0]))
        self.assertFalse(spirom.has_romtag(mods[1]))

    def test_real_build_round_trips(self):
        if not (REAL_BUILD / "spirom.img").exists():
            self.skipTest("tf4060v2 SSBL build not present")
        image = (REAL_BUILD / "spirom.img").read_bytes()
        part, romfs = spirom.split_image(image)
        strap = part[:spirom.ssbl_length(part)]
        self.assertEqual(strap, (REAL_BUILD / "spirom.strap").read_bytes())
        self.assertEqual(spirom.build_romfs(*spirom.parse_romfs(romfs)), (REAL_BUILD / "romfs.img").read_bytes())
        self.assertEqual(spirom.make_image(strap, spirom.build_romfs(*spirom.parse_romfs(romfs))), image)
        self.assertIn("spiromfs", [m.name for m in spirom.parse_toc(strap)])

    def test_real_relink(self):
        """Needs vasm/vlink (TFPI_VASM/TFPI_VLINK) and the tf4060v2 SSBL build."""
        if not (os.environ.get("TFPI_VASM") and (REAL_BUILD / "main.o").exists()):
            self.skipTest("vasm/vlink or the SSBL objects not available")
        from tfpi import sdk
        order = makefile_objects()
        objects = {n: (REAL_BUILD / n).read_bytes() for n in order}
        kit = sdk.Sdk("t", "tf4060r5", {"spirom.img": (REAL_BUILD / "spirom.img").read_bytes()}, objects,
                      (TF4060V2 / "ssbl" / "src" / "toc.i").read_bytes(), order)
        sdk.check_relink(kit, sdk.read_spirom(kit.spirom))

    def test_sdk_object_order(self):
        """The link order comes from ssbl/objects.txt; SDKs without it use the old list."""
        from tfpi import sdk
        new = ("start.o", "main.o", "reloc.o", "kprintf.o", "spi.o", "notice.o", "spi_asm.o")
        kit = sdk.load_sdk(make_sdk(self.tmp / "new.zip", [hunk("a", "a 1")], objects=True, order=new))
        self.assertEqual(kit.object_order, new)
        self.assertTrue(kit.can_relink)
        old = sdk.load_sdk(make_sdk(self.tmp / "old.zip", [hunk("a", "a 1")], objects=True))
        self.assertEqual(old.object_order, sdk.SSBL_OBJECTS)
        self.assertTrue(old.can_relink)
        # objects.txt names an object the SDK does not carry: no relink
        with zipfile.ZipFile(self.tmp / "new.zip") as src, zipfile.ZipFile(self.tmp / "gap.zip", "w") as dst:
            for info in src.infolist():
                if not info.filename.endswith("/notice.o"):
                    dst.writestr(info, src.read(info))
        self.assertFalse(sdk.load_sdk(self.tmp / "gap.zip").can_relink)


LEGACY_OBJECTS = ("start.o", "main.o", "reloc.o", "kprintf.o", "spi.o", "spi_asm.o")


def make_sdk(path, modules, objects=False, order=None):
    """An SDK zip; objects: ship ssbl/*.o (LEGACY_OBJECTS, or `order`) and toc.i;
    order: also ship ssbl/objects.txt with that link order."""
    image = spirom.make_image(ssbl(modules), spirom.build_romfs("TF4060", tree()))
    with zipfile.ZipFile(path, "w") as archive:
        base = "tf4060r5_x_SDK/"
        archive.writestr(base + "tf4060r5_ram.asc", ".comment test\n")
        archive.writestr(base + "bootrom_syn.mif", "00\n" * 1024)
        archive.writestr(base + "fsbl.mif", "4e\n71\n")
        archive.writestr(base + "spirom.img", image)
        if objects:
            for name in (*(order or LEGACY_OBJECTS), "toc.i"):
                archive.writestr(base + "ssbl/" + name, b"obj")
        if order is not None:
            archive.writestr(base + "ssbl/objects.txt", "\n".join(order) + "\n")
    return Path(path)


class CustomWebTests(Sandbox):
    def setUp(self):
        super().setUp()
        bindir = self.tmp / "bin"
        for name, body in {
            "icebram": "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
            "icepack": f"import sys; open(sys.argv[2], 'wb').write({PREAMBLE!r} + b'\\x00' * 500)",
        }.items():
            (bindir / name).write_text(f"#!{sys.executable}\n{body}\n")
            (bindir / name).chmod(0o755)
        from tfpi import sdk
        self._tools = (sdk.ICEBRAM, sdk.ICEPACK)
        sdk.ICEBRAM, sdk.ICEPACK = str(bindir / "icebram"), str(bindir / "icepack")
        from tfpi.web import create_app
        self.client = create_app().test_client()
        token = self.csrf("/setup")
        self.client.post("/setup", data={"csrf": token, "password": "secret1", "confirm": "secret1"})

    def tearDown(self):
        from tfpi import sdk
        sdk.ICEBRAM, sdk.ICEPACK = self._tools
        super().tearDown()

    def csrf(self, path):
        return re.search(r'name="csrf" value="([0-9a-f]+)"', self.client.get(path).get_data(as_text=True)).group(1)

    def post(self, url, editor, **fields):
        data = {"csrf": self.csrf(editor), **fields}
        reply = self.client.post(url, data=data, content_type="multipart/form-data", follow_redirects=True)
        page = reply.get_data(as_text=True)
        return re.findall(r'<div class="flash (\w+)">(.*?)</div>', page), page

    def open_sdk(self, **kw):
        z = make_sdk(self.tmp / "x_SDK.zip", [hunk("one.device", "one 1.0"), hunk("spiromfs", "spiromfs 1.3")], **kw)
        reply = self.client.post("/upload", data={"csrf": self.csrf("/"), "firmware": (open(z, "rb"), "x_SDK.zip")},
                                 content_type="multipart/form-data")
        self.assertIn("/custom/", reply.headers["Location"])
        return reply.headers["Location"]

    def test_romfs_edit_and_build(self):
        editor = self.open_sdk()
        page = self.client.get(editor).get_data(as_text=True)
        for text in ("one.device", "spiromfs", "README.txt", "only ROMFS changes are possible"):
            self.assertIn(text, page)
        token = editor.rsplit("/", 1)[1]
        msgs, _ = self.post(f"/custom/{token}/module", editor, file=(io.BytesIO(hunk("b", "b")), "b.device"))
        self.assertIn("no SSBL objects", msgs[0][1])
        self.post(f"/custom/{token}/file", editor, folder="C", file=(io.BytesIO(hunk("t", "t")), "Tool"))
        self.post(f"/custom/{token}/remove", editor, path="Disk Info")
        msgs, _ = self.post(f"/custom/{token}/file", editor, folder="",
                            file=(io.BytesIO(b"\1" * 2_100_000), "huge"))
        self.assertIn("the limit is 1961984", msgs[0][1])
        msgs, page = self.post(f"/custom/{token}/build", editor)
        self.assertEqual(msgs[0][0], "ok", msgs)
        self.assertIn("will be flashed", page)

        built = sorted(config.UPLOAD_DIR.glob("*/*_custom_*.zip"))[-1]
        archive = zipfile.ZipFile(built)
        self.assertEqual(sorted(archive.namelist()), ["README.md", "tf4060r5_ram_flash.bin"])
        image = archive.read("tf4060r5_ram_flash.bin")
        self.assertTrue(image.startswith(PREAMBLE))
        _, romfs = spirom.split_image(image[0x21000:])
        _, root = spirom.parse_romfs(romfs)
        self.assertTrue(spirom.find(root, "C/Tool").executable)
        self.assertIsNone(spirom.find(root, "Disk Info"))
        self.assertIn("added C/Tool", archive.read("README.md").decode())

    def test_module_needs_romtag(self):
        editor = self.open_sdk(objects=True)
        token = editor.rsplit("/", 1)[1]
        msgs, _ = self.post(f"/custom/{token}/module", editor,
                            file=(io.BytesIO(hunk("plain", "x", romtag=False)), "blabber.driver"))
        self.assertIn("has no ROMTag", msgs[0][1])
        msgs, _ = self.post(f"/custom/{token}/module", editor, file=(io.BytesIO(b"text"), "notes.txt"))
        self.assertIn("HUNK_HEADER", msgs[0][1])
        msgs, page = self.post(f"/custom/{token}/module", editor,
                               file=(io.BytesIO(hunk("two.device", "two 2.0")), "two.device"))
        self.assertEqual(msgs, [])
        self.assertIn("two 2.0", page)
        self.post(f"/custom/{token}/module/2/up", editor)
        page = self.client.get(editor).get_data(as_text=True)
        self.assertLess(page.index("two.device"), page.index("spiromfs</b>"))

    def test_missing_tools_give_a_message(self):
        from tfpi import sdk
        sdk.ICEBRAM = str(self.tmp / "nope" / "icebram")
        editor = self.open_sdk()
        msgs, _ = self.post(f"/custom/{editor.rsplit('/', 1)[1]}/build", editor)
        self.assertIn("icebram is not installed", msgs[0][1])
