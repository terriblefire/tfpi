"""Which firmware is really on the board, read from the chips themselves.

tfpi's state.json only knows what tfpi itself flashed; a board flashed any other
way (flash_ram.sh / flash_bus.sh, another Pi) is invisible to it, and the CPLD
usercode is the same ("tf40") for every build. So:

  * RAM FPGA: the SPI flash image is read back (fomu-flash -b/-s, read only) and
    identified by
      bitstream_sha256  sha256 of flash[0:0x21000] with trailing 0xFF removed
                        (the routed design; same for a full image and a raw
                        bitstream release)
      image_sha256      sha256 of the whole image (bitstream + SSBL/ROMFS, its
                        length from the SPI ROM partition table); equals tfpi's
                        fpga sha when tfpi flashed it
      spirom stamp      "SPI ROM: <date> UTC, git <hash>" built into the ROMFS
  * BUS CPLD: the fuse map is read back (cpldread.py) and identified by
      fuse_sha256       sha256 of the fuse bits as a '0'/'1' string, QF long
    `tfdiag fwid --jed FILE` prints the same hash for any release JED, so a dump
    can be matched against every build at home, not just the ones on this Pi.
"""

import hashlib
import json
import re
import struct
import zipfile
from pathlib import Path

SPIROM_OFFSET = 0x21000
ICE40_SYNC = bytes.fromhex("7EAA997E")
STAMP = re.compile(rb"SPI ROM:\s+(\d{4}-\d\d-\d\d \d\d:\d\d) UTC, git ([0-9A-Za-z._+-]{4,40})")
MAX_MEMBER = 16 * 1024 * 1024


def sha256(data):
    return hashlib.sha256(data).hexdigest()


# ---- RAM FPGA flash image ---------------------------------------------------------------
def image_length(data):
    """Bytes in the flash image: 0x21000 + the end of the last SPI ROM partition, or None
    when there is no partition table there."""
    p = data[SPIROM_OFFSET:SPIROM_OFFSET + 512]
    if len(p) < 512 or p[510:512] != b"\x55\xaa":
        return None
    end = 0
    for i in range(4):
        e = p[446 + 16 * i:462 + 16 * i]
        if not e[4]:
            continue
        start, sectors = struct.unpack_from("<II", e, 8)
        end = max(end, (start + sectors) * 512)
    return SPIROM_OFFSET + end if end else None


def bitstream_of(data):
    return data[:SPIROM_OFFSET].rstrip(b"\xff")


def flash_ident(data):
    bs = bitstream_of(data)
    out = {"read_bytes": len(data), "bitstream": ICE40_SYNC in data[:64],
           "bitstream_bytes": len(bs), "bitstream_sha256": sha256(bs)}
    n = image_length(data)
    if n:
        out["image_bytes"] = n
        if n <= len(data):
            out["image_sha256"] = sha256(data[:n])
            out["spirom_sha256"] = sha256(data[SPIROM_OFFSET:n])
        else:
            out["image_truncated"] = True
    m = STAMP.search(data[SPIROM_OFFSET:n or len(data)])
    if m:
        out["spirom_built"] = m.group(1).decode() + " UTC"
        out["spirom_git"] = m.group(2).decode()
    return out


# ---- CPLD fuse maps ---------------------------------------------------------------------
class JedError(ValueError):
    pass


def parse_jed(text):
    """JEDEC fuse file -> (QF, fuse bits as a '0'/'1' string). Fuses not listed take the
    F default. Accepts ISE's files and the CPLD readback (cpldread.py)."""
    if isinstance(text, bytes):
        text = text.decode("ascii", "replace")
    stx = text.find("\x02")
    if stx >= 0:
        text = text[stx + 1:]
    etx = text.find("\x03")
    if etx >= 0:
        text = text[:etx]
    qf, default, runs = None, "0", []
    for n, field in enumerate(text.split("*")):
        field = field.strip()
        if n == 0 and "\n" in field:          # design spec before the first field
            field = field.splitlines()[-1].strip()
        if not field:
            continue
        key = field[0]
        if field.startswith("QF"):
            qf = int(field[2:].strip())
        elif key == "F" and field[1:].strip() in ("0", "1"):
            default = field[1:].strip()
        elif key == "L":
            m = re.match(r"L(\d+)\s+([01\s]+)$", field, re.S)
            if not m:
                raise JedError(f"bad fuse field {field[:30]!r}")
            runs.append((int(m.group(1)), re.sub(r"\s", "", m.group(2))))
    if not runs:
        raise JedError("no fuse data (L fields)")
    if qf is None:
        qf = max(a + len(b) for a, b in runs)
    bits = bytearray(default.encode() * qf)
    for addr, b in runs:
        if addr + len(b) > qf:
            raise JedError("fuse data past QF")
        bits[addr:addr + len(b)] = b.encode()
    return qf, bits.decode()


