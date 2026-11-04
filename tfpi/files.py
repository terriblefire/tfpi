"""The a314fs share (PI0: on the Amiga) as seen from the web page.

The share directory comes from a314fs's own config. Names follow what the
Amiga side can use: a314fs matches names case-insensitively and sends them as
Latin-1, and keeps protection bits/comments in a "<name>:a314" file next to
each file, which moves and goes with it here.

Uploads land in a hidden temporary file and are renamed into place only once
they are complete and on the card, so the Amiga never sees half a file and a
power cut (the Pi loses power with the Amiga) can't leave a truncated one.
"""

import json
import os
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path

A314FS_CONF = Path(os.environ.get("TFPI_A314FS_CONF", "/etc/opt/a314/a314fs.conf"))
META = ":a314"                 # a314fs metadata suffix
PART = ".tfpi-upload-"         # temporary upload prefix
JUNK = (".DS_Store", "Thumbs.db", "desktop.ini")
AMIGA_NAME_MAX = 30            # FFS/Workbench limit; longer names may not show
FREE_MARGIN = 64 * 1024 * 1024
CHUNK = 1024 * 1024


class FilesError(ValueError):
    pass


@dataclass
class Share:
    volume: str
    root: Path


@dataclass
class Entry:
    name: str
    is_dir: bool
    size: int
    mtime: float

    @property
    def long_name(self):
        return len(self.name) > AMIGA_NAME_MAX


def share():
    """The PI0: share, or None if a314fs isn't set up."""
    try:
        devices = json.loads(A314FS_CONF.read_text())["devices"]
        device = devices.get("PI0") or next(iter(devices.values()))
        root = Path(device["path"])
    except (OSError, ValueError, KeyError, StopIteration, TypeError):
        return None
    if not root.is_dir():
        return None
    return Share(volume=device.get("volume", "PI0"), root=root)


def is_junk(name):
    return name in JUNK or name.startswith("._")


def check_name(name):
    if not name or name in (".", "..") or name != name.strip():
        raise FilesError(f"{name!r} is not a valid name")
    if any(c in name for c in "/\\:") or any(ord(c) < 32 for c in name):
        raise FilesError(f"{name!r}: names can't contain / \\ : or control characters")
    if name.startswith(PART) or name.endswith(META):
        raise FilesError(f"{name!r} is a reserved name")
    try:
        name.encode("latin-1")
    except UnicodeEncodeError:
        raise FilesError(f"{name!r} has characters the Amiga can't show (it uses Latin-1)") from None
    if len(name.encode()) > 255:
        raise FilesError(f"{name!r} is too long")
    return name


def split(path):
    """'a/b/c' -> ['a', 'b', 'c'], each part checked."""
    return [check_name(p) for p in (path or "").replace("\\", "/").split("/") if p]


def _inside(root, path):
    real_root = os.path.realpath(root)
    real = os.path.realpath(path)
    if real != real_root and not real.startswith(real_root + os.sep):
        raise FilesError("that path is outside the share")
    return Path(real)


def find(directory, name):
    """The existing entry in `directory` matching `name` ignoring case (as a314fs
    does), with its on-disk spelling; an exact match wins."""
    try:
        entries = os.listdir(directory)
    except (FileNotFoundError, NotADirectoryError):
        return None
    if name in entries:
        return directory / name
    lower = name.lower()
    for entry in entries:
        if entry.lower() == lower:
            return directory / entry
    return None


def resolve(sh, path, must_exist=True, follow=True):
    """Absolute path of share-relative `path` (case-insensitive like the Amiga).
    Folders on the way are resolved. With follow=False the last part is left
    as is, so deleting or renaming a symlink acts on the link itself."""
    current = _inside(sh.root, sh.root)
    parts = split(path)
    for index, part in enumerate(parts):
        found = find(current, part)
        if found is None:
            if must_exist:
                raise FilesError(f"{path} doesn't exist")
            found = current / part
        if index < len(parts) - 1:
            found = _inside(sh.root, found)
        current = found
    if follow:
        _inside(sh.root, current)
    return current


