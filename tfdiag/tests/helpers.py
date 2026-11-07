"""Shared fixtures: a temp dir with a fake board, fake tools, diag images and a config."""

import hashlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

PI = Path(__file__).resolve().parents[2]          # .../pi
if str(PI) not in sys.path:
    sys.path.insert(0, str(PI))

from tfdiag import config as C                     # noqa: E402
from tfdiag import events as E                     # noqa: E402
from tfdiag import session as S                    # noqa: E402
from tfdiag.tests import fakes                     # noqa: E402

PREAMBLE = bytes.fromhex("FF00FF007EAA997E")


def make_body(code=b"\x4e\x71" * 300, load=0x4000):
    import struct
    from tfdiag import protocol as P
    return struct.pack(">4sIII", b"TFDB", len(code), P.body_sum(code), load) + code


def make_images(d, body=None):
    d = Path(d)
    d.mkdir(parents=True, exist_ok=True)
    files = {"diag_timing.bin": PREAMBLE + b"\x11" * 200, "diag_pins.bin": PREAMBLE + b"\x22" * 200,
             "diagex_body.bin": body or make_body()}
    for k, v in files.items():
        (d / k).write_bytes(v)
    info = {"build": "abc1234", "spi_protocol": 1,
            "images": {k: hashlib.sha256(v).hexdigest() for k, v in files.items()},
            "cpld_usercodes": [f"0x{fakes.USERCODE:08x}", "0x12345678"],
            "presets": [0, 2, 5], "tests": [{"bit": 0, "name": "chip"}, {"bit": 4, "name": "mbram"}]}
    (d / "diag_manifest.json").write_text(json.dumps(info))
    return d


class Env:
    def __init__(self, **state):
        self.tmp = Path(tempfile.mkdtemp(prefix="tfdiag-test-"))
        self.state_path = self.tmp / "board.json"
        fakes.init_state(self.state_path, **state)
        self.tools = fakes.install_tools(self.tmp / "bin", self.state_path, str(PI))
        self.images = make_images(self.tmp / "images")
        self.statedir = self.tmp / "var"
        self.statedir.mkdir()
        self.cfg = C.Config(fomu_flash=self.tools["fomu-flash"], openocd=self.tools["openocd"],
                            pinctrl=self.tools["pinctrl"], systemctl=self.tools["systemctl"],
                            setgpio=self.tools["setgpio"], state_dir=str(self.statedir),
                            xc3sprog=self.tools["xc3sprog"], xc3sprog_cable="gpiod_creator",
                            tfpi_state=str(self.statedir / "state.json"),
                            firmware_dir=str(self.statedir / "firmware"),
                            iceunpack="/nonexistent/iceunpack",
                            known_firmware=str(self.statedir / "known_firmware.json"),
                            marker=str(self.statedir / "diag-in-progress"),
                            lock_file=str(self.statedir / "diag.lock"),
                            flash_lock=str(self.statedir / "flash.lock"),
                            flash_check_bytes=4096, cdone_timeout=0.5, board_wait=30,
                            images=str(self.images))
        self.clock = fakes.FakeClock()
        self.board = fakes.State(self.state_path)

    def hw(self, cfg=None, ev=None):
        return TestHw(cfg or self.cfg, ev or E.Quiet(), self)

    def hw_factory(self):
        return lambda cfg, ev: TestHw(cfg, ev, self)

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestHw(S.Hw):
    def __init__(self, cfg, ev, env):
        super().__init__(cfg, ev, link_factory=lambda: fakes.FakeLink(env.state_path),
                         spi_factory=lambda: fakes.FakeSpiDev(env.state_path, env.clock))
        self.env = env
        self.clock = env.clock


def T_INFO(env):
    from tfdiag import timing
    return timing.load_info(env.images)


def run_cli(env, argv):
    """Run tfdiag.cli.main in process; return (rc, [events])."""
    from tfdiag import cli
    buf = io.StringIO()
    rc = cli.main(argv, hw_factory=env.hw_factory(), stream=buf, cfg=env.cfg)
    evs = [json.loads(l) for l in buf.getvalue().splitlines() if l.strip()]
    return rc, evs


class Base(unittest.TestCase):
    state = {}

    def setUp(self):
        self.env = Env(**self.state)
        self.addCleanup(self.env.cleanup)

    def board(self):
        return self.env.board.load()
