"""A fake TF4060 for the tests: one JSON state file shared by

  * fake tools (fomu-flash, pinctrl, systemctl, setgpio.sh, openocd): small
    scripts that run `python3 -m tfdiag.tests.fakes <tool> ...`;
  * FakeSpiDev (in process): the RAM FPGA over SPI (normal image: A314 SRAM only;
    diag_timing: SRAM + diag commands + tprobe + a simulated exerciser;
    diag_pins: pin readback);
  * FakeLink (in process) or the fake openocd Tcl server: the CPLD TAP.

State keys: powered, image (flash|diag_timing|diag_pins|none), gen (image loads),
amiga_reset, cpu_rsti, a314d, flash_tag, flash_writes, forbidden, jtag {ir, update,
extest}, shorts [[net, net]], stuck {net: level}, opens [fpga pin], log [..],
power_return_after (cdone polls until power comes back), exerciser options.
"""

import json
import os
import socket
import struct
import sys
import textwrap
import time
from pathlib import Path

from tfdiag import netlist as NL
from tfdiag import protocol as P

USERCODE = 0x54463630
FLASH_JEDEC = ("Manufacturer ID: Microchip (bf)\n!! JEDEC Manufacturer ID: bf\n"
               "Memory model: unknown (26)\nMemory size: unknown (41)\nDevice ID: ff\n")


def default_state(**kw):
    s = {"powered": True, "image": "flash", "gen": 0, "amiga_reset": False, "cpu_rsti": 1,
         "a314d": "active", "flash_tag": 1, "flash_writes": 0, "forbidden": [],
         "jtag": {"ir": "IDCODE", "update": "0", "extest": False},
         "shorts": [], "stuck": {}, "opens": [], "log": [], "power_return_after": 0,
         "ex": {"hang_preset": None, "errors": [], "phases_per_preset": 3, "no_body_once": False,
                "stale_dsack_preset": None}}
    s.update(kw)
    return s


class State:
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        return json.loads(self.path.read_text())

    def save(self, s):
        tmp = self.path.with_suffix(".tmp%d" % os.getpid())
        tmp.write_text(json.dumps(s))
        tmp.replace(self.path)

    def update(self, fn):
        s = self.load()
        r = fn(s)
        self.save(s)
        return r


def init_state(path, **kw):
    State(path).save(default_state(**kw))
    return path


# ---- Amiga exposure ----------------------------------------------------------------------
def exposed(s):
    """The Amiga is out of reset with a diag image loaded although the Pi did not release it
    on purpose (0xA0, for the exerciser): it would run Kickstart / AmigaOS between steps."""
    return (s.get("diag_started") and s["image"] == "diag_timing" and not s["amiga_reset"]
            and not s.get("pi_released"))


def note_exposure(st, what, s=None):
    s = s or st.load()
    if exposed(s):
        s.setdefault("exposures", []).append(what)
        st.save(s)
    return s


# ---- net model ------------------------------------------------------------------------
def net_levels(s, nl):
    """Level of every net named on a CPLD or FPGA pin."""
    upd = int(s["jtag"]["update"], 16)
    extest = s["jtag"]["extest"] and s["powered"]
    driven = {}
    if extest:
        for pin, e in nl.cpld_pins.items():
            if "ctl" in e and (upd >> e["ctl"]) & 1:
                v = (upd >> e["out"]) & 1
                driven[e["net"]] = min(v, driven.get(e["net"], 1))
    groups = {}
    for a, b in s["shorts"]:
        ga, gb = groups.get(a, {a}), groups.get(b, {b})
        g = ga | gb
        for n in g:
            groups[n] = g
    nets = {e["net"] for e in nl.cpld_pins.values()} | {e["net"] for e in nl.fpga_pins.values()}
    lv = {}
    for n in nets:
        g = groups.get(n, {n})
        dv = [driven[m] for m in g if m in driven]
        if dv:
            lv[n] = min(dv)
        elif n == "CPU_RSTI":
            lv[n] = s["cpu_rsti"]
        elif n in s.get("toggle", []):
            # a free-running clock: the sample instant lands anywhere in its period
            lv[n] = ((s.get("tick", 0) * 2654435761 + hash(n) % 97) >> 9) & 1
        elif n in s.get("drift", {}):
            # a floating line: one level, then the other after some scans
            lv[n] = 1 if s.get("tick", 0) < s["drift"][n] else 0
        else:
            lv[n] = 1
        if n in s["stuck"]:
            lv[n] = s["stuck"][n]
    return lv


# ---- CPLD TAP --------------------------------------------------------------------------
IR_NAMES = {v: k for k, v in P.IR.items()}


