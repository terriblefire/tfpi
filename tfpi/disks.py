"""The disks a314scsi.device shows the Amiga: images on the Pi and scsi.conf.

a314's scsi service (/opt/a314/scsi.py) presents up to four SCSI units, each an
image file on the Pi named in /etc/opt/a314/scsi.conf: a hard disk image
(512-byte blocks, read/write or read-only) or a CD image (2048-byte blocks,
always read-only). scsi.py reads an image as plain blocks (block n at
n * block_size), reloads scsi.conf on SIGHUP and checks the images every two
seconds, so a unit given another image is a disk change on the Amiga.

The images live in ~/a314hdfs of the Pi user, as a314's README says. Uploads
use the same streaming, atomic write as the Files tab (files.save). A unit set
to a file outside that folder by hand is shown and kept as it is.

- Hard disk images: .hdf, .img (raw) and .vhd. A fixed VHD is raw data plus a
  512-byte footer, so it is used as it is; a dynamic or differencing VHD isn't
  plain blocks and is refused.
- CD images: .iso, and .bin with or without a .cue. A data track of 2048-byte
  sectors at the start of its .bin is used as it is. A raw track (2352 or 2336
  bytes per sector) can't be: the page offers to convert it to an .iso (the
  2048 data bytes of each sector of the first data track; audio tracks are
  left out), keeping the .bin/.cue or not, as the user chooses.
"""

import json
import os
import pwd
import re
import secrets
import struct
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import files

SCSI_CONF = Path(os.environ.get("TFPI_SCSI_CONF", "/etc/opt/a314/scsi.conf"))
SCSI_PY = Path(os.environ.get("TFPI_SCSI_PY", "/opt/a314/scsi.py"))
PKILL = os.environ.get("TFPI_PKILL", "pkill")
UNITS = 4
HD_EXTS = (".hdf", ".img", ".vhd")
CD_EXTS = (".iso", ".bin", ".cue")
KEEP = "=keep"                  # form value: leave a hand-set unit as it is

# kind -> (label, rw, block_size, cdrom)
KINDS = {
    "hd": ("Hard disk", True, 512, False),
    "hdro": ("Hard disk, read-only", False, 512, False),
    "cd": ("CD-ROM", False, 2048, True),
}

# CD track modes: bytes per sector in the file, offset of the 2048 data bytes
MODES = {"MODE1/2048": (2048, 0), "MODE1/2352": (2352, 16), "MODE2/2352": (2352, 24),
         "MODE2/2336": (2336, 8)}
SYNC = b"\x00" + b"\xff" * 10 + b"\x00"
VHD_TYPES = {2: "fixed", 3: "dynamic", 4: "differencing"}


class DisksError(ValueError):
    pass


@dataclass
class Unit:
    unit: int
    filename: str = None        # absolute path, None = no image
    kind: str = "hd"
    name: str = None            # file name if it is in the images folder
    present: bool = False


@dataclass
class Track:
    file: str                   # the .bin (name as in the .cue, or the .bin itself)
    mode: str                   # a MODES key
    start: int                  # first sector in the file
    sectors: int                # sectors of the track


@dataclass
class Image:
    name: str
    size: int
    used_by: list = field(default_factory=list)   # unit numbers
    cd: bool = False
    note: str = ""              # shown next to the name
    usable: bool = True         # can go in a unit as it is
    convert: Track = None       # a raw CD track the page offers to convert
    part_of: str = None         # a .bin that a .cue in the folder describes


# --- where things are ------------------------------------------------------------

def images_dir():
    """~/a314hdfs of the Pi user (uid 1000, as install.sh uses for a314)."""
    override = os.environ.get("TFPI_A314HDFS")
    if override:
        return Path(override)
    try:
        home = pwd.getpwuid(1000).pw_dir
    except KeyError:
        home = os.path.expanduser("~")
    return Path(home) / "a314hdfs"


def store():
    """The images folder as a files.Share, created (owned by the Pi user) if missing."""
    root = images_dir()
    if not root.is_dir():
        root.mkdir(parents=True)
        if os.geteuid() == 0:
            info = os.stat(root.parent)
            os.chown(root, info.st_uid, info.st_gid)
    return files.Share(volume="a314hdfs", root=root)


def installed():
    return SCSI_PY.is_file()


def cd_supported():
    """CD units came with a314scsi's CD support (a314 tf4060, Nov 2026)."""
    try:
        return "cdrom" in SCSI_PY.read_text(errors="replace")
    except OSError:
        return False


def is_cd_name(name):
    return name.lower().endswith(CD_EXTS)


def check_image_name(name):
    files.check_name(name)
    if not name.lower().endswith(HD_EXTS + CD_EXTS):
        raise DisksError(f"{name}: hard disk images end in {', '.join(HD_EXTS)}, "
                         f"CD images in {', '.join(CD_EXTS)}")
    return name