def fuse_ident(text):
    qf, bits = parse_jed(text)
    return {"fuses": qf, "fuse_sha256": sha256(bits.encode()), "ones": bits.count("1")}, bits


def distance(a, b):
    if len(a) != len(b):
        return None
    return bin(int(a, 2) ^ int(b, 2)).count("1")


# ---- boot ROM (FSBL) version from the bitstream's BRAM ----------------------------------------
# The FSBL (rom/bootrom.asm) carries "$VER:<board>r<rev>_<date>_<git>" (rom/version.inc), and
# icebram puts it into the bitstream's block RAM. The 1 KB byte-wide ROM is two SB_RAM40_4K
# in 1024x4 mode, one nibble each; in that mode physical bit c of row r holds nibble bit
# NB[c] of address r*4 + SEL[c & 3] (learned from a release, fixed by the RAM mode). Which
# two BRAMs hold it depends on placement, so every pair is tried.
BRAM_NB = (0, 0, 0, 0, 2, 2, 2, 2, 1, 1, 1, 1, 3, 3, 3, 3)
BRAM_SEL = (0, 2, 1, 3)
# since #36 (2026-10-26) the last field is the short git hash; before that a tag ("v2")
VER_RE = re.compile(rb"\$VER:([A-Za-z0-9_.-]+?)_(\d{4}-\d\d-\d\d)_([0-9A-Za-z]{1,40})")
GIT_RE = re.compile(r"^[0-9a-f]{7,40}$")
ASC_RAM = re.compile(r"\.ram_data (\d+) (\d+)\n((?:[0-9a-f]{64}\n){16})")


def asc_brams(asc):
    """.asc text (iceunpack) -> [256 16-bit words] per BRAM, in file order."""
    out = []
    for m in ASC_RAM.finditer(asc):
        w = []
        for line in m.group(3).split():
            for j in range(16):
                w.append(int(line[(15 - j) * 4:(16 - j) * 4], 16))
        out.append(w)
    return out


def _nibbles(words):
    out = [0] * 1024
    for c in range(16):
        nb, sel = 1 << BRAM_NB[c], BRAM_SEL[c & 3]
        for r in range(256):
            if (words[r] >> c) & 1:
                out[r * 4 + sel] |= nb
    return out


def boot_rom_version(asc):
    """{"name", "built", "tag", "git"} from the FSBL's $VER string in the bitstream's BRAM, or
    None. git is None for builds before #36, whose stamp ends in a tag."""
    nib = [_nibbles(w) for w in asc_brams(asc)]
    for hi in nib:
        if not any(hi):
            continue
        for lo in nib:
            if lo is hi:
                continue
            m = VER_RE.search(bytes((h << 4) | l for h, l in zip(hi, lo)))
            if m:
                tag = m.group(3).decode()
                return {"name": m.group(1).decode(), "built": m.group(2).decode(), "tag": tag,
                        "git": tag if GIT_RE.match(tag) else None}
    return None


