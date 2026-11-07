"""Tool paths and settings. Defaults match tfpi/config.py; every value can be
overridden by an environment variable (TFDIAG_*, falling back to tfpi's TFPI_*)
or by a JSON file (TFDIAG_CONFIG, default /etc/tfpi/diag.json)."""

import json
import os
from dataclasses import dataclass, field, fields, replace
from pathlib import Path


def _env(name, default, tfpi=None):
    v = os.environ.get("TFDIAG_" + name)
    if v is None and tfpi:
        v = os.environ.get("TFPI_" + tfpi)
    return default if v is None else v


_SHARE = _env("SHARE", "/opt/tfpi", "SHARE")
_STATE = _env("STATE_DIR", "/var/lib/tfpi", "STATE_DIR")


@dataclass
class Config:
    fomu_flash: str = _env("FOMU_FLASH", "fomu-flash", "FOMU_FLASH")
    openocd: str = _env("OPENOCD", "openocd")
    # read only: cpldread.py runs nothing but xc3sprog's read action (FILE:r)
    xc3sprog: str = _env("XC3SPROG", "xc3sprog", "XC3SPROG")
    xc3sprog_cable: str = _env("XC3SPROG_CABLE", "gpiod_creator", "XC3SPROG_CABLE")
    cpld_readback: bool = _env("CPLD_READBACK", "1") not in ("0", "no", "false", "")
    cpld_read_timeout: float = float(_env("CPLD_READ_TIMEOUT", "300"))
    tfpi_state: str = _env("TFPI_STATE", str(Path(_STATE) / "state.json"))
    iceunpack: str = _env("ICEUNPACK", "iceunpack")
    # known releases (official builds' firmware ids), shipped with tfpi
    known_firmware: str = _env("KNOWN_FIRMWARE",
                               str(Path(__file__).resolve().parent.parent / "tfpi" / "known_firmware.json"))
    firmware_dir: str = _env("FIRMWARE_DIR", str(Path(_STATE) / "firmware"))
    pinctrl: str = _env("PINCTRL", "pinctrl", "PINCTRL")
    systemctl: str = _env("SYSTEMCTL", "systemctl", "SYSTEMCTL")
    setgpio: str = _env("SETGPIO", str(Path(_SHARE) / "setgpio.sh"), "SETGPIO")
    cdone_gpio: int = int(_env("CDONE_GPIO", "23", "CDONE_GPIO"))
    a314_service: str = _env("A314_SERVICE", "a314d")
    state_dir: str = _STATE
    marker: str = _env("MARKER", str(Path(_STATE) / "diag-in-progress"))
    lock_file: str = _env("LOCK", str(Path(_STATE) / "diag.lock"))
    # held too so a firmware flash can't start during a diagnostic
    flash_lock: str = _env("FLASH_LOCK", str(Path(_STATE) / "flash.lock"))
    images: str = _env("IMAGES", str(Path(_SHARE) / "diag"))
    netlist: str = _env("NETLIST", str(Path(__file__).resolve().parent / "data" / "r5_netlist.json"))
    openocd_tcl_port: int = int(_env("OPENOCD_PORT", "6666"))
    spi_hz: int = int(_env("SPI_HZ", "4000000"))
    # bytes of SPI flash read for the image checksum and firmware id: bitstream +
    # SSBL/ROMFS (0x21000 + ~0.9 MB, so more than 1 MB); clamped to the chip's size
    flash_check_bytes: int = int(_env("FLASH_CHECK_BYTES", str(0x200000)), 0)
    cdone_timeout: float = float(_env("CDONE_TIMEOUT", "5"))
    board_wait: float = float(_env("BOARD_WAIT", "600"))     # seconds to wait for a board that went away
    tool_timeout: float = float(_env("TOOL_TIMEOUT", "120"))
    fake_spi: str = _env("FAKE_SPI", "")                       # tests only: path to a fake board state
    extra: dict = field(default_factory=dict)


def load(path=None, **overrides):
    cfg = Config()
    path = path or os.environ.get("TFDIAG_CONFIG", "/etc/tfpi/diag.json")
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        data = {}
    names = {f.name for f in fields(Config)}
    known = {k: v for k, v in data.items() if k in names}
    cfg = replace(cfg, **known)
    cfg = replace(cfg, **{k: v for k, v in overrides.items() if v is not None})
    return cfg
