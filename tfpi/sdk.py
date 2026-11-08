"""TF4060 RAM FPGA SDK: review and customise the SSBL/ROMFS, build a flash image.

An SDK zip (tf4060v2/scripts/mksdk.sh) holds:
  tf4060r5_ram.asc, bootrom_syn.mif, fsbl.mif   routed design + boot ROM
  spirom.img                                    the release SSBL/ROMFS
  ssbl/*.o, ssbl/toc.i                          (newer SDKs) SSBL objects, so
                                                the TOC can be relinked

ROMFS changes need nothing else. TOC (resident module) changes relink the SSBL
with vasm + vlink exactly as tf4060v2/ssbl/Makefile does; the result is
checked by rebuilding the unchanged TOC first, which must give the SDK's own
SSBL byte for byte.
"""

import hashlib
import os
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import spirom
from .fwimage import SPIROM_OFFSET, combine

VASM = os.environ.get("TFPI_VASM", "vasmm68k_mot")
VLINK = os.environ.get("TFPI_VLINK", "vlink")
ICEBRAM = os.environ.get("TFPI_ICEBRAM", "icebram")
ICEPACK = os.environ.get("TFPI_ICEPACK", "icepack")

# The SSBL's link order (tf4060v2/ssbl/Makefile OBJECTS, toc.o last). SDKs list
# it in ssbl/objects.txt; this is the order of SDKs made before that file.
SSBL_OBJECTS = ("start.o", "main.o", "reloc.o", "kprintf.o", "spi.o", "spi_asm.o")
ASFLAGS = ["-quiet", "-Fhunk", "-kick1hunks", "-nosym", "-m68060", "-cnop=0x0000"]
LDFLAGS = ["-t", "-x", "-Bstatic", "-Bsymbolic", "-Cvbcc", "-s", "-b", "rawbin1", "-q", "-mrel"]
BOOTROM_SIZE = 1024


class SdkError(ValueError):
    pass


@dataclass
class Sdk:
    name: str
    board: str
    files: dict                      # basename -> bytes (top level)
    ssbl_objects: dict = field(default_factory=dict)   # name -> bytes, may be empty
    toc_i: bytes = None
    object_order: tuple = SSBL_OBJECTS                 # link order, from ssbl/objects.txt

    @property
    def can_relink(self):
        return (self.toc_i is not None and bool(self.object_order)
                and all(n in self.ssbl_objects for n in self.object_order))

    @property
    def spirom(self):
        return self.files["spirom.img"]


def is_sdk(path):
    try:
        with zipfile.ZipFile(path) as archive:
            names = {PurePosixPath(n).name for n in archive.namelist()}
    except zipfile.BadZipFile:
        return False
    return "spirom.img" in names and "bootrom_syn.mif" in names and any(n.endswith("_ram.asc") for n in names)


def load_sdk(path, name=None):
    files, objects, toc_i, order = {}, {}, None, None
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir() or info.file_size > 16 * 1024 * 1024:
                continue
            parts = PurePosixPath(info.filename).parts
            base = parts[-1]
            if len(parts) >= 2 and parts[-2] == "ssbl":
                if base.endswith(".o"):
                    objects[base] = archive.read(info)
                elif base == "toc.i":
                    toc_i = archive.read(info)
                elif base == "objects.txt":
                    order = tuple(archive.read(info).decode("ascii", "replace").split())
            elif base.endswith((".asc", ".mif", ".img", ".md")):
                files[base] = archive.read(info)
    asc = [n for n in files if n.endswith("_ram.asc")]
    if len(asc) != 1 or "spirom.img" not in files or "bootrom_syn.mif" not in files:
        raise SdkError("not a TF4060 SDK zip (needs *_ram.asc, bootrom_syn.mif, spirom.img)")
    if order is not None and any(n == "toc.o" or not n.endswith(".o") for n in order):
        raise SdkError("ssbl/objects.txt in this SDK is not a list of SSBL objects")
    return Sdk(name=name or Path(path).name, board=asc[0][:-len("_ram.asc")], files=files,
               ssbl_objects=objects, toc_i=toc_i, object_order=order or SSBL_OBJECTS)


# --- review ------------------------------------------------------------------

@dataclass
class Contents:
    ssbl: bytes                  # exact SSBL (strap) bytes
    modules: list                # spirom.Module, boot order
    volume: str
    romfs: object                # spirom.Node root

    def size(self):
        return len(self.image())

    def image(self):
        return spirom.make_image(self.ssbl, spirom.build_romfs(self.volume, self.romfs))


def read_spirom(image):
    ssbl_part, romfs_part = spirom.split_image(image)
    ssbl = ssbl_part[:spirom.ssbl_length(ssbl_part)]
    volume, root = spirom.parse_romfs(romfs_part)
    return Contents(ssbl=ssbl, modules=spirom.parse_toc(ssbl), volume=volume, romfs=root)


