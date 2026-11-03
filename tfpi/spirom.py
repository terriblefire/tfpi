"""Read and rebuild the TF4060 SPI ROM image (spirom.img, flashed at 0x21000).

spirom.img is an MBR with two partitions (tf4060v2/ssbl/tools/mkspirom.py):
  type 0x68  the SSBL ("spirom.strap"): code, then the TOC (resident modules
             the SSBL loads at boot), data, a relocation table and a CRC word
  type 0x69  the ROMFS (Linux romfs, made by genromfs), mounted as TF4060:

Reading needs nothing else. Changing the TOC means relinking the SSBL, which
needs the SSBL objects from the SDK (see sdk.py); changing only the ROMFS does
not touch the SSBL.
"""

import binascii
import re
import struct
from dataclasses import dataclass, field

SECTOR = 512
HUNK_HEADER = 0x3F3
RTC_MATCHWORD = 0x4AFC


class SpiromError(ValueError):
    pass


def crc32(data):
    return binascii.crc32(data) & 0xFFFFFFFF


# --- MBR -------------------------------------------------------------------

def _chs(lba, heads=255, spt=63):
    c, rem = divmod(lba, heads * spt)
    h, s = divmod(rem, spt)
    if c > 1023:
        c, h, s = 1023, heads - 1, spt - 1
    s += 1
    return bytes([h, (s & 0x3F) | ((c >> 2) & 0xC0), c & 0xFF])


def _entry(ptype, lba, count):
    return bytes([0]) + _chs(lba) + bytes([ptype]) + _chs(lba + count - 1) + struct.pack("<II", lba, count)


def make_image(ssbl, romfs):
    """Same bytes as `mkspirom.py image`."""
    n1 = (len(ssbl) + SECTOR - 1) // SECTOR
    n2 = (len(romfs) + SECTOR - 1) // SECTOR
    mbr = bytearray(SECTOR)
    struct.pack_into("<I", mbr, 440, crc32(ssbl + romfs))
    mbr[446:462] = _entry(0x68, 1, n1)
    mbr[462:478] = _entry(0x69, 1 + n1, n2)
    mbr[510:512] = b"\x55\xaa"
    return bytes(mbr) + ssbl.ljust(n1 * SECTOR, b"\0") + romfs.ljust(n2 * SECTOR, b"\0")


def add_crc(data):
    """Same as `mkspirom.py crc32`: append the word that makes the CRC 0xFFFFFFFF."""
    data = data + struct.pack("<I", ~crc32(data) & 0xFFFFFFFF)
    assert crc32(data) == 0xFFFFFFFF
    return data


def split_image(image):
    """(ssbl partition bytes, romfs partition bytes), both sector padded."""
    if len(image) < SECTOR or image[510:512] != b"\x55\xaa":
        raise SpiromError("SPI ROM image has no MBR")
    parts = {}
    for index in range(4):
        entry = image[446 + 16 * index:462 + 16 * index]
        if entry[4]:
            lba, count = struct.unpack_from("<II", entry, 8)
            parts[entry[4]] = image[lba * SECTOR:(lba + count) * SECTOR]
    if 0x68 not in parts or 0x69 not in parts:
        raise SpiromError("SPI ROM image needs an SSBL (0x68) and a ROMFS (0x69) partition")
    return parts[0x68], parts[0x69]


# --- SSBL and its TOC ------------------------------------------------------

@dataclass
class Module:
    index: int
    data: bytes              # the hunk file, as incbin'd by toc.s
    name: str = ""           # rt_Name of the first Resident, if found
    idstring: str = ""       # rt_IdString
    version: int = None

    @property
    def size(self):
        return len(self.data)


