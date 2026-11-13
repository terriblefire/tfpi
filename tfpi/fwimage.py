"""Read and check a TF firmware release zip.

A release zip holds:
  <board>_ram_flash.bin RAM FPGA image (or <board>_ram.bin): either the full
                        SPI flash image (bitstream + SSBL/ROMFS at 0x21000) or
                        a raw bitstream (flashed as it is, with a warning, when
                        there is no spirom.img)
  *.jed                 BUS CPLD. One .jed is used whatever its name; with
                        several, _bus_prod, then _bus_top, then _bus wins
  seed.txt              optional, nextpnr seed (else "seed N" in README.md)
  spirom.img            optional, SSBL/ROMFS for raw-bitstream releases

Combine/validate logic is copied from tf4060/boards/tf4060r5/build_flash_image.py.
"""

import hashlib
import re
import struct
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath

SPIROM_OFFSET = 0x21000
PREAMBLE = bytes.fromhex("ff0000ff7eaa997e")

MAX_ZIP_MEMBER = 16 * 1024 * 1024
MAX_FLASH_IMAGE = 16 * 1024 * 1024


class FirmwareError(ValueError):
    pass


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def validate_spirom(payload):
    if len(payload) < 512 or payload[510:512] != b"\x55\xaa":
        raise FirmwareError("SPI image has no valid MBR signature")
    partitions = {}
    ranges = []
    for index in range(4):
        entry = payload[446 + 16 * index:462 + 16 * index]
        kind = entry[4]
        if not kind:
            continue
        start, sectors = struct.unpack_from("<II", entry, 8)
        lo, hi = start * 512, (start + sectors) * 512
        if not sectors or lo < 512 or hi > len(payload):
            raise FirmwareError("SPI partition lies outside the image")
        if any(lo < end and begin < hi for begin, end in ranges):
            raise FirmwareError("SPI partitions overlap")
        if kind in partitions:
            raise FirmwareError("SPI image has duplicate partition types")
        ranges.append((lo, hi))
        partitions[kind] = payload[lo:hi]
    if 0x68 not in partitions or 0x69 not in partitions:
        raise FirmwareError("SPI image must contain SSBL (0x68) and ROMFS (0x69)")
    romfs = partitions[0x69]
    if romfs[:8] != b"-rom1fs-":
        raise FirmwareError("ROMFS magic does not match")
    size = struct.unpack_from(">I", romfs, 8)[0]
    if size < 16 or size > len(romfs):
        raise FirmwareError("ROMFS size lies outside its partition")
    header = romfs[:min(size, 512)]
    if len(header) % 4 or sum(struct.unpack(f">{len(header) // 4}I", header)) & 0xffffffff:
        raise FirmwareError("ROMFS header checksum does not match")


def combine(bitstream, payload):
    if not bitstream.startswith(PREAMBLE):
        raise FirmwareError("bitstream has no iCE40 preamble/sync word")
    if len(bitstream) > SPIROM_OFFSET:
        raise FirmwareError("bitstream overlaps the SSBL at 0x21000")
    validate_spirom(payload)
    return bitstream + b"\xff" * (SPIROM_OFFSET - len(bitstream)) + payload


def check_jed(data):
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as error:
        raise FirmwareError("CPLD file is not an ASCII JEDEC file") from error
    stx, etx = text.find("\x02"), text.rfind("\x03")
    if stx < 0 or etx < stx or "QF" not in text[stx:etx]:
        raise FirmwareError("CPLD file has no JEDEC body (STX/QF/ETX)")
    device = None
    for line in text.splitlines():
        if line.startswith("N DEVICE "):
            device = line[len("N DEVICE "):].rstrip("*").strip()
    return device


@dataclass
class Release:
    name: str
    board: str
    seed: str = ""
    fpga_image: bytes = b""
    fpga_source: str = ""        # "full image" or "raw bitstream + spirom.img"
    fpga_file: str = ""
    cpld_file: str = None
    cpld_jed: bytes = None
    cpld_device: str = None
    a314_commit: str = ""        # the a314 the SPI ROM was built from (README.md)
    members: list = field(default_factory=list)
    warnings: list = field(default_factory=list)   # shown before flashing

    @property
    def fpga_sha(self):
        return sha256(self.fpga_image)

    @property
    def cpld_sha(self):
        return sha256(self.cpld_jed) if self.cpld_jed is not None else None

    def summary(self):
        return {
            "name": self.name,
            "board": self.board,
            "seed": self.seed,
            "fpga_sha": self.fpga_sha,
            "fpga_bytes": len(self.fpga_image),
            "fpga_source": self.fpga_source,
            "fpga_file": self.fpga_file,
            "cpld_file": self.cpld_file,
            "cpld_sha": self.cpld_sha,
            "cpld_device": self.cpld_device,
            "a314_commit": self.a314_commit,
            "members": self.members,
            "warnings": self.warnings,
        }


