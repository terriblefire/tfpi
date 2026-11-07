"""The diagnostic images' manifest (diag_manifest.json next to the images).

  {
    "images": {"diag_timing.bin": "<sha256>", "diag_pins.bin": "<sha256>",
               "diagex_body.bin": "<sha256>"},       # or name -> {"sha256": ...}
    "build": "abc1234",                              # commit the images were built from
    "spi_protocol": 1,                               # diag ID version byte (0xA2) of diag_timing.bin
    "cpld_usercodes": ["0x54463630", ...],           # CPLD designs the timing image works with
    "cpld_fuses": ["<sha256>", ...],                 # ... by fuse map (fwid fuse_sha256): the
                                                     # usercode is the same for every build
    "presets": [0, 2, 5],                            # default CLKSEL presets (optional)
    "tests": [{"bit": 0, "name": "chip"}, ...]       # test mask bits (optional)
  }
diag_info.json (older name) is read the same way. Unknown keys are kept.
"""

import json
from pathlib import Path

NAMES = ("diag_manifest.json", "diag_info.json")
SUPPORTED_SPI_PROTOCOL = {1}


def _int(v):
    if isinstance(v, int):
        return v
    try:
        return int(str(v), 0) if str(v).lower().startswith("0x") else int(str(v), 16)
    except ValueError:
        return None


def load(images):
    d = Path(images)
    raw, src = {}, None
    for n in NAMES:
        try:
            raw = json.loads((d / n).read_text())
            src = n
            break
        except (OSError, ValueError):
            continue
    m = dict(raw)
    imgs = {}
    for k, v in (raw.get("images") or {}).items():
        imgs[k] = v.get("sha256") if isinstance(v, dict) else v
    m["images"] = imgs
    m["build"] = raw.get("build") or raw.get("commit")
    m["spi_protocol"] = raw.get("spi_protocol", raw.get("protocol"))
    codes = raw.get("cpld_usercodes", raw.get("compatible_cpld_usercodes"))
    m["cpld_usercodes"] = None if codes is None else [c for c in (_int(x) for x in codes) if c is not None]
    m["source"] = src
    return m


def cpld_compatible(manifest, usercode):
    """(ok, message) for the timing image against the installed CPLD design."""
    codes = manifest.get("cpld_usercodes")
    uc = _int(usercode) if usercode is not None else None
    if uc is None:
        return False, "the CPLD usercode could not be read: the timing test needs a known CPLD design"
    if not codes:
        return False, (f"the image manifest lists no compatible CPLD designs: the timing test is not run "
                       f"(CPLD usercode 0x{uc:08x})")
    if uc in codes:
        return True, f"CPLD usercode 0x{uc:08x} is supported by the timing image"
    return False, (f"CPLD usercode 0x{uc:08x} is not one the timing image was built for "
                   f"({', '.join(f'0x{c:08x}' for c in codes)}): update the firmware or tfpi; "
                   "the board test still runs")


def cpld_design_compatible(manifest, fuse_sha256, usercode, any_cpld=False):
    """(ok, message): the timing image against the CPLD design read back from the board
    (fuse map checksum). The usercode check is only the fallback when the readback failed."""
    fuses = manifest.get("cpld_fuses")
    if any_cpld:
        return True, "CPLD design check overridden (TFDIAG_ANY_CPLD): the timing image runs on any CPLD"
    if not fuses:
        return cpld_compatible(manifest, usercode)
    if not fuse_sha256:
        ok, msg = cpld_compatible(manifest, usercode)
        return ok, msg + " (the CPLD design could not be read back, so only its usercode was checked)"
    if fuse_sha256 in fuses:
        return True, f"the BUS CPLD design (fuses {fuse_sha256[:16]}) is one the timing image supports"
    names = manifest.get("cpld_fuses_note") or "the current release"
    return False, (f"the BUS CPLD design on this board (fuses {fuse_sha256[:16]}) is not one the timing image "
                   f"supports ({names}). The timing capture would give wrong results with it, so it is not "
                   "run: update the TF firmware (both chips) to the current release, then run again. "
                   "The board test still runs.")


def diag_id_matches(manifest, did):
    """[(status, message)] for the diag ID read from the loaded timing image."""
    out = []
    want = manifest.get("spi_protocol")
    if did["version"] not in SUPPORTED_SPI_PROTOCOL:
        out.append(("incomplete", f"diag image protocol version {did['version']} is not one this tfdiag decodes "
                            f"({sorted(SUPPORTED_SPI_PROTOCOL)})"))
    elif want is not None and int(want) != did["version"]:
        out.append(("incomplete", f"diag image protocol version {did['version']}, manifest says {want}"))
    b = manifest.get("build")
    if b:
        from .timing import build_matches
        if not build_matches(did["commit"], str(b)):
            out.append(("info", f"diag image build {did['commit']} differs from the manifest's {b}"))
    need = {"waveform", "clock_counters", "reset"}           # tprobe is optional
    missing = need - set(did["features"])
    if missing:
        out.append(("info", "diag image lacks " + ", ".join(sorted(missing))))
    return out