def spirom_limit(flash_bytes):
    return flash_bytes - SPIROM_OFFSET


# --- SSBL relink ---------------------------------------------------------------

def _missing(tool):
    return SdkError(f"{Path(str(tool)).name} is not installed on this Pi; run 'sudo tfpi-update'")


def _run(cmd, cwd):
    try:
        result = subprocess.run([str(c) for c in cmd], cwd=cwd, capture_output=True, text=True)
    except FileNotFoundError as error:
        raise _missing(cmd[0]) from error
    if result.returncode:
        raise SdkError(f"{Path(str(cmd[0])).name} failed: {(result.stdout + result.stderr).strip()[-400:]}")


def link_ssbl(sdk, modules):
    """SSBL bytes for `modules` (list of hunk-file bytes, boot order)."""
    if not sdk.can_relink:
        raise SdkError("this SDK has no SSBL objects (ssbl/*.o), so resident modules can't be "
                       "changed; ROMFS changes still work")
    with tempfile.TemporaryDirectory(prefix="tfpi-ssbl-") as tmp:
        work = Path(tmp)
        for name, data in sdk.ssbl_objects.items():
            (work / name).write_bytes(data)
        (work / "toc.i").write_bytes(sdk.toc_i)
        lines = ['    include "toc.i"', "    TOC_BEGIN"]
        for index, data in enumerate(modules):
            spirom.check_hunk(data)
            (work / f"m{index:02d}.bin").write_bytes(data)
            lines.append(f'    tocentry "m{index:02d}.bin"')
        lines.append("    TOC_END")
        (work / "toc.s").write_text("\n".join(lines) + "\n")
        _run([VASM, "toc.s", "-o", "toc.o", *ASFLAGS, "-I."], work)
        _run([VLINK, *LDFLAGS, *sdk.object_order, "toc.o", "-o", "spirom.strap"], work)
        return spirom.add_crc((work / "spirom.strap").read_bytes())


def check_relink(sdk, contents):
    """Relinking the SDK's own TOC must reproduce its SSBL exactly."""
    rebuilt = link_ssbl(sdk, [m.data for m in contents.modules])
    if rebuilt != contents.ssbl:
        raise SdkError("relinking the SDK's own TOC does not reproduce its SSBL; the SSBL "
                       "objects don't match spirom.img or the vasm/vlink version differs")


# --- bitstream -------------------------------------------------------------------

def _mif_lines(data):
    return [line.strip() for line in data.decode("ascii").splitlines() if line.strip()]


def build_bitstream(sdk, cache_dir=None):
    """The release bitstream: the SDK's fsbl.mif swapped into the routed design
    (icebram), packed (icepack). Same as the SDK Makefile."""
    fsbl = _mif_lines(sdk.files["fsbl.mif"])
    if len(fsbl) > BOOTROM_SIZE:
        raise SdkError("fsbl.mif is larger than the 1024-byte boot ROM")
    fsbl += ["ff"] * (BOOTROM_SIZE - len(fsbl))
    asc_name = f"{sdk.board}_ram.asc"
    key = hashlib.sha256(sdk.files[asc_name] + sdk.files["bootrom_syn.mif"] + "\n".join(fsbl).encode()).hexdigest()
    if cache_dir:
        cached = Path(cache_dir) / f"{key}.bin"
        if cached.exists():
            return cached.read_bytes()
    with tempfile.TemporaryDirectory(prefix="tfpi-bit-") as tmp:
        work = Path(tmp)
        (work / "bootrom_syn.mif").write_bytes(sdk.files["bootrom_syn.mif"])
        (work / "fsbl_full.mif").write_text("\n".join(fsbl) + "\n")
        with open(work / "design.asc", "wb") as handle:
            handle.write(sdk.files[asc_name])
        with open(work / "design.asc", "rb") as src, open(work / "rom.asc", "wb") as dst:
            try:
                result = subprocess.run([ICEBRAM, "bootrom_syn.mif", "fsbl_full.mif"], cwd=work,
                                        stdin=src, stdout=dst, stderr=subprocess.PIPE, text=False)
            except FileNotFoundError as error:
                raise _missing(ICEBRAM) from error
        if result.returncode:
            raise SdkError("icebram failed: " + result.stderr.decode(errors="replace")[-300:])
        _run([ICEPACK, "rom.asc", "bitstream.bin"], work)
        bitstream = (work / "bitstream.bin").read_bytes()
    if cache_dir:
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        (Path(cache_dir) / f"{key}.bin").write_bytes(bitstream)
    return bitstream


def flash_image(bitstream, spirom_image, flash_bytes):
    if len(spirom_image) > spirom_limit(flash_bytes):
        raise SdkError(f"SPI ROM image is {len(spirom_image)} bytes; the flash leaves room for "
                       f"{spirom_limit(flash_bytes)} after the bitstream")
    return combine(bitstream, spirom_image)