def _read_members(path):
    files = {}
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                base = PurePosixPath(info.filename).name
                if not base or base.startswith(".") or "__MACOSX" in info.filename:
                    continue
                if info.file_size > MAX_ZIP_MEMBER:
                    raise FirmwareError(f"{base} is too large ({info.file_size} bytes)")
                if base in files:
                    raise FirmwareError(f"{base} appears twice in the zip")
                files[base] = archive.read(info)
    except zipfile.BadZipFile as error:
        raise FirmwareError("not a valid zip file") from error
    return files


def _pick(files, suffixes, what):
    for suffix in suffixes:
        hits = sorted(name for name in files if name.endswith(suffix))
        if len(hits) > 1:
            raise FirmwareError(f"more than one {what} file: {', '.join(hits)}")
        if hits:
            return hits[0]
    return None


def _pick_jed(files):
    jeds = sorted(name for name in files if name.lower().endswith(".jed"))
    if len(jeds) <= 1:
        return jeds[0] if jeds else None
    for suffix in ("_bus_prod.jed", "_bus_top.jed", "_bus.jed"):
        hits = [name for name in jeds if name.endswith(suffix)]
        if len(hits) == 1:
            return hits[0]
    raise FirmwareError(f"cannot tell which CPLD file to use: {', '.join(jeds)}")


def load_release(path, name=None, board=None, spirom=None):
    """Parse a release zip. `board` (e.g. tf4060r5) is enforced when given.
    `spirom` is the fallback SSBL/ROMFS bytes for raw-bitstream releases."""
    files = _read_members(path)
    ram_name = _pick(files, ("_ram_flash.bin", "_ram.bin"), "RAM FPGA image")
    if ram_name is None:
        raise FirmwareError("zip has no *_ram_flash.bin or *_ram.bin FPGA image")
    zip_board = ram_name.rsplit("_ram", 1)[0]
    if board and zip_board != board:
        raise FirmwareError(f"zip is for board {zip_board!r}, this Pi is set up for {board!r}")

    release = Release(name=name or PurePosixPath(str(path)).name, board=zip_board,
                      members=sorted(files))
    release.fpga_file = ram_name
    release.seed = files.get("seed.txt", b"").decode("ascii", "replace").strip()
    readme = files.get("README.md", b"").decode("utf-8", "replace")
    if not release.seed and readme:
        found = re.search(r"seed\s+(\d+)", readme, re.I)
        release.seed = found.group(1) if found else ""
    # "a314 (a314.device, ...) built in ... from https://github.com/erique/a314 tf4060 at <commit>"
    found = re.search(r"^.*\ba314\b.*\bat ([0-9a-f]{7,40})\b", readme, re.M)
    release.a314_commit = found.group(1) if found else ""

    ram = files[ram_name]
    if not ram.startswith(PREAMBLE):
        raise FirmwareError(f"{ram_name} does not start with an iCE40 bitstream")
    if len(ram) > MAX_FLASH_IMAGE:
        raise FirmwareError(f"{ram_name} is larger than the SPI flash")
    if len(ram) > SPIROM_OFFSET:
        validate_spirom(ram[SPIROM_OFFSET:])
        release.fpga_image = ram
        release.fpga_source = "full image"
    else:
        payload = files.get("spirom.img", spirom)
        if payload is None:
            release.fpga_image = ram
            release.fpga_source = "raw bitstream"
            release.warnings.append(
                f"{ram_name} is a raw bitstream and there is no spirom.img (in the zip or in "
                "/etc/tfpi/spirom.img): only the bitstream is flashed, with no SSBL/ROMFS.")
        else:
            release.fpga_image = combine(ram, payload)
            release.fpga_source = "raw bitstream + spirom.img" + (
                " (from zip)" if "spirom.img" in files else " (from /etc/tfpi)")

    jed_name = _pick_jed(files)
    if jed_name is not None:
        release.cpld_file = jed_name
        release.cpld_jed = files[jed_name]
        release.cpld_device = check_jed(release.cpld_jed)
    return release
