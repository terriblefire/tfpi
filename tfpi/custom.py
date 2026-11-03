"""A customisation session: an SDK zip plus the changes made on the page.

Stored in STATE_DIR/custom/<token>/:
  base.zip     the SDK (or a release with a full flash image)
  edits.json   {"toc": [entry...], "add": [file...], "remove": [path...]}
  files/       uploaded modules and ROMFS files

toc entries: {"sdk": index} for a module already in the SSBL, or
{"file": name, "label": original file name} for an uploaded one.
"""

import hashlib
import io
import json
import re
import secrets
import shutil
import zipfile
from pathlib import Path, PurePosixPath

from . import config, sdk, spirom
from .fwimage import SPIROM_OFFSET, FirmwareError, load_release

SAFE_PART = re.compile(r"^[^/\\:\x00-\x1f]{1,30}$")   # AmigaDOS names: <= 30 chars, no '/' ':'


class CustomError(ValueError):
    pass


def flash_bytes():
    return int(config.read_conf().get("SPI_FLASH_BYTES", 2 * 1024 * 1024))


def root_dir():
    return config.STATE_DIR / "custom"


def create(zip_path, name):
    token = secrets.token_hex(16)
    folder = root_dir() / token
    (folder / "files").mkdir(parents=True)
    shutil.copyfile(zip_path, folder / "base.zip")
    session = Session(token)
    session.meta = {"name": name}
    base = session.base()                       # validates
    session.edits = {"toc": [{"sdk": m.index} for m in base.contents.modules], "add": [], "remove": []}
    session.save()
    return token


class Base:
    """What we start from: the SPI ROM contents and how to get a bitstream."""

    def __init__(self, path, name):
        self.name = name
        if sdk.is_sdk(path):
            self.sdk = sdk.load_sdk(path, name)
            self.kind = "SDK"
            self.board = self.sdk.board
            image = self.sdk.spirom
        else:
            try:
                release = load_release(path, name=name, board=config.board(), spirom=config.spirom())
            except FirmwareError as error:
                raise CustomError(str(error)) from error
            self.sdk = None
            self.kind = "release"
            self.board = release.board
            self.release_bitstream = release.fpga_image[:SPIROM_OFFSET]
            image = release.fpga_image[SPIROM_OFFSET:]
        try:
            self.contents = sdk.read_spirom(image)
        except (spirom.SpiromError, ValueError) as error:
            raise CustomError(f"can't read the SSBL/ROMFS: {error}") from error

    @property
    def can_relink(self):
        return self.sdk is not None and self.sdk.can_relink

    def bitstream(self):
        if self.sdk is None:
            return self.release_bitstream
        return sdk.build_bitstream(self.sdk, cache_dir=config.STATE_DIR / "bitcache")


