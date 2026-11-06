"""The r5 board netlist (data/r5_netlist.json, made by scripts/gen_r5_netlist.py)."""

import json
import re
from functools import lru_cache
from pathlib import Path

DEFAULT = Path(__file__).resolve().parent / "data" / "r5_netlist.json"
# Parts that cannot drive a net. Not JP (a jumper can tie a net to a supply) and resistors
# only when they are pull-ups/downs (>= MIN_PULL_OHMS); see Netlist.passive().
PASSIVE = re.compile(r"^(R|RN|C|TP|L|FB)\d")
MIN_PULL_OHMS = 1000
SDRAM = {"IC2", "IC3", "IC7", "IC8"}
CPU = "IC1"


class Netlist:
    def __init__(self, data):
        self.d = data
        self.cpld_part = data["cpld"]["part"]
        self.fpga_part = data["fpga"]["part"]

    @property
    def nets(self):
        return self.d["nets"]

    @property
    def cpld_pins(self):
        return self.d["cpld"]["pins"]

    @property
    def fpga_pins(self):
        return self.d["fpga"]["pins"]

    def neighbours(self, part, pad):
        return self.d["neighbours"].get(part, {}).get(str(pad), [])

    def net_of(self, part, pad):
        return self.d["parts"].get(part, {}).get("pads", {}).get(str(pad))

    def cpld_name(self, pin):
        """UCF name of a CPLD pin, else the board net name."""
        e = self.cpld_pins.get(str(pin), {})
        return e.get("ucf") or e.get("net")

    def cpld_pin_by_name(self, name):
        for pin, e in self.cpld_pins.items():
            if e.get("ucf") == name or e.get("net") == name:
                return pin
        return None

    def ohms(self, part):
        """Resistance from a value such as '22R', '1K', '4K7' or a CAY16-472J4LF code; None if unknown."""
        v = (self.d["parts"].get(part, {}).get("value") or "").upper()
        m = re.match(r"^CAY\d+-(\d)(\d)(\d)", v)
        if m:
            return int(m.group(1) + m.group(2)) * 10 ** int(m.group(3))
        m = re.match(r"^(\d+)([RKM])(\d*)$", v)
        if m:
            mult = {"R": 1, "K": 1000, "M": 1_000_000}[m.group(2)]
            return float(m.group(1) + "." + (m.group(3) or "0")) * mult
        m = re.match(r"^(\d+(?:\.\d+)?)$", v)
        return float(m.group(1)) if m else None

    def passive(self, part):
        """True for parts that cannot drive a net: capacitors, test points, ferrites and
        resistors of at least MIN_PULL_OHMS (a lower value, e.g. a 22R series resistor,
        links the net to whatever is on its other side)."""
        if not PASSIVE.match(part):
            return False
        if part.startswith("R"):
            r = self.ohms(part)
            return r is not None and r >= MIN_PULL_OHMS
        return True

    def adjacent(self, net_a, net_b):
        """Pads of net_a and net_b that sit next to each other: [(part, pad_a, pad_b)]."""
        out = []
        mb = {}
        for part, pad in self.nets.get(net_b, {}).get("members", []):
            mb.setdefault(part, set()).add(pad)
        for part, pad in self.nets.get(net_a, {}).get("members", []):
            if part not in mb:
                continue
            for n in self.neighbours(part, pad):
                if n in mb[part]:
                    out.append((part, pad, n))
        return out


@lru_cache(maxsize=4)
def _load(path):
    return Netlist(json.loads(Path(path).read_text()))


def load(path=None):
    return _load(str(path or DEFAULT))
