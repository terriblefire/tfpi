"""Do the installed TF4060 firmware and this Pi's a314d work together?

Since erique/a314 72cac83, a314d takes the SPI bus it shares with the TF4060's
flash with a CE1 handshake. Only firmware whose FPGA arbitrates that bus
(TF4060 v0.8.2 and later, tf4060v2 #123) supports it. Mixed either way, the A314
link (PI0:, a314eth) breaks as soon as the Amiga reads TF4060:.

The Pi side comes from versions.json (the a314 commit the installer built). The
firmware side comes from what this tool flashed: the a314 commit named in the
release's README.md, else the release version of the published build it matches.
Firmware flashed some other way is unknown, and then nothing is shown.
"""

import re
import subprocess
from functools import lru_cache

from . import config, feed

A314_HANDSHAKE = "72cac8333bbf0a9121eb890cc89bb1f9070f02ab"
FIRMWARE_ARBITRATION = (0, 8, 2)
VERSION = re.compile(r"^v(\d+)\.(\d+)\.(\d+)")


def a314_src():
    """The installer's a314 checkout (install.sh: $PREFIX/src/a314)."""
    return config.SHARE / "src" / "a314"


@lru_cache(maxsize=64)
def _contains_handshake(src, commit):
    if A314_HANDSHAKE.startswith(commit):
        return True
    try:
        rc = subprocess.run(["git", "-C", src, "merge-base", "--is-ancestor", A314_HANDSHAKE, commit],
                            capture_output=True, timeout=10).returncode
    except (OSError, subprocess.TimeoutExpired):
        return None
    return {0: True, 1: False}.get(rc)       # anything else: a commit git doesn't have


def a314_has_handshake(commit):
    """True/False for an a314 commit, None when it can't be told."""
    if not commit or not re.fullmatch(r"[0-9a-f]{7,40}", commit):
        return None
    return _contains_handshake(str(a314_src()), commit)


def release_version(name):
    found = VERSION.match(name or "")
    return tuple(int(x) for x in found.groups()) if found else None


def firmware(state, builds):
    """(name, has arbitration: True/False/None) for the firmware this tool flashed."""
    fpga = state.get("fpga", {})
    if not fpga.get("sha"):
        return None, None
    build = feed.installed_build(builds, state)
    name = (build or {}).get("name") or state.get("release")
    arbitration = a314_has_handshake(fpga.get("a314"))
    if arbitration is None and build is not None:
        version = release_version(build.get("name"))
        if version is not None:
            arbitration = version >= FIRMWARE_ARBITRATION
    return name, arbitration


def banner(state=None, versions=None, builds=None):
    """The warning to show on every page, or None."""
    state = config.load_state() if state is None else state
    versions = config.read_json(config.VERSIONS_FILE, {}) if versions is None else versions
    if builds is None:
        builds = (feed.cached() or {}).get("builds") or []
    a314 = (versions.get("a314") or {}).get("ref")
    pi = a314_has_handshake(a314)
    name, fw = firmware(state, builds)
    if pi is None or fw is None or pi == fw:
        return None
    if pi:
        return (f"This Pi's a314d (a314 {a314[:7]}) needs TF4060 firmware v0.8.2 or later, but the "
                f"TF4060 runs {name or 'older firmware'}. Together they break the A314 link (PI0:, "
                "a314eth) as soon as the Amiga reads TF4060:. Flash v0.8.2 or later on the Firmware page.")
    return (f"The TF4060 runs {name or 'firmware'} (v0.8.2 or later), which needs a newer a314d than "
            f"this Pi has (a314 {a314[:7]}). Together they break the A314 link (PI0:, a314eth) as "
            "soon as the Amiga reads TF4060:. Update the Pi software on the Updates page.")
