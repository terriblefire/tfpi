"""Unpack and repack the TF4060 SPI ROM (SSBL + ROMFS) from the command line.

  tfpi-romfs list   IMAGE
  tfpi-romfs unpack IMAGE DIR
  tfpi-romfs pack   DIR OUT [--bitstream FLASH_OR_BITSTREAM]

IMAGE is a spirom.img, a full RAM flash image (*_ram_flash.bin, SPI ROM at
0x21000), a firmware release zip or an SDK zip.

unpack writes:
  DIR/ssbl.bin        the SSBL exactly as in the image (pack reuses it)
  DIR/modules/        the SSBL's resident modules, for reference only:
                      changing them needs a relink (the web page's SPI ROM editor)
  DIR/romfs/          the ROMFS tree; executable files get mode 755
  DIR/volume.txt      the ROMFS volume name
  DIR/romfs.order     entry order in the image, so an unchanged tree packs
                      back byte for byte; new files go after, sorted

pack builds spirom.img from DIR. With --bitstream it writes a full flash
image instead: the bitstream (or the first 0x21000 bytes of a flash image)
plus the new SPI ROM at 0x21000.

Runs on the Pi (PYTHONPATH=/opt/tfpi/lib) or anywhere with python3.
"""

import argparse
import os
import re
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath

from . import spirom
from .fwimage import MAX_FLASH_IMAGE, SPIROM_OFFSET, FirmwareError, combine

ORDER = "romfs.order"


class RomfsError(ValueError):
    pass


# --- reading ---------------------------------------------------------------------

def _spirom_of(data, what):
    """The spirom.img inside a spirom.img or a full flash image."""
    if len(data) >= 512 and data[510:512] == b"\x55\xaa":
        return data
    tail = data[SPIROM_OFFSET:]
    if len(tail) >= 512 and tail[510:512] == b"\x55\xaa":
        return tail
    raise RomfsError(f"{what}: no SPI ROM (MBR) at 0 or at 0x21000")


def load(path):
    path = Path(path)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            names = {PurePosixPath(n).name: n for n in archive.namelist()
                     if not n.endswith("/") and "__MACOSX" not in n}
            for wanted in ("spirom.img",):
                if wanted in names:
                    return _spirom_of(archive.read(names[wanted]), wanted)
            for suffix in ("_ram_flash.bin", "_ram.bin"):
                hits = sorted(n for n in names if n.endswith(suffix))
                if hits:
                    data = archive.read(names[hits[0]])
                    if len(data) <= SPIROM_OFFSET:
                        raise RomfsError(f"{hits[0]} is a raw bitstream with no SPI ROM in it")
                    return _spirom_of(data, hits[0])
        raise RomfsError(f"{path.name}: zip has no spirom.img or *_ram_flash.bin")
    return _spirom_of(path.read_bytes(), path.name)


def read(image):
    """(ssbl bytes, modules, volume, root)."""
    ssbl_part, romfs_part = spirom.split_image(image)
    ssbl = ssbl_part[:spirom.ssbl_length(ssbl_part)]
    volume, root = spirom.parse_romfs(romfs_part)
    return ssbl, spirom.parse_toc(ssbl), volume, root


def _safe(name):
    if not name or name in (".", "..") or "/" in name or "\0" in name:
        raise RomfsError(f"ROMFS entry name {name!r} can't be written to disk")
    return name


def _module_name(module):
    spirom.describe_module(module)
    label = re.sub(r"[^A-Za-z0-9._-]+", "_", module.name or "module").strip("_") or "module"
    return f"{module.index:02d}_{label}"


def unpack(image, out):
    ssbl, modules, volume, root = read(image)
    out = Path(out)
    (out / "romfs").mkdir(parents=True, exist_ok=False)
    (out / "modules").mkdir()
    (out / "ssbl.bin").write_bytes(ssbl)
    (out / "volume.txt").write_text(volume + "\n")
    for module in modules:
        (out / "modules" / _module_name(module)).write_bytes(module.data)
    order = []

    def write(node, folder, prefix):
        for child in node.children:
            target = folder / _safe(child.name)
            rel = prefix + child.name
            order.append(("d" if child.is_dir() else "l" if child.kind == spirom.SYMLINK else "f")
                         + ("x" if child.executable else "-") + " " + rel)
            if child.is_dir():
                target.mkdir()
                write(child, target, rel + "/")
            elif child.kind == spirom.SYMLINK:
                os.symlink(child.data.decode("latin-1"), target)
            else:
                target.write_bytes(child.data)
                target.chmod(0o755 if child.executable else 0o644)
    write(root, out / "romfs", "")
    (out / ORDER).write_text("\n".join(order) + ("\n" if order else ""))
    return modules, root


# --- writing ---------------------------------------------------------------------

def _read_order(folder):
    path = Path(folder) / ORDER
    flags, order = {}, []
    if path.exists():
        for line in path.read_text().splitlines():
            if len(line) > 3 and line[2] == " ":
                order.append(line[3:])
                flags[line[3:]] = line[:2]
    return order, flags