def ssbl_length(part):
    """Exact SSBL length inside its sector-padded partition: the shortest
    prefix past the TOC end marker whose CRC-32 is 0xFFFFFFFF (zero padding
    keeps that residue, so search upwards)."""
    mark = part.rfind(b"\xde\xad\xbe\xef")
    if mark < 0:
        raise SpiromError("SSBL has no TOC end marker")
    for length in range(mark + 8, len(part) + 1):
        if crc32(part[:length]) == 0xFFFFFFFF:
            return length
    raise SpiromError("SSBL CRC does not check out")


def parse_toc(ssbl):
    """The TOC modules, in boot order. The TOC sits between TOC_BEGIN
    (-1, 0, 0) and TOC_END (0, count, $deadbeef); entries are
    {next, size, 0, 0, data[size], crc, 0, 0} with next an SSBL offset."""
    end_mark = ssbl.rfind(b"\xde\xad\xbe\xef")
    if end_mark < 8:
        raise SpiromError("SSBL has no TOC end marker")
    count = struct.unpack_from(">I", ssbl, end_mark - 4)[0]
    terminator = end_mark - 8
    begin = b"\xff\xff\xff\xff" + b"\0" * 8
    start = ssbl.find(begin)
    while start >= 0:
        modules = _walk(ssbl, start + 12, terminator)
        if modules is not None and len(modules) == count:
            return modules
        start = ssbl.find(begin, start + 2)
    raise SpiromError("could not find the SSBL TOC")


def _walk(ssbl, offset, terminator):
    modules = []
    while offset != terminator:
        if offset < 0 or offset + 16 > len(ssbl):
            return None
        nxt, size = struct.unpack_from(">II", ssbl, offset)
        if nxt != offset + 16 + size + 12:
            return None
        module = Module(index=len(modules), data=ssbl[offset + 16:offset + 16 + size])
        describe_module(module)
        modules.append(module)
        offset = nxt
        if len(modules) > 64:
            return None
    return modules


def check_hunk(data):
    if len(data) < 24 or struct.unpack_from(">I", data, 0)[0] != HUNK_HEADER:
        raise SpiromError("not an AmigaOS executable (no HUNK_HEADER)")


def hunk_bodies(data):
    """Code/data hunk bodies of an AmigaOS hunk file (HUNK_CODE/DATA)."""
    check_hunk(data)
    first, last = struct.unpack_from(">II", data, 12)
    count = last - first + 1
    pos = 20 + 4 * count
    bodies = []
    while pos + 8 <= len(data) and len(bodies) < count:
        kind = struct.unpack_from(">I", data, pos)[0] & 0x3FFFFFFF
        if kind == 0x3EB:                         # HUNK_BSS: no body
            bodies.append(b"")
            pos += 8
        elif kind in (0x3E9, 0x3EA):              # HUNK_CODE, HUNK_DATA
            longs = struct.unpack_from(">I", data, pos + 4)[0] & 0x3FFFFFFF
            bodies.append(data[pos + 8:pos + 8 + 4 * longs])
            pos += 8 + 4 * longs
        else:
            break
        # skip reloc/symbol blocks up to HUNK_END
        while pos + 4 <= len(data):
            word = struct.unpack_from(">I", data, pos)[0] & 0x3FFFFFFF
            pos += 4
            if word == 0x3F2:                     # HUNK_END
                break
            if word in (0x3EC, 0x3ED, 0x3EE):     # RELOC32/16/8: count, hunk, offsets
                while True:
                    n = struct.unpack_from(">I", data, pos)[0]
                    pos += 4
                    if not n:
                        break
                    pos += 4 + 4 * n
            elif word == 0x3F7:                   # RELOC32SHORT: 16-bit words
                start = pos
                while True:
                    n = struct.unpack_from(">H", data, pos)[0]
                    pos += 2
                    if not n:
                        break
                    pos += 2 + 2 * n
                pos = (pos + 3) & ~3
            elif word == 0x3F0:                   # HUNK_SYMBOL
                while True:
                    n = struct.unpack_from(">I", data, pos)[0]
                    pos += 4
                    if not n:
                        break
                    pos += 4 * (n & 0xFFFFFF) + 4
            elif word == 0x3F1:                   # HUNK_DEBUG
                n = struct.unpack_from(">I", data, pos)[0]
                pos += 4 + 4 * n
            else:
                return bodies
    return bodies