# ---- known releases (tfpi/known_firmware.json) ------------------------------------------------
def load_catalog(path):
    """[{"name", "git", "date", "fpga": {"bitstream_sha256", "image_sha256"},
    "cpld": [{"file", "fuse_sha256"}]}] or []."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError, TypeError):
        return []
    rel = data.get("releases") if isinstance(data, dict) else None
    return [r for r in rel or [] if isinstance(r, dict) and r.get("name")]


def catalog_entry(name, image=None, jeds=(), git=None, date=None):
    """A known_firmware.json entry from release files (image bytes, [(file, jed text)])."""
    e = {"name": name, "git": git, "date": date, "fpga": None, "cpld": []}
    if image is not None:
        fi = flash_ident(image)
        e["fpga"] = {"bitstream_sha256": fi["bitstream_sha256"], "image_sha256": fi.get("image_sha256")}
        e["git"] = e["git"] or fi.get("spirom_git")
    for f, text in jeds:
        e["cpld"].append({"file": f, "fuse_sha256": fuse_ident(text)[0]["fuse_sha256"]})
    return e


# ---- what this Pi knows about --------------------------------------------------------------
def _read_member(z, name):
    info = z.getinfo(name)
    if info.file_size > MAX_MEMBER:
        return None
    return z.read(name)


def known_releases(firmware_dir):
    """Release zips tfpi keeps (rollback copies): per zip, the bitstream/image hashes and
    every JED's fuse map."""
    out = []
    for zp in sorted(Path(firmware_dir).glob("*.zip")) if firmware_dir else []:
        rel = {"name": zp.name, "jeds": []}
        try:
            with zipfile.ZipFile(zp) as z:
                names = z.namelist()
                ram = next((n for n in names if n.endswith("_ram_flash.bin")), None) or \
                    next((n for n in names if n.endswith("_ram.bin")), None)
                if ram:
                    data = _read_member(z, ram) or b""
                    rel["bitstream_sha256"] = sha256(bitstream_of(data))
                    if len(data) > SPIROM_OFFSET:
                        rel["image_sha256"] = sha256(data)
                for n in names:
                    if n.lower().endswith(".jed"):
                        raw = _read_member(z, n)
                        if raw is None:
                            continue
                        try:
                            fid, bits = fuse_ident(raw)
                        except JedError:
                            continue
                        rel["jeds"].append({"file": n, "jed_sha256": sha256(raw), **fid, "_bits": bits})
        except (OSError, zipfile.BadZipFile, KeyError) as e:
            rel["error"] = str(e)
        out.append(rel)
    return out


def tfpi_state(path):
    try:
        s = json.loads(Path(path).read_text())
    except (OSError, ValueError, TypeError):
        return None
    return {"release": s.get("release"), "seed": s.get("seed"),
            "fpga_sha256": (s.get("fpga") or {}).get("sha"), "fpga_time": (s.get("fpga") or {}).get("time"),
            "cpld_sha256": (s.get("cpld") or {}).get("sha"), "cpld_time": (s.get("cpld") or {}).get("time"),
            "history": [{k: h.get(k) for k in ("time", "release", "fpga", "cpld", "result")}
                        for h in (s.get("history") or [])[:10]]}


def identify(fpga=None, cpld=None, cpld_bits=None, releases=(), state=None, catalog=()):
    """Match what was read from the chips against the known releases (catalog) and the
    releases this Pi has, and say whether tfpi's own record still describes the board."""
    res = {"fpga": dict(fpga or {}), "cpld": dict(cpld or {}), "tfpi_state": state}
    f, c = res["fpga"], res["cpld"]
    if fpga:
        f["known"] = [r["name"] for r in catalog if (r.get("fpga") or {}).get("bitstream_sha256")
                      and r["fpga"]["bitstream_sha256"] == fpga.get("bitstream_sha256")]
        f["known_image"] = [r["name"] for r in catalog if (r.get("fpga") or {}).get("image_sha256")
                            and r["fpga"]["image_sha256"] == fpga.get("image_sha256")]
        f["matches"] = [r["name"] for r in releases
                        if r.get("bitstream_sha256") == fpga.get("bitstream_sha256")]
        if state and state.get("fpga_sha256") and fpga.get("image_sha256"):
            f["tfpi_record"] = "matches" if state["fpga_sha256"] == fpga["image_sha256"] else "differs"
    if cpld and cpld.get("fuse_sha256"):
        c["known"] = [r["name"] for r in catalog for j in r.get("cpld") or []
                      if j.get("fuse_sha256") == cpld["fuse_sha256"]]
    if cpld and cpld_bits:
        exact, near = [], []
        for r in releases:
            for j in r["jeds"]:
                d = distance(j["_bits"], cpld_bits)
                if d == 0:
                    exact.append(f"{r['name']}:{j['file']}")
                elif d is not None:
                    near.append({"release": f"{r['name']}:{j['file']}", "fuses_differ": d})
        c["matches"] = exact
        c["nearest"] = sorted(near, key=lambda x: x["fuses_differ"])[:3]
        if state and state.get("cpld_sha256"):
            rec = [j for r in releases for j in r["jeds"] if j["jed_sha256"] == state["cpld_sha256"]]
            if rec:
                c["tfpi_record"] = "matches" if rec[0]["fuse_sha256"] == cpld.get("fuse_sha256") else "differs"
            else:
                c["tfpi_record"] = "unknown (that release is no longer stored on this Pi)"
    return res