class Session:
    def __init__(self, token):
        if len(token) != 32 or not set(token) <= set("0123456789abcdef"):
            raise CustomError("no such session")
        self.token = token
        self.folder = root_dir() / token
        if not (self.folder / "base.zip").exists():
            raise CustomError("no such session")
        data = config.read_json(self.folder / "edits.json", {})
        self.meta = data.get("meta", {})
        self.edits = data.get("edits", {"toc": [], "add": [], "remove": []})
        self._base = None

    def save(self):
        config.write_json(self.folder / "edits.json", {"meta": self.meta, "edits": self.edits})

    def base(self):
        if self._base is None:
            self._base = Base(self.folder / "base.zip", self.meta.get("name", "base.zip"))
        return self._base

    # --- the edited contents ---------------------------------------------------

    def modules(self):
        """[(module, entry)] in boot order."""
        base = self.base()
        out = []
        for entry in self.edits["toc"]:
            if "sdk" in entry:
                out.append((base.contents.modules[entry["sdk"]], entry))
            else:
                data = (self.folder / "files" / entry["file"]).read_bytes()
                module = spirom.Module(index=len(out), data=data)
                spirom.describe_module(module)
                out.append((module, entry))
        return out

    def toc_changed(self):
        return self.edits["toc"] != [{"sdk": m.index} for m in self.base().contents.modules]

    def romfs(self):
        """(volume, root) with the adds/removes applied (a fresh tree)."""
        volume, root = spirom.parse_romfs(spirom.build_romfs(self.base().contents.volume,
                                                             self.base().contents.romfs))
        for path in self.edits["remove"]:
            _remove(root, path)
        for item in self.edits["add"]:
            data = (self.folder / "files" / item["file"]).read_bytes()
            _add(root, item["path"], data, item.get("executable", False))
        return volume, root

    def ssbl(self):
        if not self.toc_changed():
            return self.base().contents.ssbl
        base = self.base()
        if not base.can_relink:
            raise CustomError("this SDK has no SSBL objects, so resident modules can't change")
        self.check_relink()
        return sdk.link_ssbl(base.sdk, [m.data for m, _ in self.modules()])

    def check_relink(self):
        marker = self.folder / "relink_ok"
        if marker.exists():
            return
        try:
            sdk.check_relink(self.base().sdk, self.base().contents)
        except sdk.SdkError as error:
            raise CustomError(str(error)) from error
        marker.write_text("ok\n")

    def estimate(self):
        """(estimated SPI ROM bytes, limit). Uploaded modules add their size
        plus the 28-byte TOC entry overhead; exact once built."""
        volume, root = self.romfs()
        romfs = spirom.build_romfs(volume, root)
        base = self.base().contents
        ssbl = len(base.ssbl)
        if self.toc_changed():
            ssbl += sum(m.size + 28 for m, _ in self.modules()) - sum(m.size + 28 for m in base.modules)
        sectors = lambda n: (n + 511) // 512 * 512
        return 512 + sectors(ssbl) + sectors(len(romfs)), sdk.spirom_limit(flash_bytes())

    # --- changes -------------------------------------------------------------------

    def _store(self, data):
        name = hashlib.sha256(data).hexdigest()[:24] + ".bin"
        (self.folder / "files" / name).write_bytes(data)
        return name

    def _fits(self):
        size, limit = self.estimate()
        if size > limit:
            raise CustomError(f"that would make the SPI ROM {size} bytes; the limit is {limit}")

    def _changing(self, change):
        before = json.loads(json.dumps(self.edits))
        change()
        try:
            self._fits()
        except CustomError:
            self.edits = before
            raise
        self.save()

    def add_module(self, filename, data):
        if not self.base().can_relink:
            raise CustomError("this SDK has no SSBL objects (ssbl/*.o), so resident modules can't "
                              "be added; you can still add files to the ROMFS")
        try:
            spirom.check_hunk(data)
        except spirom.SpiromError as error:
            raise CustomError(f"{filename}: {error}") from error
        if not spirom.has_romtag(data):
            raise CustomError(f"{filename} has no ROMTag (Resident structure), so the SSBL can't "
                              "start it. Add it to the ROMFS instead (for example Devs/ or C/).")
        stored = self._store(data)
        self._changing(lambda: self.edits["toc"].append({"file": stored, "label": filename}))

    def remove_module(self, position):
        toc = self.edits["toc"]
        if not 0 <= position < len(toc):
            raise CustomError("no such module")
        if not self.base().can_relink:
            raise CustomError("this SDK has no SSBL objects, so resident modules can't change")
        self._changing(lambda: toc.pop(position))

    def move_module(self, position, delta):
        toc = self.edits["toc"]
        other = position + delta
        if not (0 <= position < len(toc) and 0 <= other < len(toc)):
            return
        if not self.base().can_relink:
            raise CustomError("this SDK has no SSBL objects, so resident modules can't change")
        toc[position], toc[other] = toc[other], toc[position]
        self.save()

    def add_file(self, folder, filename, data):
        parts = [p for p in folder.strip().strip("/").split("/") if p] + [filename]
        for part in parts:
            if not SAFE_PART.match(part) or part in (".", ".."):
                raise CustomError(f"{part!r} is not a valid AmigaDOS name (max 30 characters, no / or :)")
        path = "/".join(parts)
        executable = data[:4] == b"\x00\x00\x03\xf3"
        stored = self._store(data)

        def change():
            self.edits["add"] = [a for a in self.edits["add"] if a["path"].lower() != path.lower()]
            self.edits["add"].append({"path": path, "file": stored, "executable": executable})
        self._changing(change)

    def remove_path(self, path):
        def change():
            added = [a for a in self.edits["add"] if a["path"].lower() == path.lower()]
            if added:
                self.edits["add"] = [a for a in self.edits["add"] if a not in added]
            else:
                self.edits["remove"].append(path)
        self._changing(change)

    # --- build -------------------------------------------------------------------

    def build(self):
        """Write a firmware zip (flash image + README) and return its path."""
        base = self.base()
        ssbl = self.ssbl()
        volume, root = self.romfs()
        image = spirom.make_image(ssbl, spirom.build_romfs(volume, root))
        sdk.read_spirom(image)                         # parses back cleanly
        try:
            flash = sdk.flash_image(base.bitstream(), image, flash_bytes())
        except (sdk.SdkError, ValueError) as error:
            raise CustomError(str(error)) from error
        digest = hashlib.sha1(flash).hexdigest()[:8]
        stem = PurePosixPath(base.name).stem.replace("_SDK", "").replace(" ", "_")
        stem = re.sub(r"[^A-Za-z0-9_.-]", "", stem)[:60]
        name = f"{stem}_custom_{digest}.zip"
        readme = self.readme(image, flash, digest)
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(f"{base.board}_ram_flash.bin", flash)
            archive.writestr("README.md", readme)
        target = self.folder / name
        target.write_bytes(out.getvalue())
        return target

    def readme(self, image, flash, digest):
        base = self.base()
        lines = [f"# {base.board} custom firmware {digest}", "",
                 f"Built on the TF Pi from {base.name} ({base.kind}).", "",
                 f"SPI ROM: {len(image)} bytes of {sdk.spirom_limit(flash_bytes())}.",
                 f"Flash image SHA-1: {hashlib.sha1(flash).hexdigest()}", "",
                 "## Resident modules (boot order)", ""]
        for module, entry in self.modules():
            origin = "from the SDK" if "sdk" in entry else f"added ({entry['label']})"
            lines.append(f"- {module.name or '?'} {module.idstring} - {module.size} bytes, {origin}")
        if self.edits["add"] or self.edits["remove"]:
            lines += ["", "## ROMFS changes", ""]
            lines += [f"- removed {p}" for p in self.edits["remove"]]
            lines += [f"- added {a['path']}" for a in self.edits["add"]]
        lines.append("")
        return "\n".join(lines)


def _split(path):
    parts = [p for p in path.strip("/").split("/") if p]
    if not parts:
        raise CustomError("empty path")
    return parts[:-1], parts[-1]


def _remove(root, path):
    folders, name = _split(path)
    parent = spirom.find(root, "/".join(folders))
    if parent is None or not parent.is_dir():
        return
    parent.children = [c for c in parent.children if c.name.lower() != name.lower()]


def _add(root, path, data, executable):
    folders, name = _split(path)
    node = root
    for part in folders:
        match = [c for c in node.children if c.name.lower() == part.lower()]
        if match and not match[0].is_dir():
            raise CustomError(f"{part} is a file, not a folder")
        if not match:
            child = spirom.Node(part, spirom.DIRECTORY, True)
            node.children.append(child)
            match = [child]
        node = match[0]
    node.children = [c for c in node.children if c.name.lower() != name.lower()]
    node.children.append(spirom.Node(name, spirom.REGULAR, executable, data=data))