def describe_module(module):
    """Fill in name/idstring/version from the module's Resident (ROMTag): a
    RTC_MATCHWORD whose rt_MatchTag points back at it (pointers are
    hunk-relative before relocation). module.name stays "" when there is no
    ROMTag; the SSBL can't start such a file."""
    try:
        bodies = hunk_bodies(module.data)
    except (SpiromError, struct.error):
        return
    for body in bodies:
        for at in range(0, len(body) - 26, 2):
            if struct.unpack_from(">H", body, at)[0] != RTC_MATCHWORD:
                continue
            if struct.unpack_from(">I", body, at + 2)[0] != at:
                continue
            name_ptr, id_ptr = struct.unpack_from(">II", body, at + 14)
            module.version = body[at + 11]
            module.name = _cstring(body, name_ptr)
            module.idstring = _cstring(body, id_ptr).strip()
            return
    found = re.search(rb"\$VER: ([ -~]{3,80})", module.data)
    if found:
        module.idstring = found.group(1).decode("latin-1")


def has_romtag(data):
    module = Module(index=0, data=data)
    describe_module(module)
    return bool(module.name)


def _cstring(body, at):
    if not 0 <= at < len(body):
        return ""
    end = body.find(b"\0", at)
    return body[at:end if end >= 0 else len(body)].decode("latin-1", "replace")[:80]


# --- ROMFS (Linux romfs, genromfs layout) ----------------------------------

ROMFS_MAGIC = b"-rom1fs-"
HARDLINK, DIRECTORY, REGULAR, SYMLINK = 0, 1, 2, 3


@dataclass
class Node:
    name: str
    kind: int
    executable: bool = False
    data: bytes = b""                            # file contents / symlink target
    children: list = field(default_factory=list)  # directories, in image order

    def is_dir(self):
        return self.kind == DIRECTORY


def _pad16(n):
    return (n + 15) & ~15


def _name_field(name):
    raw = name.encode("latin-1") + b"\0"
    return raw.ljust(_pad16(len(raw)), b"\0")


def _sum(data):
    data = data.ljust((len(data) + 3) & ~3, b"\0")
    return sum(struct.unpack(f">{len(data) // 4}I", data)) & 0xFFFFFFFF


def parse_romfs(image):
    """(volume name, root Node). The '.' and '..' entries are dropped; the
    writer recreates them."""
    if image[:8] != ROMFS_MAGIC:
        raise SpiromError("ROMFS magic does not match")
    size = struct.unpack_from(">I", image, 8)[0]
    if size > len(image):
        raise SpiromError("ROMFS is truncated")
    if _sum(image[:min(size, 512)]):
        raise SpiromError("ROMFS header checksum does not match")
    volume_end = image.index(b"\0", 16)
    volume = image[16:volume_end].decode("latin-1")
    first = 16 + _pad16(volume_end - 16 + 1)
    root = Node("", DIRECTORY, True, children=_read_dir(image, first, size, 0))
    return volume, root


def _read_dir(image, offset, size, depth):
    if depth > 32:
        raise SpiromError("ROMFS directories nest too deep")
    nodes, seen = [], set()
    while offset:
        if offset in seen or offset + 16 > size:
            raise SpiromError("ROMFS entry chain is broken")
        seen.add(offset)
        nxt, spec, length, _ = struct.unpack_from(">IIII", image, offset)
        name_end = image.index(b"\0", offset + 16)
        name = image[offset + 16:name_end].decode("latin-1")
        data_at = offset + 16 + _pad16(name_end - offset - 16 + 1)
        kind, executable = nxt & 7, bool(nxt & 8)
        offset = nxt & ~15
        if name in (".", ".."):
            continue
        if kind == DIRECTORY:
            nodes.append(Node(name, DIRECTORY, executable,
                              children=_read_dir(image, spec, size, depth + 1)))
        elif kind in (REGULAR, SYMLINK):
            if data_at + length > size:
                raise SpiromError(f"ROMFS file {name} runs past the image")
            nodes.append(Node(name, kind, executable, data=image[data_at:data_at + length]))
        else:
            raise SpiromError(f"ROMFS entry {name} has unsupported type {kind}")
    return nodes