class Tap:
    def __init__(self, state_path, nl=None):
        self.st = State(state_path)
        self.nl = nl or NL.load()

    def command(self, text):
        note_exposure(self.st, "jtag " + text[:40])
        f = text.split()
        if not f:
            return ""
        if f[0] == "init":
            return ""
        if f[:2] == ["jtag", "arp_init"]:
            def tlr(s):
                s["jtag"]["ir"] = "IDCODE"
                s["jtag"]["extest"] = False
                s["log"].append("jtag:tlr")
            self.st.update(tlr)
            return ""
        if f[0] == "irscan":
            code = int(f[2], 0)
            name = IR_NAMES.get(code, f"0x{code:02x}")

            def ir(s):
                if name not in P.IR:
                    s["forbidden"].append(f"ir {name}")
                s["jtag"]["ir"] = name
                s["jtag"]["extest"] = name == "EXTEST"
                s["log"].append(f"jtag:ir {name}")
            self.st.update(ir)
            return ""
        if f[0] == "drscan":
            widths = [int(x) for x in f[2::2]]
            vals = [int(x, 0) for x in f[3::2]]
            nbits = sum(widths)
            v_in = 0
            sh = 0
            for w, v in zip(widths, vals):
                v_in |= (v & ((1 << w) - 1)) << sh
                sh += w
            out = self.dr(v_in, nbits)
            words, sh = [], 0
            for w in widths:
                words.append("%08x" % ((out >> sh) & ((1 << w) - 1)))
                sh += w
            return " ".join(words)
        raise ValueError(f"fake openocd: unknown command {f[0]}")

    def dr(self, v_in, n):
        s = self.st.load()
        s["tick"] = s.get("tick", 0) + 1
        self.st.save(s)
        if not s["powered"]:
            return (1 << n) - 1
        ir = s["jtag"]["ir"]
        if ir == "IDCODE":
            cap, L = 0x59616093, 32
        elif ir == "USERCODE":
            cap, L = USERCODE, 32
        elif ir == "BYPASS":
            cap, L = 0, 1
        elif ir in ("SAMPLE", "EXTEST"):
            lv = net_levels(s, self.nl)
            cap = 0
            for pin, e in self.nl.cpld_pins.items():
                if "in" in e and lv.get(e["net"], 1):
                    cap |= 1 << e["in"]
            L = P.BSR_LEN
        else:
            cap, L = 0, 1
        full = cap | (v_in << L)
        out = full & ((1 << n) - 1)
        reg = (full >> n) & ((1 << L) - 1)
        if ir in ("SAMPLE", "EXTEST"):
            s["jtag"]["update"] = "%x" % reg
            self.st.save(s)
        return out


class FakeLink:
    """In-process stand-in for jtag.OpenocdLink."""

    def __init__(self, state_path):
        self.tap = Tap(state_path)
        self.closed = False

    def cmd(self, text):
        from tfdiag.jtag import JtagError
        try:
            return self.tap.command(text)
        except ValueError as e:
            raise JtagError(str(e))

    def close(self):
        self.closed = True


# ---- RAM FPGA over SPI ---------------------------------------------------------------
class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += max(s, 0.001)