# --- what an image is ------------------------------------------------------------

def vhd_type(path):
    """None for a file without a VHD footer, else 'fixed', 'dynamic', ..."""
    try:
        with open(path, "rb") as handle:
            handle.seek(-512, os.SEEK_END)
            footer = handle.read(512)
    except OSError:
        return None
    if footer[:8] != b"conectix":
        return None
    return VHD_TYPES.get(struct.unpack_from(">I", footer, 60)[0], "unknown")


def _msf(text):
    m, s, f = (int(x) for x in text.split(":"))
    return (m * 60 + s) * 75 + f


def parse_cue(path):
    """[(file, mode, index01 sector)] per track, in order."""
    tracks, current, mode = [], None, None
    for line in Path(path).read_text(errors="replace").splitlines():
        words = line.strip().split()
        if not words:
            continue
        key = words[0].upper()
        if key == "FILE":
            found = re.match(r'\s*FILE\s+"([^"]+)"', line, re.I) or re.match(r"\s*FILE\s+(\S+)", line, re.I)
            current = found.group(1) if found else None
        elif key == "TRACK" and len(words) >= 3:
            mode = words[2].upper()
        elif key == "INDEX" and len(words) >= 3 and words[1] == "01" and current and mode:
            tracks.append((current, mode, _msf(words[2])))
            mode = None
    return tracks


def cue_data_track(root, cue):
    """The first data track a .cue describes, as a Track (sizes from its .bin)."""
    entries = parse_cue(root / cue)
    for index, (name, mode, start) in enumerate(entries):
        if mode == "AUDIO":
            continue
        if mode not in MODES:
            raise DisksError(f"{cue}: track mode {mode} isn't supported")
        binary = files.find(root, os.path.basename(name.replace("\\", "/")))
        if binary is None:
            raise DisksError(f"{cue}: its data file {name} isn't in the folder")
        size, _offset = MODES[mode]
        end = binary.stat().st_size // size
        for other, _mode, other_start in entries[index + 1:]:
            if other == name:
                end = other_start
                break
        return Track(binary.name, mode, start, end - start)
    raise DisksError(f"{cue} has no data track")