def tree(folder):
    """Build a spirom.Node tree from DIR/romfs, in romfs.order order."""
    folder = Path(folder)
    base = folder / "romfs"
    if not base.is_dir():
        raise RomfsError(f"{base} is not a folder (run unpack first)")
    order, flags = _read_order(folder)
    rank = {path: i for i, path in enumerate(order)}

    def build(directory, prefix):
        entries = []
        for entry in directory.iterdir():
            if entry.name.startswith("._") or entry.name == ".DS_Store":
                continue                          # macOS litter
            rel = prefix + entry.name
            try:
                entry.name.encode("latin-1")
            except UnicodeEncodeError as error:
                raise RomfsError(f"{rel}: name must be Latin-1 for the Amiga") from error
            info = entry.lstat()
            if stat.S_ISLNK(info.st_mode):
                node = spirom.Node(entry.name, spirom.SYMLINK, False,
                                   data=os.readlink(entry).encode("latin-1"))
            elif stat.S_ISDIR(info.st_mode):
                node = spirom.Node(entry.name, spirom.DIRECTORY, flags.get(rel, "dx")[1] == "x",
                                   children=build(entry, rel + "/"))
            elif stat.S_ISREG(info.st_mode):
                node = spirom.Node(entry.name, spirom.REGULAR, bool(info.st_mode & 0o111),
                                   data=entry.read_bytes())
            else:
                raise RomfsError(f"{rel}: not a file, folder or symlink")
            entries.append((rank.get(rel, len(rank)), entry.name, node))
        return [node for _, _, node in sorted(entries, key=lambda e: (e[0], e[1]))]

    return spirom.Node("", spirom.DIRECTORY, True, children=build(base, ""))


def pack(folder, bitstream=None):
    folder = Path(folder)
    ssbl_path = folder / "ssbl.bin"
    if not ssbl_path.exists():
        raise RomfsError(f"{ssbl_path} is missing (run unpack first)")
    volume_path = folder / "volume.txt"
    volume = volume_path.read_text().strip() if volume_path.exists() else "TF4060"
    image = spirom.make_image(ssbl_path.read_bytes(), spirom.build_romfs(volume, tree(folder)))
    if bitstream is None:
        return image
    data = Path(bitstream).read_bytes()
    try:
        full = combine(data[:SPIROM_OFFSET].rstrip(b"\xff"), image)
    except FirmwareError as error:
        raise RomfsError(str(error)) from error
    if len(full) > MAX_FLASH_IMAGE:
        raise RomfsError(f"flash image is {len(full)} bytes, more than the {MAX_FLASH_IMAGE}-byte flash")
    return full


# --- command line ----------------------------------------------------------------

def _list(image):
    ssbl, modules, volume, root = read(image)
    print(f"SPI ROM: {len(image)} bytes, SSBL {len(ssbl)} bytes")
    print("Resident modules (boot order):")
    for module in modules:
        spirom.describe_module(module)
        ident = (module.idstring or "").strip()
        print(f"  {module.index:2d}  {len(module.data):7d}  {module.name or '?'}" + (f"  ({ident})" if ident else ""))
    print(f"ROMFS volume {volume!r}:")
    for path, node in spirom.walk(root):
        if node.is_dir():
            print(f"  {'':>7}  {path}/")
        else:
            print(f"  {len(node.data):7d}  {path}" + (" *" if node.executable else "")
                  + (" -> " + node.data.decode("latin-1") if node.kind == spirom.SYMLINK else ""))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="tfpi-romfs", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list", help="show the resident modules and ROMFS files")
    p.add_argument("image")
    p = sub.add_parser("unpack", help="unpack the SSBL, modules and ROMFS into a folder")
    p.add_argument("image")
    p.add_argument("dir")
    p = sub.add_parser("pack", help="build spirom.img (or a full flash image) from a folder")
    p.add_argument("dir")
    p.add_argument("out")
    p.add_argument("--bitstream", help="also put the bitstream from this file in front (full flash image)")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "list":
            _list(load(args.image))
        elif args.cmd == "unpack":
            if Path(args.dir).exists() and any(Path(args.dir).iterdir()):
                raise RomfsError(f"{args.dir} is not empty")
            Path(args.dir).mkdir(parents=True, exist_ok=True)
            modules, root = unpack(load(args.image), args.dir)
            files = sum(1 for _, n in spirom.walk(root) if not n.is_dir())
            print(f"{args.dir}: ssbl.bin, {len(modules)} modules, {files} ROMFS files")
        else:
            data = pack(args.dir, args.bitstream)
            Path(args.out).write_bytes(data)
            print(f"{args.out}: {len(data)} bytes" + (" (flash image)" if args.bitstream else " (spirom.img)"))
    except (RomfsError, spirom.SpiromError, OSError) as error:
        print(f"tfpi-romfs: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