def relative(sh, path):
    rel = Path(path).relative_to(os.path.realpath(sh.root)).as_posix()
    return "" if rel == "." else rel


def listing(directory):
    entries = []
    for name in os.listdir(directory):
        if name.endswith(META) or name.startswith(PART):
            continue
        full = directory / name
        try:
            info = full.stat()
        except OSError:
            continue                      # dangling symlink
        is_dir = full.is_dir()
        entries.append(Entry(name, is_dir, 0 if is_dir else info.st_size, info.st_mtime))
    entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
    return entries


def free_bytes(sh):
    return shutil.disk_usage(sh.root).free


# --- changes -------------------------------------------------------------------

def _fsync_dir(directory):
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _own(sh, path):
    """Files belong to the share's owner (a314fs runs as that user), not root."""
    if os.geteuid() == 0:
        info = os.stat(sh.root)
        os.chown(path, info.st_uid, info.st_gid)


def make_dirs(sh, path):
    """Create (case-insensitively reuse) each folder in `path`; return the last."""
    current = _inside(sh.root, sh.root)
    for part in split(path):
        found = find(current, part)
        if found is None:
            found = current / part
            found.mkdir()
            _own(sh, found)
            _fsync_dir(current)
        elif not found.is_dir():
            raise FilesError(f"{part} is a file, not a folder")
        current = _inside(sh.root, found)
    return current


def save(sh, folder, relpath, stream, length, overwrite=False):
    """Write `length` bytes from `stream` to folder/relpath (relpath may hold
    sub-folders, for dropped folders). Returns the share-relative path."""
    parts = split(relpath)
    if not parts:
        raise FilesError("no file name")
    name = parts[-1]
    if is_junk(name):
        raise FilesError(f"{name} is a desktop metadata file, skipped")
    if length is None:
        raise FilesError("upload size unknown")
    if length > free_bytes(sh) - FREE_MARGIN:
        raise FilesError(f"not enough space on the Pi for {name}")
    directory = make_dirs(sh, "/".join(split(folder) + parts[:-1]))
    existing = find(directory, name)
    if existing is not None:
        if existing.is_dir():
            raise FilesError(f"{name} is a folder")
        if not overwrite:
            raise FileExistsError(name)

    temporary = directory / f"{PART}{secrets.token_hex(6)}"
    written = 0
    try:
        with open(temporary, "wb") as handle:
            while written < length:
                chunk = stream.read(min(CHUNK, length - written))
                if not chunk:
                    break
                handle.write(chunk)
                written += len(chunk)
            if written != length:
                raise FilesError(f"{name}: upload was cut short ({written} of {length} bytes)")
            handle.flush()
            os.fsync(handle.fileno())
        _own(sh, temporary)
        target = existing or directory / name
        os.replace(temporary, target)
        if existing is not None and existing.name != name:
            os.rename(target, directory / name)     # take the new spelling
            _move_meta(existing, directory / name)
        _fsync_dir(directory)
    finally:
        if temporary.exists():
            temporary.unlink()
    return relative(sh, directory / name)


def _meta(path):
    return path.with_name(path.name + META)


def _move_meta(old, new):
    if _meta(old).exists():
        os.replace(_meta(old), _meta(new))


def mkdir(sh, folder, name):
    check_name(name)
    directory = resolve(sh, folder)
    if find(directory, name) is not None:
        raise FilesError(f"{name} already exists")
    make_dirs(sh, "/".join(split(folder) + [name]))


def rename(sh, path, new_name):
    check_name(new_name)
    source = resolve(sh, path, follow=False)
    if source == _inside(sh.root, sh.root):
        raise FilesError("can't rename the share itself")
    other = find(source.parent, new_name)
    if other is not None and other != source:
        raise FilesError(f"{new_name} already exists")
    target = source.parent / new_name
    os.rename(source, target)
    _move_meta(source, target)
    _fsync_dir(source.parent)


def delete(sh, path):
    target = resolve(sh, path, follow=False)
    if target == _inside(sh.root, sh.root):
        raise FilesError("can't delete the share itself")
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    else:
        target.unlink()
    if _meta(target).exists():
        _meta(target).unlink()
    _fsync_dir(target.parent)