def bin_track(path):
    """A .bin without a .cue: raw if its first sector starts with the CD sync bytes."""
    size = path.stat().st_size
    with open(path, "rb") as handle:
        head = handle.read(16)
    if head[:12] == SYNC and size % 2352 == 0:
        mode = "MODE2/2352" if head[15] == 2 else "MODE1/2352"
        return Track(path.name, mode, 0, size // 2352)
    return Track(path.name, "MODE1/2048", 0, size // 2048)


def cue_for(root, name, cues):
    for cue, track in cues.items():
        if track is not None and track.file.lower() == name.lower():
            return cue
    return None


def images(unit_list=None):
    unit_list = units() if unit_list is None else unit_list
    root = images_dir()
    if not root.is_dir():
        return []
    entries = [e for e in files.listing(root)
               if not e.is_dir and e.name.lower().endswith(HD_EXTS + CD_EXTS)]
    cues, cue_errors = {}, {}
    for e in entries:
        if e.name.lower().endswith(".cue"):
            try:
                cues[e.name] = cue_data_track(root, e.name)
            except (DisksError, OSError, ValueError) as error:
                cues[e.name], cue_errors[e.name] = None, str(error)
    found = []
    for e in entries:
        lower = e.name.lower()
        image = Image(e.name, e.size, [u.unit for u in unit_list if u.name == e.name], cd=is_cd_name(e.name))
        if lower.endswith(".vhd"):
            kind = vhd_type(root / e.name)
            if kind not in (None, "fixed"):
                image.usable, image.note = False, f"{kind} VHD: only fixed VHDs can be used"
            elif kind is None and (e.size == 0 or e.size % 512):
                image.usable, image.note = False, "not a whole number of 512-byte blocks"
        elif lower.endswith((".hdf", ".img")):
            if e.size == 0 or e.size % 512:
                image.usable, image.note = False, "not a whole number of 512-byte blocks"
        elif lower.endswith(".iso"):
            if e.size == 0 or e.size % 2048:
                image.usable, image.note = False, "not a whole number of 2048-byte blocks"
        elif lower.endswith(".cue"):
            track = cues[e.name]
            image.usable = False                 # the unit takes the .bin
            if track is None:
                image.note = cue_errors[e.name]
            elif track.mode == "MODE1/2048" and track.start == 0:
                image.note = f"data track in {track.file}: put {track.file} in a CD-ROM unit"
            else:
                image.note = f"{track.mode} data track in {track.file}"
                image.convert = track
        elif lower.endswith(".bin"):
            cue = cue_for(root, e.name, cues)
            try:
                track = cues[cue] if cue else bin_track(root / e.name)
            except OSError as error:
                image.usable, image.note = False, str(error)
                track = None
            image.part_of = cue
            if track is not None and not (track.mode == "MODE1/2048" and track.start == 0):
                image.usable = False
                image.note = f"raw CD image ({track.mode})" + (f", see {cue}" if cue else "")
                if not cue:
                    image.convert = track
            elif cue:
                image.note = f"described by {cue}"
        found.append(image)
    return found


# --- units (scsi.conf) -----------------------------------------------------------

def read_conf():
    try:
        conf = json.loads(SCSI_CONF.read_text())
    except FileNotFoundError:
        return {"units": []}
    except (OSError, ValueError) as error:
        raise DisksError(f"{SCSI_CONF} can't be read: {error}") from None
    if not isinstance(conf, dict) or not isinstance(conf.get("units", []), list):
        raise DisksError(f"{SCSI_CONF} is not a units list")
    conf.setdefault("units", [])
    return conf


def kind_of(entry):
    if entry.get("cdrom"):
        return "cd"
    return "hd" if entry.get("rw") else "hdro"


def units(conf=None):
    conf = read_conf() if conf is None else conf
    root = os.path.realpath(images_dir())
    by_unit = {e.get("unit"): e for e in conf["units"] if isinstance(e, dict)}
    result = []
    for number in range(UNITS):
        entry = by_unit.get(number)
        if not entry or not entry.get("filename"):
            result.append(Unit(number))
            continue
        filename = entry["filename"]
        real = os.path.realpath(filename)
        name = os.path.basename(real) if os.path.dirname(real) == root else None
        result.append(Unit(number, filename, kind_of(entry), name, os.path.isfile(filename)))
    return result


def attached(name, unit_list=None):
    unit_list = units() if unit_list is None else unit_list
    return [u.unit for u in unit_list if u.name is not None and u.name.lower() == name.lower()]


def _write_conf(conf):
    """Atomic like config.write_json, but readable by scsi.py (it runs as the Pi user)."""
    SCSI_CONF.parent.mkdir(parents=True, exist_ok=True)
    temporary = SCSI_CONF.with_name(f".{SCSI_CONF.name}.tfpi-{os.getpid()}")
    try:
        with open(temporary, "w") as handle:
            json.dump(conf, handle, indent=4)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, SCSI_CONF)
    finally:
        if temporary.exists():
            temporary.unlink()
    directory = os.open(SCSI_CONF.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def reload():
    """Tell a running scsi.py to read scsi.conf again. If it isn't running,
    a314d starts it when the Amiga opens a314scsi.device and it reads the file then."""
    try:
        subprocess.run([PKILL, "-HUP", "-f", str(SCSI_PY)], capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def save_units(choices):
    """choices: {unit: (image name or "" or KEEP, kind)}. Validates the whole
    set, writes scsi.conf and reloads scsi.py. Returns the new unit list."""
    conf = read_conf()
    current = {u.unit: u for u in units(conf)}
    old_entries = {e.get("unit"): e for e in conf["units"] if isinstance(e, dict)}
    root = store().root
    info = {i.name.lower(): i for i in images(list(current.values()))}
    entries, writers, seen = [], {}, {}
    for number in range(UNITS):
        name, kind = choices.get(number, ("", "hd"))
        if name == KEEP:
            if number in old_entries and current[number].filename:
                entries.append(old_entries[number])
            continue
        if not name:
            continue
        if kind not in KINDS:
            raise DisksError(f"unit {number}: unknown disk type {kind!r}")
        check_image_name(name)
        path = files.find(root, name)
        if path is None or not path.is_file():
            raise DisksError(f"unit {number}: {name} isn't in {root}")
        path = root / files._inside(root, path).name     # checked inside, written as root/name
        image = info.get(path.name.lower())
        _label, rw, block_size, cdrom = KINDS[kind]
        if cdrom:
            if not cd_supported():
                raise DisksError("this Pi's a314 has no CD support yet: update tfpi first")
            if not image.cd:
                raise DisksError(f"unit {number}: {path.name} is a hard disk image, not a CD image")
            if path.name.lower().endswith(".cue"):
                raise DisksError(f"unit {number}: put the .bin of {path.name} in the unit, not the .cue")
        elif image.cd:
            raise DisksError(f"unit {number}: {path.name} is a CD image: use the CD-ROM type")
        if not image.usable:
            hint = ": convert it to an .iso first (Images, below)" if (
                image.convert or (image.part_of and info[image.part_of.lower()].convert)) else ""
            raise DisksError(f"unit {number}: {path.name} can't be used ({image.note}){hint}")
        key = str(path)
        if key in seen and (rw or writers.get(key)):
            raise DisksError(f"{path.name} is in units {seen[key]} and {number}: an image used "
                             "read/write can be in one unit only")
        seen.setdefault(key, number)
        writers[key] = writers.get(key) or rw
        entry = {"unit": number, "filename": key, "rw": rw, "block_size": block_size}
        if cdrom:
            entry["cdrom"] = True
        entries.append(entry)
    # entries this page doesn't show (no unit number 0-3) are left in the file
    entries += [e for e in conf["units"] if not (isinstance(e, dict) and e.get("unit") in range(UNITS))]
    conf["units"] = entries
    _write_conf(conf)
    reload()
    return units(conf)


# --- image files -----------------------------------------------------------------

def save_image(name, stream, length, overwrite=False):
    check_image_name(name)
    sh = store()
    existing = files.find(sh.root, name)
    if existing is not None and overwrite and attached(existing.name):
        raise DisksError(f"{existing.name} is in unit {attached(existing.name)[0]}: "
                         "take it out of the unit before replacing it")
    return files.save(sh, "", name, stream, length, overwrite=overwrite)


def rename_image(name, new_name):
    check_image_name(name)
    check_image_name(new_name)
    if attached(name):
        raise DisksError(f"{name} is in unit {attached(name)[0]}: take it out of the unit first")
    info = {i.name.lower(): i for i in images()}
    if name.lower() in info and info[name.lower()].part_of:
        raise DisksError(f"{name} is named in {info[name.lower()].part_of}: renaming it would break that .cue")
    sh = store()
    if not files.resolve(sh, name, follow=False).is_file():
        raise DisksError(f"{name} is not an image file")
    files.rename(sh, name, new_name)


def delete_image(name):
    check_image_name(name)
    if attached(name):
        raise DisksError(f"{name} is in unit {attached(name)[0]}: take it out of the unit first")
    sh = store()
    target = files.resolve(sh, name, follow=False)
    if target.is_dir() and not target.is_symlink():
        raise DisksError(f"{name} is a folder")
    files.delete(sh, name)


def free_bytes():
    return files.free_bytes(store())


# --- raw CD image -> .iso --------------------------------------------------------

_convert_lock = threading.Lock()
CONVERSION = {}                 # the one running or last finished: name, target, done, total, error, finished
CONVERT_THREAD = None
BATCH = 256                     # sectors per read


def converting():
    return bool(CONVERSION) and not CONVERSION.get("finished")


def start_convert(name, keep):
    """Check, then convert in a thread (one at a time). Returns the .iso name."""
    global CONVERT_THREAD
    check_image_name(name)
    sh = store()
    info = {i.name.lower(): i for i in images()}
    image = info.get(name.lower())
    if image is None or image.convert is None:
        raise DisksError(f"{name} is not a raw CD image that needs converting")
    track = image.convert
    source = files.find(sh.root, track.file)
    target = Path(image.name).stem + ".iso"
    if files.find(sh.root, target) is not None:
        raise DisksError(f"{target} already exists: rename or delete it first")
    originals = [image.name] + ([track.file] if track.file.lower() != image.name.lower() else [])
    if not keep and any(attached(o) for o in originals):
        raise DisksError(f"{', '.join(originals)} is in a unit: take it out first, or keep the originals")
    if track.sectors * 2048 > files.free_bytes(sh) - files.FREE_MARGIN:
        raise DisksError(f"not enough space on the Pi for {target}")
    if not _convert_lock.acquire(blocking=False):
        raise DisksError("a conversion is already running")
    CONVERSION.clear()
    CONVERSION.update(name=image.name, target=target, done=0, total=track.sectors, error=None, finished=False)
    CONVERT_THREAD = threading.Thread(target=_convert, args=(sh, source, track, target,
                                                            None if keep else originals), daemon=True)
    CONVERT_THREAD.start()
    return target


def _convert(sh, source, track, target, delete):
    size, offset = MODES[track.mode]
    temporary = sh.root / f"{files.PART}{secrets.token_hex(6)}"
    try:
        with open(source, "rb") as src, open(temporary, "wb") as out:
            src.seek(track.start * size)
            done = 0
            while done < track.sectors:
                count = min(BATCH, track.sectors - done)
                block = src.read(count * size)
                if len(block) < count * size:
                    raise DisksError(f"{source.name} ends at sector {done + len(block) // size} "
                                     f"of {track.sectors}")
                out.write(b"".join(block[i:i + 2048] for i in range(offset, len(block), size)))
                done += count
                CONVERSION["done"] = done
            out.flush()
            os.fsync(out.fileno())
        files._own(sh, temporary)
        os.replace(temporary, sh.root / target)
        files._fsync_dir(sh.root)
        for name in delete or []:
            files.delete(sh, name)
    except Exception as error:      # reported on the page
        CONVERSION["error"] = str(error)
    finally:
        if temporary.exists():
            temporary.unlink()
        CONVERSION["finished"] = True
        _convert_lock.release()