class FakeSpiDev:
    def __init__(self, state_path, clock=None):
        self.st = State(state_path)
        self.clock = clock
        self.gen = None
        self.sram = bytearray(P.SRAM_SIZE)
        self.ex = None
        self.tp = {"sel": 0, "region": 0}
        self.wave = {"status": 0}
        self.last_latch = None
        self._seen_switch_phase = False     # the Pi has read phase 0x0001 at least once

    def now(self):
        return self.clock.now() if self.clock else time.monotonic()

    def close(self):
        pass

    def _sync(self):
        s = self.st.load()
        if s["gen"] != self.gen:
            self.gen = s["gen"]
            self.ex = None
            self.wave = {"status": 0}
            if s["image"] != "flash":
                self.sram = bytearray(P.SRAM_SIZE)
        return s

    def xfer2(self, data, speed_hz=0, *rest):
        data = list(data)
        sw = (self.ex or {}).get("sw") if isinstance(self.ex, dict) else None
        if (sw and sw["state"] == "window" and self.now() < sw["t0"] + sw["len"] and data
                and (not speed_hz or speed_hz > P.SPI_SLOW_HZ)):
            # the FPGA runs on the 25 MHz PLL bypass clock: the command byte is garbled.
            # 0xA1 (hold reset) is what it turns into on the T: the Amiga stops.
            self.st.update(lambda st: st.setdefault("garbled", []).append(f"{data[0]:#04x}"))
            data = [P.CMD_RESET_ASSERT] + [0] * (len(data) - 1)
        if self.ex and not self.ex.get("stub") and self._u16(P.MB_PHASE) == 0x0001 and data \
                and (not speed_hz or speed_hz > P.SPI_SLOW_HZ) and self._seen_switch_phase:
            self.st.update(lambda st: st.setdefault("fast_during_switch", []).append(f"{data[0]:#04x}"))
        if data and data[0] != P.CMD_RESET_ASSERT:
            note_exposure(self.st, f"spi {data[0]:#04x}")
        s = self._sync()
        n = len(data)
        if not s["powered"] or s["image"] == "none":
            return [0xFF] * n
        if s["image"] == "diag_pins" and data and data[0] == P.CMD_DIAG_ID:
            return ([0xFF, 0xFF, P.MAGIC_DIAG_ID, 1, s.get("pins_features", 0x90)]
                    + list(bytes.fromhex("0abc1234")) + [0xFF] * n)[:n]
        if s["image"] == "diag_pins":
            lv = net_levels(s, NL.load())
            nl = NL.load()
            bits = 0
            for pin in P.PINREAD_ORDER:
                net = nl.fpga_pins.get(pin, {}).get("net")
                b = 1 if pin in s["opens"] else lv.get(net, 1)
                bits = (bits << 1) | b
            nb = 8 * (P.PINREAD_LEN - 1)
            bits <<= nb - len(P.PINREAD_ORDER)
            resp = [P.PINREAD_MAGIC] + list(bits.to_bytes(nb // 8, "big"))
            if not speed_hz or speed_hz > 2_000_000:
                return [0xFF] * n          # too fast for the 25 MHz sampler
            return (resp + [0x00] * n)[:n]
        c = data[0]
        if c >> 5 == 0:
            a = ((data[1] & 0x3F) << 8) | data[2]
            if a == P.MAILBOX and self.ex is not None:
                self._exerciser_tick(s)
                self._seen_switch_phase = self._u16(P.MB_PHASE) == 0x0001
            if P.HIST_LO <= a < P.HIST_HI + 0x100 and getattr(self, "hs", None):
                self._fill_hist(s)
            out = [0xFF] * P.SRAM_READ_DATA_AT
            out += [self.sram[(a + i) % P.SRAM_SIZE] for i in range(n - P.SRAM_READ_DATA_AT)]
            return out[:n]
        if c >> 5 == 1:
            a = ((data[1] & 0x3F) << 8) | data[2]
            for i, b in enumerate(data[3:]):
                self.sram[(a + i) % P.SRAM_SIZE] = b
            sw = (self.ex or {}).get("sw")
            if sw and sw["state"] == "announce" and self._u16(P.MB_SWITCH_ACK) == sw["value"]:
                sw.update(state="window", t0=self.now(), acked=True)   # it switches straight away
            return [0xFF] * n
        if c == P.PROTO_VER:
            return [0xFF, 0xFF, 0x02, 0x02][:n] + [0x02] * max(0, n - 4)
        if s["image"] != "diag_timing":
            return [0xFF] * n
        return self._diag(c, data, n, s)

    def _diag(self, c, data, n, s):
        pad = lambda r: (r + [0xFF] * n)[:n]
        if c == P.CMD_RESET_ASSERT:
            def f(st):
                st["amiga_reset"] = True
                st["cpu_rsti"] = 0
                st["pi_released"] = False
            self.st.update(f)
            self.ex = None
            return [0xFF] * n
        if c == P.CMD_RESET_RELEASE:
            def f(st):
                st["amiga_reset"] = False
                st["cpu_rsti"] = 1
                st["pi_released"] = True            # the exerciser is meant to run
            self.st.update(f)
            self._boot(s)
            return [0xFF] * n
        if c == P.CMD_DIAG_ID:
            return pad([0xFF, 0xFF, P.MAGIC_DIAG_ID, 1, s.get("features", 0x6E)]
                       + list(bytes.fromhex(s.get("commit", "0abc1234"))))
        if c == P.CMD_CLOCKS:
            free = int(self.now() * P.FABRIC_MHZ * 1e6 * s.get("fabric_scale", 1.0)) & 0xFFFFFFFF
            gate = 1 << 22
            aux = int(gate / (P.FABRIC_MHZ / 25.0) * s.get("aux_scale", 1.0))
            body = struct.pack(">III", aux, gate, free) + bytes([7])
            return pad([0xFF, 0xFF, P.MAGIC_CLOCKS] + list(body))
        if c in (P.CMD_WAVE_ARM, P.CMD_WAVE_LOG_ARM):
            log = c == P.CMD_WAVE_LOG_ARM
            if log:
                flags = struct.unpack_from(">I", self.sram, P.MAILBOX + P.MB_STATUS)[0]
                if not flags & P.STATUS_BODY_RUNNING:
                    self.st.update(lambda st: st["forbidden"].append("0xB4 before the body runs"))
            self.wave = {"status": P.WAVE_STATUS_ARMED, "kind": data[1], "region": data[2], "reads": 0,
                         "log": log}
            self.logst = {"status": P.LOG_STATUS_ARMED if log else 0, "count": 0, "first": 0,
                          "trigger": 0, "next": 0, "lost": 0}
            return [0xFF] * n
        if c == P.CMD_LOG_STATUS:
            ls = getattr(self, "logst", {"status": 0, "count": 0, "first": 0, "trigger": 0, "next": 0, "lost": 0})
            body = (bytes([ls["status"]]) + struct.pack(">HHHH", ls["count"], ls["first"], ls["trigger"], ls["next"])
                    + bytes([ls["lost"]]))
            return pad([0xFF, 0xFF, P.MAGIC_LOG] + list(body))
        if c == P.CMD_CYCLES_CLEAR:
            self.cc_thr = data[1] if n > 1 else getattr(self, "cc_thr", 2)
            return [0xFF] * n
        if c == P.CMD_CYCLES:
            body = (b"".join(v.to_bytes(3, "big") for v in (1000, 300, 700, 650))
                    + bytes([5, 7, 2, 4, getattr(self, "cc_thr", 2)]))
            return pad([0xFF, 0xFF, P.MAGIC_CYCLES] + list(body))
        if c == P.CMD_HIST_START:
            self.hs = {"shift": data[1] & 3 if n > 1 else 0, "region": data[2] & 7 if n > 2 else 0, "reads": 0,
                       "t0": self.now()}
            self.starts = getattr(self, "starts", 0) + 1
            return [0xFF] * n
        if c == P.CMD_HIST_STATUS:
            h = getattr(self, "hs", None) or {"shift": 0, "region": 0, "reads": 1, "t0": self.now()}
            self.hs = h
            h["reads"] += 1
            clearing = 1 if h["reads"] == 1 and s.get("hist_slow_clear") else 0
            body = (bytes([h["shift"], h["region"], clearing]) + (self._hist_count() & 0xFFFFFF).to_bytes(3, "big")
                    + (2).to_bytes(2, "big"))
            return pad([0xFF, 0xFF, P.MAGIC_HIST] + list(body))
        if c == P.CMD_WAVE_STOP:
            self.wave["status"] = P.WAVE_STATUS_FULL
            if getattr(self, "logst", None) and self.logst["status"] & P.LOG_STATUS_ARMED:
                self.logst["status"] = P.LOG_STATUS_FROZEN
            return [0xFF] * n
        if c == P.CMD_WAVE_READ:
            w = self.wave
            if w.get("status") == P.WAVE_STATUS_ARMED:
                w["reads"] += 1
                if w["reads"] >= 1:
                    ex = s["ex"]
                    cur = (self.ex["presets"][self.ex["pi"]] if self.ex and not self.ex.get("stub")
                           and self.ex["pi"] < len(self.ex["presets"]) else None)
                    self._fill_wave(w["kind"], w["region"],
                                    stale=ex.get("stale_dsack_preset") is not None
                                    and cur == ex["stale_dsack_preset"] and w["region"] == 2)
                    w["status"] = P.WAVE_STATUS_TRIGGERED | P.WAVE_STATUS_FULL
            st = w.get("status", 0)
            cnt = P.WAVE_ENTRIES if st & P.WAVE_STATUS_FULL else 0
            return pad([0xFF, 0xFF, P.MAGIC_WAVE, st] + list(struct.pack(">HHHH", cnt, 0, P.WAVE_PRETRIGGER, 0))
                       + [w.get("kind", 0), 0])
        if c >> 5 == 6:
            self.tp["sel"] = c & 0x1F
            return [0xFF] * n
        if c >> 3 == 0x1C:
            self.tp["region"] = c & 7
            return [0xFF] * n
        if c == P.TPROBE_CLEAR:
            return [0xFF] * n
        if c == P.TPROBE_READ:
            bins = [0] * 32
            m = self.tp["sel"]
            bins[5] = 100
            if m == 10:
                bins = [500] + [0] * 31
                ex = s["ex"]
                cur = (self.ex["presets"][self.ex["pi"]] if self.ex and not self.ex.get("stub")
                       and self.ex["pi"] < len(self.ex["presets"]) else None)
                if ex.get("stale_dsack_preset") is not None and cur == ex["stale_dsack_preset"]:
                    bins[3] = 7
            words = [200, sum(bins)] + bins
            body = b"".join(struct.pack(">H", min(w, 0xFFFF)) for w in words)
            return pad([0xFF, P.TPROBE_MAGIC, m, self.tp["region"]] + list(body))
        return [0xFF] * n

    # DSACK latency (fabric clks) per histogram/waveform region code; 6 (Ramsey) ends with STERM
    WAVE_LAT = {0: 20, 1: 22, 2: 140, 3: 24, 4: 15, 5: 30, 6: 6}

    # test-mask bit -> group (phase id), and the region whose cycles that group makes
    HOLD_PHASE = {0: 0x10, 1: 0x40, 2: 0x50, 3: 0x60, 4: 0x30, 10: 0x68}
    HOLD_REGION = {0: 1, 1: 3, 2: 2, 3: 4, 4: 6, 10: 5}
    # AS20 cycles per second in each region while its group runs
    REGION_RATE = {1: 1_000_000, 2: 20_000, 3: 50_000, 4: 200_000, 5: 30_000, 6: 300_000}

    def _announce(self, preset, opt):
        """Clock switch step 1: 0x8000 | count << 8 | preset at 0x088, wait for the Pi's ack.
        A version 1 body does not announce: it switches at once."""
        e = self.ex
        if opt.get("version", 2) < P.HOLD_MIN_VERSION:
            e["sw"] = {"value": 0, "state": "window", "t0": self.now(), "len": opt.get("switch_window_s", 0.02),
                       "acked": False}
            return
        e["sw_count"] = (e.get("sw_count", 0) + 1) & 0x7F
        v = 0x8000 | (e["sw_count"] << 8) | (preset & 0xFF)
        struct.pack_into(">H", self.sram, P.MAILBOX + P.MB_SWITCH, v)
        e["sw"] = {"value": v, "state": "announce", "waited": 0, "len": opt.get("switch_window_s", 0.02),
                   "acked": False}

    def _switch_done(self, opt):
        """Advance the announced switch; True once it is over (0x088 bit 15 cleared)."""
        e = self.ex
        sw = e.get("sw")
        if not sw:
            e["sw_acked"] = False
            return True
        if sw["state"] == "announce":
            sw["waited"] += 1
            if sw["waited"] >= opt.get("ack_timeout_ticks", 50):
                sw.update(state="window", t0=self.now())          # no ack: switches anyway
            return False
        if self.now() < sw["t0"] + sw["len"]:
            return False
        v = self._u16(P.MB_SWITCH)
        struct.pack_into(">H", self.sram, P.MAILBOX + P.MB_SWITCH, v & 0x7FFF)
        e["sw_acked"] = sw["acked"]
        e["sw"] = None
        return True

    def _cur_preset(self):
        return (self.ex["presets"][self.ex["pi"]] if self.ex and not self.ex.get("stub")
                and self.ex["pi"] < len(self.ex["presets"]) else None)

    def _hist_count(self):
        """AS20 cycles in the selected region since 0xA8: the region's rate while its group
        runs (held, or everything when nothing is held), a tenth of it otherwise."""
        h = self.hs
        dt = max(0.0, self.now() - h["t0"])
        held = (self.ex or {}).get("held", 0)
        busy = not held or self.HOLD_REGION.get(held - 1) == h["region"]
        return int(self.REGION_RATE.get(h["region"], 10_000) * dt * (1.0 if busy else 0.1))

    def _fill_hist(self, s):
        """Bins of the 8 measurements for the selected region, as the FPGA would leave them
        after the time since 0xA8 (16-bit counters, saturating)."""
        reg, sh = self.hs["region"], self.hs["shift"]
        n = self._hist_count()
        lat = self.WAVE_LAT.get(reg, 20)
        b = lambda d: min(31, min(255, d) >> sh)
        bins = [[0] * 32 for _ in range(8)]
        f = lambda x: int(n * x)
        if reg == 6:
            bins[2][b(lat)] = f(0.3)
        else:
            bins[0][b(lat - 2)] = f(0.5)
            bins[1][b(lat - 2)] = f(0.2)
            bins[3][b(lat + 3)] = f(0.7)
            bins[4][0], bins[4][b(lat)] = f(0.4), f(0.1)
            bins[5][0], bins[5][1] = f(0.05), f(0.65)
            bins[6][0], bins[6][b(2)] = f(0.6), f(0.1)
            bins[7][0] = f(0.7)
            ex = s["ex"]
            if reg == 2 and ex.get("stale_dsack_preset") is not None and self._cur_preset() == ex["stale_dsack_preset"]:
                bins[7][b(40)] += max(1, f(0.007))
        for m in range(8):
            for k in range(32):
                v = min(bins[m][k], 0xFFFF)
                self.sram[P.HIST_LO + 32 * m + k] = v & 0xFF
                self.sram[P.HIST_HI + 32 * m + k] = v >> 8

    @staticmethod
    def _cycle(lat, sterm=False, write=False, stale=False):
        idle = 0xFFF
        AS, DS, RW, DK, TA = 1, 2, 4, 8 | 16, 32
        rw = RW if write else 0
        base = idle & ~rw
        if stale:
            base &= ~DK
        out = [(base & ~AS, 2), (idle & ~rw & ~AS & ~DS, max(1, lat - 2))]
        if sterm:
            out += [(idle & ~rw & ~AS & ~DS & ~TA, 1)]
        else:
            out += [(idle & ~rw & ~AS & ~DS & ~DK, 3), (idle & ~rw & ~AS & ~DS & ~DK & ~TA, 1),
                    (idle & ~DK, 2)]
        out += [(idle, 10)]
        return out

    def _fill_wave(self, kind=0, region=0, stale=False):
        filler = self._cycle(20) + self._cycle(20, write=True)
        ents = []
        while len(ents) < P.WAVE_PRETRIGGER:
            ents += filler
        ents = ents[-P.WAVE_PRETRIGGER:]
        if ents[-1][0] != 0xFFF:
            ents[-1] = (0xFFF, 10)
        reg = region if kind == 3 else 6 if kind == 4 else 1
        trig_pos = len(ents)
        ents += self._cycle(self.WAVE_LAT.get(reg, 20), sterm=(reg == 6), stale=stale)
        while len(ents) < P.WAVE_ENTRIES:
            ents += filler
        ents = ents[:P.WAVE_ENTRIES]
        if self.wave.get("log"):
            self._fill_log(ents, trig_pos, reg)
        for e, (sig, dur) in enumerate(ents[:P.WAVE_ENTRIES]):
            v = (sig << 12) | dur
            for k in range(3):
                self.sram[P.wave_addr(e, k)] = (v >> (8 * (2 - k))) & 0xFF

    LOG_ADDR = {1: 0x00012340, 2: 0x00BFE001, 3: 0x00DFF09A, 4: 0x00F80010, 5: 0x00DE0000, 6: 0x07F00010}

    def _fill_log(self, ents, trig_pos, reg):
        """One 8-byte entry per AS20 fall into the body region (it overwrites the body)."""
        n, prev_as, trig_entry = 0, 1, 0
        for j, (sig, _) in enumerate(ents):
            a = sig & 1
            if prev_as == 1 and a == 0 and n < P.LOG_ENTRIES:
                r = reg if j == trig_pos else 1                 # filler cycles are chip RAM
                if j == trig_pos:
                    trig_entry = n
                rw = 0 if sig & 4 == 0 else 1                   # RW20 bit 2: 1 = read
                b4 = (0 << 6) | (rw << 5) | (r << 2)            # SIZ long
                ent = (self.LOG_ADDR[r] + 4 * n).to_bytes(4, "big") + bytes(
                    [b4, j & 0xFF, ((j >> 8) << 7) | (36 >> 8), 36 & 0xFF])
                self.sram[P.LOG_BASE + 8 * n:P.LOG_BASE + 8 * n + 8] = ent
                n += 1
            prev_as = a
        self.logst = {"status": P.LOG_STATUS_FROZEN, "count": n, "first": 0,
                      "trigger": 0x400 | trig_entry, "next": n % P.LOG_ENTRIES, "lost": 0}

    # ---- exerciser (stub + body, rom/diagex*.asm) ----------------------------------------
    def _u16(self, off):
        return struct.unpack_from(">H", self.sram, P.MAILBOX + off)[0]

    def _boot(self, s):
        """Reset released: the stub runs, checks the body and the plan."""
        mb = P.MAILBOX
        if bytes(self.sram[mb:mb + 4]) != P.MAGIC_RUNNING:
            struct.pack_into(">I", self.sram, mb + P.MB_BOOTS, 0)
            struct.pack_into(">I", self.sram, mb + P.MB_HEARTBEAT, 0)
        boots = struct.unpack_from(">I", self.sram, mb + P.MB_BOOTS)[0] + 1
        struct.pack_into(">I", self.sram, mb + P.MB_BOOTS, boots)
        self.sram[mb:mb + 4] = P.MAGIC_RUNNING
        struct.pack_into(">H", self.sram, mb + P.MB_VERSION, 1)
        hdr = bytes(self.sram[P.BODY_BASE:P.BODY_BASE + P.BODY_HEADER])
        ok = hdr[:4] == P.BODY_MAGIC
        if ok:
            length, csum, load = struct.unpack(">III", hdr[4:16])
            code = bytes(self.sram[P.BODY_BASE + 16:P.BODY_BASE + 16 + length])
            ok = P.body_sum(code) == csum
        if ok and s["ex"].get("no_body_once") and not s.get("_nb_done"):
            self.st.update(lambda st: st.__setitem__("_nb_done", True))
            ok = False
        go = bytes(self.sram[mb + P.MB_GO:mb + P.MB_GO + 4]) == P.MAGIC_GO
        hb = struct.unpack_from(">I", self.sram, mb + P.MB_HEARTBEAT)[0]
        if not ok:
            flags = struct.unpack_from(">I", self.sram, mb + P.MB_STATUS)[0] | P.STATUS_NOBODY
            struct.pack_into(">H", self.sram, mb + P.MB_PHASE,
                             P.PH_BADBODY if hdr[:4] == P.BODY_MAGIC else P.PH_WAIT)
            struct.pack_into(">I", self.sram, mb + P.MB_STATUS, flags)
            self.ex = {"stub": True, "hb": hb}
            return
        npre = self._u16(P.MB_NPRE) if go else 1
        presets = list(self.sram[mb + P.MB_PRE:mb + P.MB_PRE + npre]) if go else [0]
        start = self._u16(P.MB_START) if go else 0
        append = bool(self._u16(P.MB_FLAGS) & P.FLAG_APPEND) if go else False
        if not append:
            self.sram[mb + 0x0C:mb + 0x38] = bytes(0x38 - 0x0C)
            struct.pack_into(">H", self.sram, mb + P.MB_VERSION, 1)
            self.sram[mb + P.MB_RESULTS:mb + P.MB_RESULTS_END] = bytes(P.MB_RESULTS_END - P.MB_RESULTS)
        self.sram[mb + P.MB_MACHINE:mb + P.MB_MACHINE + 0x20] = bytes(
            [0x0F, 0, 0x0C, 0xD0, 0, 0, 0, 0, 0, 0xFC, 0, 0xFC, 0x23, 0, 0, 0,
             0, 0x20, 0, 0, 0x07, 0, 0, 0, 0x01, 0, 0, 0, 0, 40, 0, 63])
        # machine ID first, then each preset starts with its clock switch: neither can be held
        self.ex = {"presets": presets, "pi": start, "phase": 0x03, "hb": hb, "ticks": 0,
                   "busy": s["ex"].get("machine_id_ticks", 6), "after": "switch"}
        flags = struct.unpack_from(">I", self.sram, mb + P.MB_STATUS)[0]
        if not s["ex"].get("old_body"):
            flags |= P.STATUS_BODY_RUNNING                   # the body's first instruction ...
        struct.pack_into(">I", self.sram, mb + P.MB_STATUS, flags)
        if not s["ex"].get("old_body"):                     # ... then its version over the stub's 1
            struct.pack_into(">H", self.sram, mb + P.MB_VERSION, s["ex"].get("version", 2))
        self._write_status()

    def _write_status(self):
        e = self.ex
        mb = P.MAILBOX
        if e.get("stub"):
            struct.pack_into(">I", self.sram, mb + P.MB_HEARTBEAT, e["hb"])
            return
        done = e["pi"] >= len(e["presets"])
        preset = e["presets"][min(e["pi"], len(e["presets"]) - 1)]
        flags = struct.unpack_from(">I", self.sram, mb + P.MB_STATUS)[0] & ~P.STATUS_NOBODY
        if done:
            flags |= P.STATUS_DONE
        struct.pack_into(">IHHI", self.sram, mb + P.MB_HEARTBEAT, e["hb"], P.PHASE_DONE if done else e["phase"],
                         preset, flags)
        struct.pack_into(">H", self.sram, mb + P.MB_PIDX, min(e["pi"], len(e["presets"]) - 1))

    def _add_result(self, preset, group, flags=0, size=4, addr=0x07F00000, exp=0x55AA55AA, got=0x55AA5DAA):
        mb = P.MAILBOX
        n = self._u16(P.MB_NRES)
        struct.pack_into(">HBBIII", self.sram, mb + P.MB_RESULTS + 16 * n, (preset << 8) | group, size, flags,
                         addr, exp, got)
        struct.pack_into(">H", self.sram, mb + P.MB_NRES, n + 1)
        if not flags & P.RF_NOT_FAILURE:
            st = struct.unpack_from(">I", self.sram, mb + P.MB_STATUS)[0] | 2
            struct.pack_into(">I", self.sram, mb + P.MB_STATUS, st)

    def _zorro(self, opt):
        """Body 3: the expansion board walk after the machine ID (opt "zorro": list of
        [zorro3, manufacturer, product, er_type, er_flags, configured_base or None])."""
        boards = opt.get("zorro")
        if boards is None:
            return
        info = 1 << P.RF["info"]
        for n, (z3, man, prod, typ, flg, base) in enumerate(boards):
            self._add_result(0, 0x04, flags=info, size=n | (0x80 if z3 else 0), addr=0x1000 + n,
                             exp=typ << 24 | prod << 16 | flg << 8, got=man << 16)
            if base is not None:
                self._add_result(0, 0x05, flags=info, size=n, addr=base, exp=0x10000,
                                 got=0xFF000000 if z3 else 0x00E80000)
        self._add_result(0, 0x06, flags=info, size=len(boards), addr=opt.get("zorro_end", 0), exp=0, got=0)

    def _exerciser_tick(self, s):
        e = self.ex
        if e.get("stub") or e["pi"] >= len(e["presets"]):
            e["hb"] += 1                       # stub waiting, or done: idles with a heartbeat
            self._write_status()
            return
        preset = e["presets"][e["pi"]]
        opt = s["ex"]
        if opt.get("hang_preset") is not None and preset == opt["hang_preset"]:
            return                             # hung: heartbeat frozen
        e["hb"] += 1
        if e.get("busy"):
            # machine ID (0x03) or the clock switch (0x01): runs to the end, ignores hold
            e["busy"] -= 1
            if not e["busy"]:
                if e["after"] == "switch":
                    self._zorro(opt)
                    e["phase"], e["busy"], e["after"] = 0x01, opt.get("switch_ticks", 2), "groups"
                    self._announce(preset, opt)
                elif not self._switch_done(opt):
                    e["busy"] = 1                      # still switching: stay in phase 0x01
                else:
                    sw = opt.get("switch", {}).get(str(preset), [1500 + 10 * preset, preset])
                    if sw is None:
                        self._add_result(preset, 0x01, flags=1 << P.RF["timeout"], addr=0x00E90044,
                                         exp=preset, got=0)
                    else:
                        self._add_result(preset, 0x01, flags=1 << P.RF["info"],
                                         addr=preset | (0x100 if e["sw_acked"] else 0), exp=sw[0], got=sw[1])
                    e["phase"] = 0x10
            self._write_status()
            return
        g = self._u16(P.MB_HOLD)
        if g and opt.get("first_hold_delay") and not e.get("delayed"):
            # the first request lands in a long group: acted on only when that group ends
            e["delay_left"] = e.get("delay_left", opt["first_hold_delay"]) - 1
            if e["delay_left"] > 0:
                self._write_status()
                return
            e["delayed"] = True
        if g and not opt.get("no_hold") and opt.get("version", 2) >= P.HOLD_MIN_VERSION:
            # hold mode: loop the requested group and echo it
            e["phase"] = self.HOLD_PHASE.get(g - 1, e["phase"])
            struct.pack_into(">HH", self.sram, P.MAILBOX + P.MB_HOLD_ECHO, g, self._u16(P.MB_HOLD_SEQ))
            e["held"] = g
            self._write_status()
            return
        e["held"] = 0
        e["ticks"] += 1
        if e["ticks"] % opt.get("ticks_per_phase", 4) == 0:
            groups = [0x10, 0x30, 0x40][:max(1, opt.get("phases_per_preset", 3))]
            k = groups.index(e["phase"]) if e["phase"] in groups else 0
            for err in opt.get("errors", []):
                if err[0] == preset and err[1] == e["phase"]:
                    self._add_result(preset, e["phase"])
            if opt.get("info_result") and k == 0:
                self._add_result(preset, 0x02, flags=1 << P.RF["info"])
            if k + 1 < len(groups):
                e["phase"] = groups[k + 1]
            else:
                e["phase"] = 0x01
                e["pi"] += 1
                if e["pi"] < len(e["presets"]):
                    e["busy"], e["after"] = opt.get("switch_ticks", 2), "groups"
                    self._announce(e["presets"][e["pi"]], opt)
        self._write_status()


# ---- fake tool scripts --------------------------------------------------------------------
TOOLS = ("fomu-flash", "pinctrl", "systemctl", "openocd", "xc3sprog")


def install_tools(bindir, state_path, pythonpath):
    bindir = Path(bindir)
    bindir.mkdir(parents=True, exist_ok=True)
    for t in TOOLS:
        p = bindir / t
        p.write_text(textwrap.dedent(f"""\
            #!/bin/sh
            FAKE_BOARD_STATE='{state_path}' PYTHONPATH='{pythonpath}' exec '{sys.executable}' -m tfdiag.tests.fakes {t} "$@"
            """))
        p.chmod(0o755)
    sg = bindir / "setgpio.sh"
    sg.write_text(f"FAKE_BOARD_STATE='{state_path}' PYTHONPATH='{pythonpath}' exec '{sys.executable}' "
                  f"-m tfdiag.tests.fakes setgpio\n")
    return {t: str(bindir / t) for t in TOOLS} | {"setgpio": str(sg)}


def flash_bytes(tag, n):
    head = bytes.fromhex("FF00FF00") + bytes.fromhex("7EAA997E")
    return (head + bytes([tag]) * n)[:n]


def tool_fomu(st, args):
    s = note_exposure(st, "fomu-flash " + " ".join(args))
    s["log"].append("fomu-flash " + " ".join(args))
    if any(a in args for a in ("-w", "-u", "-e", "-v", "-k")) or not args:
        s["forbidden"].append("fomu-flash " + " ".join(args))
        s["flash_writes"] += 1
        s["flash_tag"] += 1
        st.save(s)
        return 0
    if not s["powered"]:
        st.save(s)
        print("no response from flash")
        return 1
    if "-f" in args:
        img = Path(args[args.index("-f") + 1]).name
        s["image"] = "diag_pins" if "pins" in img else "diag_timing" if "timing" in img else "other"
        s["gen"] += 1
        s["amiga_reset"] = s["image"] == "diag_pins"     # diag_pins holds RESET low itself
        s["pi_released"] = False
        s["diag_started"] = True
        print("loaded", img)
    elif "-r" in args:
        if s.get("diag_started"):
            s.setdefault("boots", []).append(len(s["log"]))   # AmigaOS boots from here
        s["image"] = "flash"
        s["gen"] += 1
        s["amiga_reset"] = False
        s["cpu_rsti"] = 1
        print("reset")
    elif "-i" in args:
        s["image"] = "none"
        s["gen"] += 1
        print(FLASH_JEDEC)
    elif "-s" in args:
        n = int(args[args.index("-b") + 1], 0)
        Path(args[args.index("-s") + 1]).write_bytes(flash_bytes(s["flash_tag"], n))
        s["image"] = "none"
        s["gen"] += 1
    st.save(s)
    return 0


def cpld_jed(tag, qf=64):
    """A small JED whose fuse map depends on tag (the 'design' programmed in the fake CPLD)."""
    bits = "".join("1" if (i * 7 + tag) % 5 == 0 else "0" for i in range(qf))
    return f"\x02QF{qf}*\nQV0*\nF0*\nX0*\nJ0 0*\nN DEVICE XC95288XL-TQ144*\nL0 {bits[:32]}*\nL32 {bits[32:]}*\n\x0300000\n"


def tool_xc3sprog(st, args):
    """Only the read form '-c CABLE -p 0 FILE:r' reads; anything else would program the CPLD."""
    s = note_exposure(st, "xc3sprog " + " ".join(args))
    s["log"].append("xc3sprog " + " ".join(args))
    spec = args[-1] if args else ""
    path, _, action = spec.rpartition(":")
    if action != "r" or "-e" in args or "-l" in args or len(args) != 5:
        s["forbidden"].append("xc3sprog " + " ".join(args))
        st.save(s)
        return 0
    s.setdefault("cpld_reads", []).append({"amiga_reset": s["amiga_reset"], "image": s["image"]})
    st.save(s)
    if not s["powered"] or s.get("cpld_read_fails"):
        print("No device found")
        return 1
    if Path(path).exists():
        return 1
    Path(path).write_text(cpld_jed(s.get("cpld_tag", 1)))
    print("Readback done")
    return 0


def tool_pinctrl(st, args):
    s = st.load()
    if not s["powered"] and s.get("power_return_after"):
        s["power_return_after"] -= 1
        if s["power_return_after"] <= 0:
            s["powered"] = True
            s["image"] = "flash"
            s["gen"] += 1
            s["jtag"] = {"ir": "IDCODE", "update": "0", "extest": False}
            s["cpu_rsti"] = 1
        st.save(s)
    hi = s["powered"] and s["image"] not in ("none",)
    print(f"{args[-1]}: ip    pu | {'hi' if hi else 'lo'} // GPIO{args[-1]} = input")
    return 0


def tool_systemctl(st, args):
    s = st.load()
    verb = args[0]
    if verb == "is-active":
        print(s["a314d"])
        return 0 if s["a314d"] == "active" else 3
    if verb in ("stop", "start"):
        s["a314d"] = "inactive" if verb == "stop" else "active"
        s["log"].append("systemctl " + " ".join(args))
        st.save(s)
        return 0
    return 1


def tool_setgpio(st, args):
    st.update(lambda s: s["log"].append("setgpio"))
    return 0


def tool_openocd(st, args):
    cfgfile = args[args.index("-f") + 1]
    port = 6666
    for line in Path(cfgfile).read_text().splitlines():
        if line.startswith("tcl_port"):
            port = int(line.split()[1])
    tap = Tap(st.path)
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(1)
    conn, _ = srv.accept()
    buf = b""
    import re
    while True:
        chunk = conn.recv(65536)
        if not chunk:
            break
        buf += chunk
        while b"\x1a" in buf:
            msg, buf = buf.split(b"\x1a", 1)
            text = msg.decode()
            if text.strip() == "shutdown":
                conn.sendall(b"shutdown command invoked\x1a")
                conn.close()
                return 0
            m = re.search(r"catch \{(.*)\} _r", text)
            inner = m.group(1) if m else text
            try:
                r = "0 " + tap.command(inner)
            except Exception as e:
                r = "1 " + str(e)
            conn.sendall(r.encode() + b"\x1a")
    return 0


def main(argv):
    st = State(os.environ["FAKE_BOARD_STATE"])
    tool, args = argv[0], argv[1:]
    return {"fomu-flash": tool_fomu, "pinctrl": tool_pinctrl, "systemctl": tool_systemctl,
            "setgpio": tool_setgpio, "openocd": tool_openocd, "xc3sprog": tool_xc3sprog}[tool](st, args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