def verdict(ident):
    """What the Verify button shows: per chip {"state": "known" | "unknown" | "not read",
    "release", "git", "built", "detail"}."""
    f = (ident or {}).get("fpga") or {}
    c = (ident or {}).get("cpld") or {}
    out = {}
    if not f.get("bitstream_sha256"):
        out["fpga"] = {"state": "not read", "detail": f.get("error") or "the SPI flash could not be read"}
    else:
        rom = f.get("boot_rom") or {}
        known = f.get("known") or []
        v = {"state": "known" if known else "unknown", "release": known[0] if known else None,
             "git": rom.get("git"), "built": rom.get("built"), "spirom_git": f.get("spirom_git"),
             "spirom_built": f.get("spirom_built"), "bitstream": f["bitstream_sha256"]}
        if known:
            v["detail"] = ("release " + ", ".join(known)
                           + ("" if f.get("known_image") else "; its SPI ROM (SSBL/ROMFS) differs from the release's"))
        elif rom and rom.get("git"):
            v["detail"] = f"not a known release: built from git {rom['git']} on {rom['built']} (boot ROM)"
        elif rom:
            v["detail"] = (f"not a known release: built {rom['built']}, tag {rom.get('tag')} "
                           "(an older boot ROM without the git commit)")
        else:
            v["detail"] = "not a known release, and no build stamp found in its boot ROM"
        out["fpga"] = v
    if not c.get("fuse_sha256"):
        out["cpld"] = {"state": "not read", "detail": c.get("error") or "not read"}
    else:
        known = c.get("known") or []
        out["cpld"] = {"state": "known" if known else "unknown", "release": known[0] if known else None,
                       "fuses": c["fuse_sha256"],
                       "detail": ("design of release " + ", ".join(known)) if known else
                       "not a known release (the CPLD carries no build stamp; fuse checksum "
                       + c["fuse_sha256"][:16] + ")"}
    return out


def summary(ident):
    """One-line descriptions for the report: (fpga, cpld)."""
    f = (ident or {}).get("fpga") or {}
    c = (ident or {}).get("cpld") or {}
    fs = []
    if f.get("known"):
        fs.append("known release " + ", ".join(f["known"]))
    rom = f.get("boot_rom") or {}
    if rom.get("git"):
        fs.append(f"boot ROM git {rom['git']} ({rom['built']})")
    elif rom:
        fs.append(f"boot ROM {rom.get('name')} {rom['built']} tag {rom.get('tag')}")
    if f.get("spirom_git"):
        fs.append(f"SPI ROM git {f['spirom_git']} ({f.get('spirom_built')})")
    if f.get("bitstream_sha256"):
        fs.append(f"bitstream {f['bitstream_sha256'][:16]}")
    if f.get("image_sha256"):
        fs.append(f"image {f['image_sha256'][:16]} ({f.get('image_bytes')} bytes)")
    elif f.get("image_truncated"):
        fs.append("image longer than the bytes read")
    if f.get("matches"):
        fs.append("= " + ", ".join(f["matches"]))
    if f.get("tfpi_record"):
        fs.append(f"tfpi's record {f['tfpi_record']}")
    cs = []
    if c.get("fuse_sha256"):
        if c.get("known"):
            cs.append("known release " + ", ".join(c["known"]))
        cs.append(f"fuses {c['fuse_sha256'][:16]} ({c.get('fuses')} fuses)")
        if c.get("matches"):
            cs.append("= " + ", ".join(c["matches"]))
        elif c.get("nearest"):
            n = c["nearest"][0]
            cs.append(f"no stored release matches (nearest {n['release']}, {n['fuses_differ']} fuses differ)")
        if c.get("tfpi_record"):
            cs.append(f"tfpi's record {c['tfpi_record']}")
    elif c.get("error"):
        cs.append(f"not read: {c['error']}")
    return "; ".join(fs), "; ".join(cs)