def build_romfs(volume, root):
    """A romfs image in genromfs's layout: each directory's entries in order,
    '.' and '..' first, a subdirectory's contents straight after its header,
    16-byte alignment, total padded to 1 KB. Re-serialising a parsed genromfs
    image gives the same bytes."""
    out = bytearray(ROMFS_MAGIC + b"\0" * 8 + _name_field(volume))

    def header(kind, executable, spec, size, name):
        at = len(out)
        out.extend(struct.pack(">IIII", kind | (8 if executable else 0), spec, size, 0))
        out.extend(_name_field(name))
        return at

    def chain(prev, nxt):
        word = struct.unpack_from(">I", out, prev)[0]
        struct.pack_into(">I", out, prev, (word & 15) | nxt)

    def write_dir(node, dot, own_header, parent_header):
        """`dot` is this directory's '.' entry (already written); own_header
        is what '.' points at, parent_header what '..' points at."""
        prev = header(HARDLINK, False, parent_header, 0, "..")
        chain(dot, prev)
        for child in node.children:
            here = len(out)
            chain(prev, here)
            prev = here
            if child.is_dir():
                header(DIRECTORY, child.executable, 0, 0, child.name)
                child_dot = header(HARDLINK, False, here, 0, ".")
                struct.pack_into(">I", out, here + 4, child_dot)
                write_dir(child, child_dot, here, own_header)
            else:
                header(child.kind, child.executable, 0, len(child.data), child.name)
                out.extend(child.data)
                out.extend(b"\0" * (_pad16(len(out)) - len(out)))

    root_dot = len(out)
    header(DIRECTORY, root.executable, root_dot, 0, ".")   # the root is its own '.'
    write_dir(root, root_dot, root_dot, root_dot)
    struct.pack_into(">I", out, 8, len(out))      # genromfs records the unpadded size
    out.extend(b"\0" * (((len(out) + 1023) & ~1023) - len(out)))
    _fix_checksums(out, root_dot)
    # the superblock checksum covers the first 512 bytes, file headers included
    struct.pack_into(">I", out, 12, -_sum(bytes(out[:min(512, struct.unpack_from(">I", out, 8)[0])])) & 0xFFFFFFFF)
    return bytes(out)


def _fix_checksums(out, first):
    """Header checksums: each file header plus its name sums to 0."""
    todo, seen = [first], set()
    while todo:
        offset = todo.pop()
        while offset and offset not in seen:
            seen.add(offset)
            nxt, spec = struct.unpack_from(">II", out, offset)
            name_end = out.index(b"\0", offset + 16)
            header_len = 16 + _pad16(name_end - offset - 16 + 1)
            struct.pack_into(">I", out, offset + 12, 0)
            struct.pack_into(">I", out, offset + 12,
                             -_sum(bytes(out[offset:offset + header_len])) & 0xFFFFFFFF)
            if nxt & 7 == DIRECTORY and bytes(out[offset + 16:name_end]) not in (b".", b".."):
                todo.append(spec)
            offset = nxt & ~15


def walk(root, prefix=""):
    """Yield (path, node) for every file and directory, depth first."""
    for child in root.children:
        path = f"{prefix}{child.name}"
        yield path, child
        if child.is_dir():
            yield from walk(child, path + "/")


def find(root, path):
    node = root
    for part in [p for p in path.strip("/").split("/") if p]:
        match = [c for c in node.children if c.name.lower() == part.lower()]
        if not match:
            return None
        node = match[0]
    return node
