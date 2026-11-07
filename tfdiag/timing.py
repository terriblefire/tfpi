"""Timing capture with the DIAG FPGA image and the exerciser.

Sequence: load diag_timing.bin, check the diag ID, measure the clocks, hold the
Amiga in reset (0xA1), upload the exerciser body + the TFGO plan into the A314
shared SRAM, release reset (0xA0), wait for the body to run (one retry). While the
exerciser runs, step the 0xA8 histogram block through the Amiga regions (the main
timing data), arm waveform captures round-robin, read the 0xA6 cycle counters and
measure the fabric clock at each preset; with a tprobe image, tprobe windows are
interleaved. Everything is tagged with the CLKSEL preset the exerciser reports. If
the heartbeat stops (hang): keep the results, reload the image and resume at the
next preset. At the end: results, machine ID, Amiga held in reset.
"""

import csv
import json
import struct
import time
from pathlib import Path

from . import amiga
from . import clocks
from . import protocol as P
from . import vcd
from .hooks import checkpoint

# measurements per region: everything for "all"; the bus-handshake ones per region too
REGION_MEAS = {
    "all": sorted(P.MEAS),
    "chip": [2, 3, 4, 5, 6, 10, 12, 13, 14, 15],
    "custom": [2, 3, 4, 5, 6, 10, 12, 13, 14, 15],
    "cia": [2, 3, 4, 5, 6, 10, 12, 13, 14, 15],
    "rom": [2, 3, 5, 6, 10, 13],
    "other": [2, 3, 4, 5, 6, 10, 12, 13, 14, 15],
}
# (trigger kind, region code), captured round-robin while a preset runs. Kind 3
# captures attribute their trigger cycle to the region, kind 4 to "ramsey"
# (STERM: Ramsey fast RAM).
_H = P.HIST_REGIONS
WAVE_PLAN = [(3, _H["chip"]), (3, _H["custom"]), (3, _H["cia"]), (3, _H["rom"]), (3, _H["ramsey"]),
             (3, _H["other"]), (4, 0), (0, 0), (2, 0)]
# 0xA8 windows, stepped round-robin: (region, bin shift). At 100 MHz, shift 0 covers
# 0-310 ns, 1 0-620 ns, 3 0-2.5 us (CIA cycles wait for the E clock).
HIST_PLAN = [("chip", 2), ("custom", 2), ("cia", 3), ("rom", 1), ("ramsey", 1), ("other", 1)]  # bin = 2^shift clks; ranges sized from CR/T captures (2026-11-06)
# hold mode: region -> exerciser test-mask bit whose group makes that region's cycles
HOLD_BIT = {"chip": 0, "custom": 1, "cia": 2, "rom": 3, "ramsey": 4, "other": 10}
# 0xA8 sub-windows: counters are 16-bit, so windows are short (sized from the fullest bin
# of the last one) and the bins are summed on the Pi
PRESET_GROUPS = set(P.GROUPS) - {0x01, 0x03, 0x04, 0x05, 0x06}    # test groups that run at a preset
SUB_WIN_START = 0.015
SUB_WIN_MIN = 0.002
SUB_WIN_MAX = 0.5
WAVE_UNIT_MHZ = 1000.0      # waveform cycles are kept in fabric clks (1 "ns" at 1000 MHz)


class Clock:
    def now(self):
        return time.monotonic()

    def sleep(self, s):
        time.sleep(s)


class TimingError(RuntimeError):
    pass


# ---- histograms ------------------------------------------------------------------

FAB_SAMPLE_S = 10.0   # sample the free-running fabric counter this often per preset
FAB_WRAP_S = 35.0     # 2^32 clks at <= ~120 MHz is > 35 s: pairs further apart may have wrapped
FAB_MIN_HZ, FAB_MAX_HZ = 20e6, 130e6   # a fabric clock outside this is a bad pair (presets run 50-110 MHz)

def bin_ranges(meas, fabric_mhz=None):
    """[(lo_ns, hi_ns or None)] for bins 1..31 (bin 0 is special) of a non-phase measurement."""
    half = 500.0 / (fabric_mhz or P.FABRIC_MHZ)
    shift = P.MEAS[meas][1]
    step = half * (1 << shift)
    return [((b - 1) * step, None if b == 31 else b * step) for b in range(1, 32)]


def hist_stats(meas, bins, fabric_mhz=None):
    """Summary of one histogram: count, special (bin 0), min/max/mean ns, or the phase fold."""
    name, shift, special = P.MEAS[meas]
    half = 500.0 / (fabric_mhz or P.FABRIC_MHZ)
    if shift is None:
        period = P.AMIGA_PERIOD_HALF
        fold = [0] * period
        for b in range(1, 32):
            fold[(b - 1) % period] += bins[b]
        tot = sum(fold)
        return {"count": tot, "phase_ns": [round(p * half, 2) for p in range(period)], "phase": fold}
    rng = bin_ranges(meas, fabric_mhz)
    counts = bins[1:32]
    tot = sum(counts)
    st = {"count": tot, "special": bins[0], "special_meaning": special}
    if tot:
        used = [i for i, c in enumerate(counts) if c]
        st["min_ns"] = round(rng[used[0]][0], 2)
        hi = rng[used[-1]][1]
        st["max_ns"] = round(hi, 2) if hi is not None else None
        st["max_open"] = hi is None
        st["max_lo_ns"] = round(rng[used[-1]][0], 2)
        mid = [(lo + (h if h is not None else lo + (rng[0][1] - rng[0][0]))) / 2 for lo, h in rng]
        st["mean_ns"] = round(sum(m * c for m, c in zip(mid, counts)) / tot, 2)
    return st


def frac_above(meas, bins, ns, fabric_mhz=None):
    """Fraction of the (non-special) samples in bins that start at or above ns."""
    rng = bin_ranges(meas, fabric_mhz)
    counts = bins[1:32]
    tot = sum(counts)
    if not tot:
        return 0.0
    return sum(c for (lo, _), c in zip(rng, counts) if lo >= ns - 1e-9) / tot


# ---- exerciser body + mailbox -------------------------------------------------------
def check_body(data):
    """Validate diagex_body.bin (rom/diagbody.py output); it is uploaded unchanged."""
    if len(data) < P.BODY_HEADER or data[:4] != P.BODY_MAGIC:
        raise TimingError("diagex_body.bin has no 'TFDB' header")
    if len(data) > P.BODY_MAX:
        raise TimingError(f"diagex_body.bin is {len(data)} bytes; the SRAM area holds {P.BODY_MAX}")
    length, csum, load = struct.unpack(">III", data[4:16])
    code = data[16:]
    if length != len(code) or length % 4:
        raise TimingError(f"diagex_body.bin: header length {length}, code {len(code)} bytes")
    if P.body_sum(code) != csum:
        raise TimingError("diagex_body.bin: checksum mismatch")
    return {"length": length, "checksum": f"0x{csum:08x}", "load": f"0x{load:08x}"}


def plan_bytes(presets, mask, start=0, iters=0, append=False):
    """The Pi's command block at MB_GO..MB_PLAN_END."""
    if not presets or len(presets) > P.MB_MAX_PRESETS:
        raise TimingError(f"1 to {P.MB_MAX_PRESETS} presets")
    blk = bytearray(P.MB_PLAN_END - P.MB_GO)
    blk[0:4] = P.MAGIC_GO
    struct.pack_into(">IH", blk, P.MB_MASK - P.MB_GO, mask & 0xFFFFFFFF, len(presets))
    blk[P.MB_PRE - P.MB_GO:P.MB_PRE - P.MB_GO + len(presets)] = bytes(p & 0xFF for p in presets)
    struct.pack_into(">HHH", blk, P.MB_START - P.MB_GO, start, iters, P.FLAG_APPEND if append else 0)
    return bytes(blk)


class Mailbox:
    def __init__(self, spi):
        self.spi = spi

    def read(self, off, n):
        return self.spi.sram_read(P.MAILBOX + off, n)

    def status(self):
        d = self.read(0, P.MB_HEADER_LEN)
        u16 = lambda o: struct.unpack(">H", d[o:o + 2])[0]
        u32 = lambda o: struct.unpack(">I", d[o:o + 4])[0]
        return {"magic": d[0:4], "heartbeat": u32(P.MB_HEARTBEAT), "phase": u16(P.MB_PHASE),
                "preset": u16(P.MB_PRESET), "flags": u32(P.MB_STATUS), "version": u16(P.MB_VERSION),
                "result_count": u16(P.MB_NRES), "failures": u32(P.MB_NFAIL), "pdone": u32(P.MB_PDONE),
                "pfail": u32(P.MB_PFAIL), "pidx": u16(P.MB_PIDX), "xpc": u32(P.MB_XPC),
                "xaddr": u32(P.MB_XADDR), "xvec": u16(P.MB_XVEC), "xcount": u16(P.MB_XCOUNT),
                "boots": u32(P.MB_BOOTS), "plansrc": u16(P.MB_PLANSRC)}

    def clear(self):
        """Zero the Amiga half (header, machine ID, results) for a fresh run."""
        self.spi.sram_write(P.MAILBOX, bytes(P.MB_GO))

    def machine(self):
        d = self.read(P.MB_MACHINE, P.MB_MACHINE_LEN)
        out = {"raw": d.hex()}
        for name, off, size in P.MACHINE_FIELDS:
            v = d[off:off + size]
            out[name] = v.hex() if size > 4 else int.from_bytes(v, "big")
        return out

    def results(self, count):
        n = min(count, (P.MB_RESULTS_END - P.MB_RESULTS) // P.MB_RESULT_LEN)
        d = self.read(P.MB_RESULTS, n * P.MB_RESULT_LEN)
        out = []
        for k in range(n):
            test, size, flags, addr, exp, got = struct.unpack(">HBBIII", d[16 * k:16 * k + 16])
            out.append({"preset": test >> 8, "test": test & 0xFF,
                        "group": P.GROUPS.get(test & 0xFF, f"test 0x{test & 0xFF:02x}"),
                        "size": size, "flags": [n for n, b in P.RF.items() if flags >> b & 1],
                        "failure": not (flags & P.RF_NOT_FAILURE),
                        "addr": f"0x{addr:08x}", "expected": f"0x{exp:08x}", "got": f"0x{got:08x}"})
        return out


def is_body_phase(phase):
    return phase == P.PHASE_DONE or phase == P.PH_BODY or (phase & 0xF000) != 0xF000


class TimingRun:
    def __init__(self, spi, fpga, events, out_dir, images, info=None, clock=None,
                 secs=0.5, start_timeout=15.0, hang_s=5.0, total_timeout=3600.0, poll=0.2, iters=0,
                 hold=True, region_secs=3.0, region_samples=200_000, hold_timeout=5.0, waves_per_region=3,
                 phase_wait=60.0, switch_handshake=True):
        self.spi = spi
        self.fpga = fpga
        self.ev = events
        self.out = Path(out_dir)
        self.images = Path(images)
        self.info = info or {}
        self.clock = clock or Clock()
        self.secs = secs
        self.start_timeout = start_timeout
        self.hang_s = hang_s
        self.total_timeout = total_timeout
        self.poll = poll
        self.iters = iters
        self.hold = hold                  # use the exerciser's hold mode when it answers
        self.hold_ok = None               # None untried, True works, False no echo (older body)
        self.hold_seq = 0
        self.held_presets = set()
        self.region_secs = region_secs
        self.region_samples = region_samples
        self.hold_timeout = hold_timeout
        self.phase_wait = phase_wait
        self.hold_log = []                # every hold request: region, group, seq, echo, wait
        self.switch_handshake = switch_handshake
        self.switch_acked = None          # last 0x088 value copied to 0x834
        self.switching = False            # 0x088 bit 15 set: a CLKSEL switch is pending
        self.switch_log = []              # per announced switch: value, preset, acked, times
        self.waves_per_region = waves_per_region
        self.sub_win = {}                 # region -> current 0xA8 sub-window (s)
        self.body_running = False         # mailbox status bit 7: the body region is free for the log
        self.log_stats = {"captures": 0, "entries": 0, "lost": 0, "aligned": 0}
        self.mb = Mailbox(spi)
        self.tp_hist = {}       # tprobe: preset -> region -> meas -> summed record
        self.hist = {}          # 0xA8: preset -> region -> {"shift", "bins": [8][32], "as", "lost", ...}
        self.hist_n = 0
        self.has_hist = True
        self.fab = {}           # preset -> fabric clock samples [(free, t)]
        self.waves = []         # capture records (one VCD per preset/kind/region)
        self.wave_n = {}        # preset -> captures armed
        self.cycles = {}        # preset -> region -> [cycle dicts] (amiga.cycles)
        self.results = []
        self.hangs = []
        self.start_captures = []
        self.machine = None
        self.exerciser = {}
        self.last_hb = None
        self.last_hb_t = None
        self.has_tprobe = True
        self.has_cycles = True
        self.cc = {}            # preset -> summed 0xA6 cycle counters
        self.cc_preset = None

    # ---- exerciser start ------------------------------------------------------------
    def start(self, presets, mask, start_index=0, append=False):
        """Hold reset, upload body + plan, release; True once the body runs."""
        body = (self.images / "diagex_body.bin").read_bytes()
        self.exerciser["body"] = check_body(body)
        for attempt in (1, 2):
            self.spi.reset_assert()
            self.clock.sleep(0.05)
            if not append:
                self.mb.clear()
            boots0 = self.mb.status()["boots"]
            self.spi.sram_write(P.BODY_BASE, body)
            self.spi.sram_write(P.MAILBOX + P.MB_GO, plan_bytes(presets, mask, start_index, self.iters, append))
            self.spi.sram_write(P.MAILBOX + P.MB_HOLD, bytes(4))            # no hold at the start
            if self.spi.sram_read(P.BODY_BASE, len(body)) != body:
                self.ev.warn("exerciser body read back differs from what was written (shared SRAM fault?)")
            cap = self.start_capture_arm()
            self.spi.reset_release()
            t0 = self.clock.now()
            stage = None
            while self.clock.now() - t0 < self.start_timeout:
                st = self.mb.status()
                if st["magic"] == P.MAGIC_RUNNING and st["boots"] != boots0:
                    stage = st["phase"]
                    if is_body_phase(st["phase"]):
                        self.ev.info(f"exerciser running (version {st['version']}, boot {st['boots']}), "
                                     f"presets {presets} from index {start_index}")
                        self.exerciser.update(version=st["version"], plansrc=st["plansrc"])
                        self.last_hb, self.last_hb_t = st["heartbeat"], self.clock.now()
                        self.start_capture_read(cap, attempt, "started")
                        checkpoint("timing.started")
                        return True
                    if st["phase"] in (P.PH_COPYBAD, P.PH_STUBEXC):
                        self.ev.result("exerciser_stub", "finding", "boot ROM stub halted: " +
                                       P.STUB_STAGES[st["phase"]] + " (chip RAM fault?)", step="timing")
                        self.start_capture_read(cap, attempt, "stub halted")
                        self.spi.reset_assert()
                        return False
                    if st["phase"] == P.PH_BADBODY or (st["flags"] & P.STATUS_NOBODY
                                                       and self.clock.now() - t0 > 2.0):
                        self.ev.warn("exerciser stub reports no valid body: uploading again")
                        self.start_capture_read(cap, attempt, "no valid body")
                        break
                self.clock.sleep(self.poll)
            else:
                what = (f"stub stuck at '{P.STUB_STAGES.get(stage, hex(stage))}'" if stage is not None
                        else "no 'TFDX' from the boot ROM stub")
                rec = self.start_capture_read(cap, attempt, "no answer" if stage is None else "stub stuck")
                self.ev.warn(f"{what} within {self.start_timeout:.0f} s (attempt {attempt})"
                             + (f": {rec['summary']}" if rec else ""))
        self.spi.reset_assert()
        return False

    # ---- what the 060 did after reset release (each start attempt) -------------------------
    def start_capture_arm(self):
        """Before releasing reset: clear the cycle counters and arm a waveform on the first
        TS fall (the 060's first bus cycle). No address log: before the body runs it would
        overwrite the body in the shared SRAM."""
        try:
            self.spi.cycle_clear(P.CYCLE_THRESHOLD)
            self.spi.wave_arm(2, 0, log=False)
            return {"t": self.clock.now()}
        except Exception as e:                  # an image without them
            self.ev.warn(f"start capture: {e}")
            return None

    def start_capture_read(self, cap, attempt, outcome):
        """Waveform from reset release (VCD waves/wave_start_a<n>.vcd) and the Amiga-side
        cycle counters, so a start that fails shows how far the 060 got."""
        if cap is None:
            return None
        rec = {"attempt": attempt, "outcome": outcome, "seconds": round(self.clock.now() - cap["t"], 2)}
        try:
            ws = self.spi.wave_status()
            if not ws["full"]:
                self.spi.wave_stop()
                ws = self.spi.wave_status()
            entries = self.spi.wave_entries(ws)
            trig = (ws["trigger"] - ws["first"]) % P.WAVE_ENTRIES if ws["triggered"] else None
            name = f"wave_start_a{attempt}.vcd"
            wdir = self.out / "waves"
            wdir.mkdir(parents=True, exist_ok=True)
            vcd.write(wdir / name, entries, trigger_index=trig, fabric_mhz=P.FABRIC_MHZ,
                      title=f"start attempt {attempt}: reset released, trigger first TS fall ({outcome})")
            sig = {n: any((e[0] >> i) & 1 == 0 for e in entries)          # bit i = WAVE_SIGNALS[i]
                   for i, n in enumerate(P.WAVE_SIGNALS)} if entries else {}
            rec.update(file=f"waves/{name}", entries=len(entries), ts_seen=bool(ws["triggered"]),
                       low_seen=sorted(n for n, v in sig.items() if v))
        except Exception as e:
            rec["wave_error"] = str(e)
        try:
            c = self.spi.cycle_counters()
            rec["cycles"] = {k: c.get(k) for k in ("as", "dsack", "sterm")}
        except Exception as e:
            rec["cycles_error"] = str(e)
        cy = rec.get("cycles") or {}
        if rec.get("ts_seen") is False:
            rec["summary"] = "the 060 made no bus cycle after reset release (no TS)"
        elif cy and not cy.get("as"):
            rec["summary"] = ("the 060 ran bus cycles after reset release but none reached the Amiga "
                              "(no AS20): it never fetched Kickstart")
        elif cy:
            rec["summary"] = (f"after reset release: {cy.get('as')} Amiga cycles ({cy.get('dsack')} DSACK, "
                              f"{cy.get('sterm')} STERM) in {rec['seconds']} s")
        else:
            rec["summary"] = "start capture read"
        self.start_captures.append(rec)
        return rec

    def switch_guard(self, st):
        """Clock switch handshake (0x088 / 0x834). Slow SPI during phase 0x0001 or while a
        switch is pending; ack a newly announced switch, then keep SPI quiet."""
        self.spi.slow = st.get("phase") == 0x0001 or self.switching
        if self.body_version(st) < P.HOLD_MIN_VERSION:
            self.switching = False           # an older body (or not running yet): slow SPI only
            return
        if not self.switch_handshake and st.get("phase") != 0x0001 and not self.switching:
            return
        v = struct.unpack(">H", self.mb.read(P.MB_SWITCH, 2))[0]
        pending = bool(v & P.SWITCH_PENDING)
        if pending and v != self.switch_acked:
            self.spi.slow = True
            rec = {"value": f"0x{v:04x}", "preset": v & 0xFF, "count": (v >> 8) & 0x7F,
                   "t": round(self.clock.now(), 3), "acked": False}
            if self.switch_handshake:
                self.spi.sram_write(P.MAILBOX + P.MB_SWITCH_ACK, struct.pack(">H", v))
                self.clock.sleep(P.SWITCH_QUIET_S)          # no SPI while the PLL switches
                rec["acked"] = True
            self.switch_acked = v
            self.switch_log.append(rec)
        if self.switching and not pending and self.switch_log:
            self.switch_log[-1]["done_after_s"] = round(self.clock.now() - self.switch_log[-1]["t"], 3)
        self.switching = pending
        self.spi.slow = st.get("phase") == 0x0001 or pending

    def wait_switch_done(self):
        """Poll slowly until the pending clock switch is over (alive() does the waiting)."""
        return self.alive() if self.switching else None

    def alive(self, switch_limit=5.0):
        """Read the mailbox; 'hung' once the heartbeat has not moved for hang_s.
        Handles the clock switch handshake on every poll: while a switch is pending it polls
        0x088 every SWITCH_POLL_S (slow SPI) until it is over, for up to switch_limit s (the
        heartbeat check below then catches a machine that never comes back)."""
        t0 = self.clock.now()
        while True:
            st = self.mb.status()
            self.switch_guard(st)
            if not self.switching or self.clock.now() - t0 >= switch_limit:
                break
            self.clock.sleep(P.SWITCH_POLL_S)
        now = self.clock.now()
        st["dead"] = st["magic"] != P.MAGIC_RUNNING
        if st["heartbeat"] != self.last_hb:
            self.last_hb, self.last_hb_t = st["heartbeat"], now
        st["hung"] = now - self.last_hb_t >= self.hang_s
        st["fatal"] = bool(st["flags"] & P.STATUS_FATAL)
        if st["flags"] & P.STATUS_BODY_RUNNING:
            self.body_running = True
        # The 32-bit free-running fabric counter wraps every ~43 s at 100 MHz: sample it
        # at least every FAB_SAMPLE_S while one preset runs, so fabric_mhz() can sum steps.
        cp = getattr(self, "cc_preset", None)
        if (cp is not None and st.get("preset") == cp and st.get("phase") != 0x0001
                and not st["dead"] and not st["hung"]):
            s = self.fab.get(cp) or []
            if not s or now - s[-1][1] >= FAB_SAMPLE_S:
                self.fabric_sample(cp)
        return st

    # ---- measurement schedule ---------------------------------------------------------
    def schedule(self):
        for reg, ms in REGION_MEAS.items():
            for m in ms:
                yield reg, m

    def add_hist(self, preset, reg, meas, h):
        rec = self.tp_hist.setdefault(preset, {}).setdefault(reg, {}).get(meas)
        if rec is None:
            rec = {"starts": 0, "samples": 0, "bins": [0] * P.TPROBE_BINS, "reads": 0, "saturated": False}
            self.tp_hist[preset][reg][meas] = rec
        rec["starts"] += h["starts"]
        rec["samples"] += h["samples"]
        rec["bins"] = [a + b for a, b in zip(rec["bins"], h["bins"])]
        rec["reads"] += 1
        if max(h["bins"] + [h["starts"], h["samples"]]) >= P.TPROBE_SATURATE:
            rec["saturated"] = True

    def wave_pending(self, preset):
        n = self.wave_n.get(preset, 0)
        self.wave_n[preset] = n + 1
        return WAVE_PLAN[n % len(WAVE_PLAN)]

    def arm(self, kind, code, preset):
        """Arm a waveform capture, with the address log once the body runs (0xB4)."""
        if self.switching:
            self.wait_switch_done()          # never arm while a clock switch is pending
        log = self.has_hist and self.body_running
        self.spi.wave_arm(kind, code, log=log)
        return {"kind": kind, "region": code, "preset": preset, "t": self.clock.now(), "log": log}

    @staticmethod
    def align_log(log, ws, n_entries):
        """{waveform position j: log entry} (j = index into the time-ordered entries)."""
        out = {}
        for e in log:
            j = (e["widx"] - ws["first"]) % P.WAVE_ENTRIES
            if j < n_entries:
                out[j] = e
        return out

    def add_logged_cycles(self, preset, entries, by_pos):
        """Every complete cycle whose AS20 fall has a log entry goes to its logged region."""
        tl = amiga.timeline(entries, WAVE_UNIT_MHZ)
        pos = {round(t, 3): j for j, (t, _) in enumerate(tl)}
        n = 0
        for c in amiga.cycles(entries, WAVE_UNIT_MHZ):
            e = by_pos.get(pos.get(round(c["t"], 3)))
            if e is None:
                continue
            region = amiga.REGION_BY_CODE.get(e["region"])
            if region is None:
                continue
            c.update(preset=preset, addr=f"0x{e['addr']:08x}", siz=P.SIZ_NAMES[e["siz"]], logged=True)
            self.cycles.setdefault(preset, {}).setdefault(region, []).append(c)
            n += 1
        return n

    def add_cycles(self, preset, kind, reg_code, entries, trig):
        """Attribute the trigger cycle of a region capture to its Amiga region."""
        region = (amiga.REGION_BY_CODE.get(reg_code) if kind == 3 else "ramsey" if kind == 4 else None)
        if region is None or trig is None:
            return
        t_trig = amiga.timeline(entries, WAVE_UNIT_MHZ)[trig][0]
        for c in amiga.cycles(entries, WAVE_UNIT_MHZ):     # values in fabric clks
            if c["t"] >= t_trig - 0.5:
                if kind == 4 and "as_to_sterm" not in c:
                    break
                c["preset"] = preset
                self.cycles.setdefault(preset, {}).setdefault(region, []).append(c)
                break

    def measure_one(self, reg, meas, preset):
        """One histogram window; returns the mailbox status at the end."""
        self.spi.tprobe_select(meas, P.REGIONS[reg])
        t0 = self.clock.now()
        st = None
        while True:
            self.clock.sleep(min(self.poll, max(0.0, self.secs - (self.clock.now() - t0))))
            st = self.alive()
            if self._stopped(st) or st["preset"] != preset or st["phase"] == P.PHASE_DONE:
                break
            if self.clock.now() - t0 >= self.secs:
                break
        if self._stopped(st) or st["preset"] != preset:
            return st                    # window not clean: discard
        h = self.spi.tprobe_read()
        if h["sel"] == meas:
            self.add_hist(preset, reg, meas, h)
        else:
            self.ev.warn(f"tprobe reports measurement {h['sel']}, wanted {meas}")
        return st

    def hist_window(self, reg, shift, preset, rec):
        """One short 0xA8 sub-window, its bins added to rec; the next sub-window is sized
        so the fullest bin stays below HIST_BIN_TARGET. Returns False if not usable."""
        win = self.sub_win.get(reg, SUB_WIN_START)
        self.spi.hist_start(shift, P.HIST_REGIONS[reg])
        t0 = self.clock.now()
        self.clock.sleep(win)
        hs = self.spi.hist_status()
        if hs["clearing"] or hs["region"] != P.HIST_REGIONS[reg] or hs["shift"] != shift:
            self.ev.warn(f"histogram status {hs} does not match the window ({reg}, shift {shift})")
            return False
        bins = self.spi.hist_bins()
        dt = self.clock.now() - t0
        peak = max(max(b) for b in bins)
        for m in range(P.HIST_MEASUREMENTS):
            rec["bins"][m] = [x + y for x, y in zip(rec["bins"][m], bins[m])]
        rec["as"] += hs["as"]
        rec["lost"] += hs["lost"]
        rec["windows"] += 1
        rec["seconds"] = round(rec["seconds"] + dt, 4)
        if peak >= P.HIST_SATURATE:
            rec["saturated_windows"] = rec.get("saturated_windows", 0) + 1
            rec["saturated"] = True
            win /= 4
        elif peak > P.HIST_BIN_TARGET:
            win /= 2
        elif peak and peak < P.HIST_BIN_TARGET / 4:
            win *= min(2.0, P.HIST_BIN_TARGET / 2 / peak)
        elif not peak:
            win *= 2
        self.sub_win[reg] = max(SUB_WIN_MIN, min(SUB_WIN_MAX, win))
        return True

    def hist_rec(self, preset, reg, shift):
        rec = self.hist.setdefault(preset, {}).get(reg)
        if rec is None:
            rec = {"shift": shift, "bins": [[0] * P.HIST_BINS for _ in range(P.HIST_MEASUREMENTS)],
                   "as": 0, "lost": 0, "windows": 0, "seconds": 0.0, "saturated": False,
                   "saturated_windows": 0, "held": False}
            self.hist[preset][reg] = rec
        return rec

    def measure_hist(self, preset, reg=None, budget=None, samples=None):
        """Short 0xA8 sub-windows for one region (the next in HIST_PLAN if reg is None)
        for `budget` seconds (default --secs) or until `samples` AS20 cycles; the mailbox
        is polled every `poll`. Data from a burst in which the preset changed or the
        exerciser stopped is dropped."""
        if reg is None:
            reg, shift = HIST_PLAN[self.hist_n % len(HIST_PLAN)]
            self.hist_n += 1
        else:
            shift = dict(HIST_PLAN)[reg]
        budget = self.secs if budget is None else budget
        tmp = {"shift": shift, "bins": [[0] * P.HIST_BINS for _ in range(P.HIST_MEASUREMENTS)],
               "as": 0, "lost": 0, "windows": 0, "seconds": 0.0, "saturated": False, "saturated_windows": 0}
        t0 = last_poll = self.clock.now()
        st = self.alive()
        while True:
            if self.switching or not self.hist_window(reg, shift, preset, tmp):
                break
            now = self.clock.now()
            if now - last_poll >= self.poll:
                last_poll = now
                st = self.alive()
                if self._stopped(st) or st["preset"] != preset:
                    return st                # burst not clean: dropped
            if now - t0 >= budget or (samples and tmp["as"] >= samples):
                break
        st = self.alive()
        if self._stopped(st) or st["preset"] != preset:
            return st
        rec = self.hist_rec(preset, reg, shift)
        for m in range(P.HIST_MEASUREMENTS):
            rec["bins"][m] = [x + y for x, y in zip(rec["bins"][m], tmp["bins"][m])]
        for k in ("as", "lost", "windows", "saturated_windows"):
            rec[k] += tmp[k]
        rec["seconds"] = round(rec["seconds"] + tmp["seconds"], 3)
        rec["saturated"] = rec["saturated_windows"] > 0
        return st

    # ---- hold mode ------------------------------------------------------------------
    def set_hold(self, bit):
        """Ask the exerciser to loop the group of test-mask bit `bit` (None = release)."""
        self.hold_seq = (self.hold_seq + 1) & 0xFFFF or 1
        # the sequence first, then the group (DIAGNOSTICS.md: the exerciser reads them after
        # every group, and a byte-wide port can show a half-written field)
        self.spi.sram_write(P.MAILBOX + P.MB_HOLD_SEQ, struct.pack(">H", self.hold_seq))
        self.spi.sram_write(P.MAILBOX + P.MB_HOLD, struct.pack(">H", 0 if bit is None else bit + 1))
        return self.hold_seq

    @staticmethod
    def body_version(st):
        """The body's version (0x010), valid only once status bit 7 (body running) is set:
        until then 0x010 holds the stub's 1. 0 = not known yet."""
        return st.get("version", 0) if st.get("flags", 0) & P.STATUS_BODY_RUNNING else 0

    def hold_supported(self, st):
        """From the body's version and the manifest, never from a timeout."""
        if self.info.get("exerciser_hold") is False:
            return False
        return self.body_version(st) >= P.HOLD_MIN_VERSION

    def read_echo(self):
        g, q, loops = struct.unpack(">HHI", self.mb.read(P.MB_HOLD_ECHO, 8))
        return {"group": g, "seq": q, "loops": loops}

    def wait_interruptible(self):
        """Wait while the exerciser is in a phase that ignores hold requests (machine ID, a
        clock switch, the stub). Returns the status, or None if it took too long."""
        t0 = self.clock.now()
        while True:
            st = self.alive()
            if self._stopped(st) or st["phase"] == P.PHASE_DONE:
                return st
            if st["phase"] not in P.NO_HOLD_PHASES and (st["phase"] & 0xF000) != 0xF000:
                return st
            if self.clock.now() - t0 >= self.phase_wait:
                return None
            self.clock.sleep(min(self.poll, 0.1))

    def wait_hold(self, group, seq):
        """Wait for the exerciser to echo (group, seq). Returns (echoed, status, echo)."""
        t0 = self.clock.now()
        echo = None
        while True:
            st = self.alive()
            if self._stopped(st) or st["phase"] == P.PHASE_DONE:
                return False, st, echo
            echo = self.read_echo()
            if (echo["group"], echo["seq"]) == (group, seq):
                return True, st, echo
            if self.clock.now() - t0 >= self.hold_timeout:
                return False, st, echo
            self.clock.sleep(min(self.poll, 0.05))

    def capture_held(self, preset):
        """Per region: hold the exerciser in that region's group, then histograms and
        waveform captures for that region only. Returns the last mailbox status."""
        st = self.alive()
        if self.hold_ok is None and not self.hold_supported(st):
            self.ev.info(f"exerciser version {st.get('version')} has no hold mode: capturing while it "
                         "runs its plan")
            self.hold_ok = False
            self.held_presets.add(preset)
            return st
        answered = 0
        for reg, shift in HIST_PLAN:
            bit = HOLD_BIT[reg]
            w = self.wait_interruptible()
            if w is None:
                self.ev.warn(f"preset {preset}: the exerciser stayed in phase {st['phase']:#06x} "
                             f"for {self.phase_wait:g} s: no hold for {reg}")
                continue
            st = w
            if self._stopped(st) or st["phase"] == P.PHASE_DONE or st["preset"] != preset:
                break
            t0 = self.clock.now()
            seq = self.set_hold(bit)
            ok, st, echo = self.wait_hold(bit + 1, seq)
            self.hold_log.append({"preset": preset, "region": reg, "group": bit + 1, "seq": seq,
                                  "echoed": ok, "waited_s": round(self.clock.now() - t0, 3),
                                  "phase": st["phase"], "echo": echo})
            if not ok:
                if self._stopped(st) or st["phase"] == P.PHASE_DONE:
                    break
                self.ev.warn(f"preset {preset}: no hold echo for the {reg} group within "
                             f"{self.hold_timeout:g} s (0x080-0x084: {echo}); this region is captured "
                             "without hold")
                continue
            answered += 1
            self.hold_ok = True
            if st["preset"] != preset:
                break
            self.ev.progress(30 + 60 * (st["pidx"] / max(1, self._npresets)),
                             f"preset {preset}: holding {P.GROUPS.get(st['phase'], hex(st['phase']))} ({reg})")
            if self.has_hist:
                st = self.measure_hist(preset, reg, budget=self.region_secs, samples=self.region_samples)
                if self._stopped(st) or st["preset"] != preset:
                    break
                if reg in self.hist.get(preset, {}):
                    self.hist[preset][reg]["held"] = True
            if self.has_tprobe:
                for m in REGION_MEAS.get(reg, []):
                    st = self.measure_one(reg, m, preset)
                    if self._stopped(st):
                        break
            for kind, code in self.region_waves(reg):
                for _ in range(self.waves_per_region):
                    st = self.capture_one(kind, code, preset)
                    if self._stopped(st) or st["preset"] != preset:
                        break
        self.set_hold(None)                  # let the exerciser finish the plan
        self.held_presets.add(preset)
        if not answered and self.hold_ok is None:
            # every region of a whole preset went unanswered: stop asking for this run
            self.ev.info("the exerciser answered no hold request during a whole preset: capturing "
                         "while it runs its plan from now on")
            self.hold_ok = False
        return st

    @staticmethod
    def region_waves(reg):
        code = P.HIST_REGIONS[reg]
        return [(3, code), (4, 0)] if reg == "ramsey" else [(3, code)]

    def capture_one(self, kind, code, preset):
        """Arm, wait for the trigger (up to 4 x --secs + 1 s), read; returns the mailbox status."""
        armed = self.arm(kind, code, preset)
        st = self.alive()
        while armed is not None:
            self.clock.sleep(min(self.poll, 0.05))
            st = self.alive()
            if self._stopped(st):
                self.spi.wave_stop()
                return st
            armed = self.check_wave(armed)
        return st

    def fabric_break(self):
        """The FPGA was reloaded: its free-running counter starts again from 0."""
        for samples in self.fab.values():
            if samples and samples[-1] is not None:
                samples.append(None)

    def fabric_sample(self, preset):
        """Free-running fabric counter vs Pi time while `preset` runs (0xA4)."""
        try:
            c = self.spi.clocks_latch()
        except Exception:
            return
        self.fab.setdefault(preset, []).append((c["free"], self.clock.now()))

    def fabric_mhz(self, preset):
        s = self.fab.get(preset) or []
        if len(s) < 2:
            return None
        # Sum the steps between consecutive samples (each < one counter wrap); a pair
        # further apart than FAB_WRAP_S may have wrapped more than once and is skipped.
        # None marks an FPGA reload (the counter restarts from 0): no step across it. A step
        # that implies a clock the PLL cannot make is a missed reload or a bad read: skipped.
        clks = secs = 0.0
        for a, b in zip(s, s[1:]):
            if a is None or b is None:
                continue
            (f0, t0), (f1, t1) = a, b
            dt = t1 - t0
            d = (f1 - f0) & 0xFFFFFFFF
            if 0 < dt < FAB_WRAP_S and FAB_MIN_HZ <= d / dt <= FAB_MAX_HZ:
                clks += d
                secs += dt
        if secs < 0.3:
            return None
        return round(clks / secs / 1e6, 3)

    def wait_window(self, preset):
        """No tprobe in this image: just watch the exerciser for one window."""
        t0 = self.clock.now()
        while True:
            self.clock.sleep(self.poll)
            st = self.alive()
            if (self._stopped(st) or st["preset"] != preset or st["phase"] == P.PHASE_DONE
                    or self.clock.now() - t0 >= self.secs):
                return st

    def cycles_roll(self, preset):
        """At a preset change (None = the end): the 0xA6 counters of the preset that
        ended are read and cleared, and the fabric clock is sampled at both ends."""
        if preset == self.cc_preset:
            return
        if self.cc_preset is not None:
            self.fabric_sample(self.cc_preset)
        if preset is not None:
            self.fabric_sample(preset)
        if not self.has_cycles:
            self.cc_preset = preset
            return
        try:
            if self.cc_preset is not None:
                c = self.spi.cycle_counters()
                acc = self.cc.setdefault(self.cc_preset, {"reads": 0})
                for k, v in c.items():
                    if k.endswith("_min"):
                        acc[k] = min(acc.get(k, 0xFF), v)
                    elif k.endswith("_max"):
                        acc[k] = max(acc.get(k, 0), v)
                    elif k == "threshold":
                        acc[k] = v
                    else:
                        acc[k] = acc.get(k, 0) + v
                acc["reads"] += 1
            self.spi.cycle_clear(P.CYCLE_THRESHOLD)
        except Exception as e:     # an image without the counters
            self.ev.warn(f"cycle counters (0xA6): {e}")
            self.has_cycles = False
        self.cc_preset = preset

    @staticmethod
    def _stopped(st):
        return st["hung"] or st["dead"] or (st["fatal"] and st["phase"] != P.PHASE_DONE)

    def check_wave(self, armed):
        if armed is None:
            return None
        if self.switching:
            return armed                     # read it once the clock switch is over
        ws = self.spi.wave_status()
        if not ws["full"]:
            if self.clock.now() - armed["t"] > 4 * self.secs + 2:
                self.spi.wave_stop()     # no trigger: keep what the ring holds
                ws = self.spi.wave_status()
                armed["timeout"] = True
            else:
                return armed
        entries = self.spi.wave_entries(ws)
        kind, reg, preset = armed["kind"], armed["region"], armed["preset"]
        name = f"wave_p{preset}_k{kind}_r{reg}.vcd"
        trig = (ws["trigger"] - ws["first"]) % P.WAVE_ENTRIES if (kind and ws["triggered"]) else None
        log, by_pos, ls = None, {}, None
        if armed.get("log"):
            try:
                ls = self.spi.log_status()
                if not ls["frozen"]:
                    self.spi.wave_stop()          # freezes the log with the waveform
                    ls = self.spi.log_status()
                log = self.spi.log_entries(ls)
                by_pos = self.align_log(log, ws, len(entries))
                self.log_stats["captures"] += 1
                self.log_stats["entries"] += len(log)
                self.log_stats["lost"] += ls["lost"]
                self.log_stats["aligned"] += len(by_pos)
            except Exception as e:                # an image without the log
                self.ev.warn(f"address log (0xB5): {e}")
                log = None
        if by_pos:
            self.add_logged_cycles(preset, entries, by_pos)
        else:
            self.add_cycles(preset, kind, reg, entries, trig)
        if any(w["preset"] == preset and w["kind"] == kind and w["region"] == reg for w in self.waves):
            return None              # one VCD per preset / trigger / region
        wdir = self.out / "waves"
        wdir.mkdir(parents=True, exist_ok=True)
        rname = amiga.REGION_BY_CODE.get(reg, "any") if kind == 3 else "any"
        events = sorted(by_pos.items())
        vcd.write(wdir / name, entries, trigger_index=trig, events=events,
                  title=f"preset {preset}, trigger {P.TRIGGERS.get(kind, kind)}, region {rname}")
        rec = {"preset": preset, "kind": kind, "trigger": P.TRIGGERS.get(kind, str(kind)),
               "region": reg, "region_name": rname, "entries": len(entries), "file": f"waves/{name}",
               "triggered": ws["triggered"], "timeout": bool(armed.get("timeout")),
               "trigger_index": trig, "status": ws, "data": entries, "events": events}
        if log is not None:
            lname = name[:-4] + ".log.json"
            (wdir / lname).write_text(json.dumps({
                "status": ls, "waveform": {k: ws[k] for k in ("first", "count", "trigger")},
                "entries": [{**e, "addr": f"0x{e['addr']:08x}", "siz": P.SIZ_NAMES[e["siz"]],
                             "rw": "read" if e["rw"] else "write",
                             "region": amiga.REGION_BY_CODE.get(e["region"], e["region"]),
                             "wave_pos": (e["widx"] - ws["first"]) % P.WAVE_ENTRIES} for e in log]}, indent=1))
            rec.update(log_file=f"waves/{lname}", log_entries=len(log), log_aligned=len(by_pos),
                       log_lost=ls["lost"])
        self.waves.append(rec)
        self.ev.info(f"waveform: preset {preset}, {P.TRIGGERS.get(kind, kind)} ({rname}): {len(entries)} entries"
                     + ("" if ws["triggered"] or not kind else " (no trigger)"))
        return None

    def run_plan(self, presets, mask):
        self._npresets = len(presets)
        t_start = self.clock.now()
        start_index, append, restarts = 0, False, 0
        while start_index < len(presets):
            if not self.start(presets, mask, start_index, append):
                seen = "; ".join(f"attempt {c['attempt']}: {c['summary']}" for c in self.start_captures
                                 if c.get("summary"))
                self.ev.result("exerciser_start", "incomplete",
                               "the exerciser did not start after two tries: the 060 may not reach the TF "
                               "boot ROM, or the shared SRAM is faulty"
                               + (f" ({seen}; waveforms waves/wave_start_a*.vcd)" if seen else ""), step="timing")
                return False
            sched = self.schedule()
            armed = None
            stop = None
            win = 0
            while True:
                if self.clock.now() - t_start > self.total_timeout:
                    self.ev.warn("timing run timed out")
                    stop = "timeout"
                    break
                st = self.alive()
                if st["phase"] == P.PHASE_DONE or st["flags"] & P.STATUS_DONE:
                    stop = "done"
                    break
                if self._stopped(st):
                    stop = "hang"
                    break
                preset = st["preset"]
                self.cycles_roll(preset)
                if (self.hold and self.hold_ok is not False and preset not in self.held_presets
                        and st["phase"] in PRESET_GROUPS and st["flags"] & P.STATUS_BODY_RUNNING):
                    # hold only once this preset's own groups run (the exerciser also honours a
                    # hold after the machine ID group and the clock switch)
                    if armed is not None:
                        self.spi.wave_stop()
                        armed = None
                    st = self.capture_held(preset)
                    continue
                if armed is None:
                    nxt = self.wave_pending(preset)
                    if nxt:
                        armed = self.arm(nxt[0], nxt[1], preset)
                win += 1
                if self.has_tprobe and (not self.has_hist or win % 2 == 0):
                    try:
                        reg, meas = next(sched)
                    except StopIteration:
                        sched = self.schedule()
                        reg, meas = next(sched)
                    st = self.measure_one(reg, meas, preset)
                elif self.has_hist:
                    st = self.measure_hist(preset)
                else:
                    st = self.wait_window(preset)
                if armed is not None and armed["preset"] != st["preset"]:
                    self.spi.wave_stop()
                    armed = None             # re-arm for the new preset
                armed = self.check_wave(armed)
                self.ev.progress(30 + 60 * (st["pidx"] / max(1, len(presets))),
                                 f"preset {st['preset']} ({st['pidx'] + 1}/{len(presets)}), "
                                 f"{P.GROUPS.get(st['phase'], hex(st['phase']))}")
            self.cycles_roll(None)
            st = self.mb.status()
            self.exerciser["status"] = {k: (v.decode(errors="replace") if isinstance(v, bytes) else v)
                                        for k, v in st.items()}
            if stop != "hang":
                break
            # the results so far are in SRAM: read them before the FPGA reload clears it
            n_saved = len(self.results)
            if st["result_count"]:
                self.results += self.mb.results(st["result_count"])
            if self.machine is None:
                try:
                    self.machine = self.mb.machine()
                except Exception:
                    pass
            idx = st["pidx"]
            hung_preset = presets[idx] if idx < len(presets) else st["preset"]
            why = ("took an exception outside any test" if st["flags"] & P.STATUS_FATAL
                   else "stopped (heartbeat frozen)")
            self.hangs.append({"preset": hung_preset, "index": idx, "phase": st["phase"],
                               "group": P.GROUPS.get(st["phase"], hex(st["phase"])),
                               "heartbeat": st["heartbeat"], "xpc": f"0x{st['xpc']:08x}",
                               "xvec": st["xvec"], "why": why})
            self.ev.result(f"hang_preset_{hung_preset}", "finding",
                           f"the Amiga {why} at preset {hung_preset} in "
                           f"{P.GROUPS.get(st['phase'], hex(st['phase']))}: reset, resuming at the next preset",
                           step="timing")
            # reload the diag image: back to preset 0 with a clean FPGA (CLKSEL survives an
            # Amiga reset), then a fresh run from the next preset
            self.spi.reset_assert()
            try:
                self.fabric_break()
                self.fpga.load_image(self.images / "diag_timing.bin",
                                     (self.info.get("images") or {}).get("diag_timing.bin"),
                                     hold=self.spi.reset_assert)
                self.spi.reset_assert()
                append = False
            except Exception as e:
                self.ev.warn(f"diag image reload failed ({e}): resuming after an Amiga reset only")
                append = True
                self.results = self.results[:n_saved]     # still in SRAM (append): read at the end
            self.cc_preset = None
            start_index = idx + 1
            restarts += 1
            if restarts > len(presets):
                break
        st = self.mb.status()
        if st["result_count"]:
            self.results += self.mb.results(st["result_count"])
        try:
            m = self.mb.machine()
            if self.machine is None or any(v for k, v in m.items() if k != "raw"):
                self.machine = m
        except Exception as e:   # noqa: keep the run's data
            self.ev.warn(f"machine ID: {e}")
        self.spi.reset_assert()
        return True

    # ---- whole run --------------------------------------------------------------------
    def run(self, presets, mask, expect_build=None):
        self.out.mkdir(parents=True, exist_ok=True)
        res = {"presets": presets, "test_mask": f"0x{mask:08x}"}
        sha = (self.info.get("images") or {}).get("diag_timing.bin")
        self.ev.progress(5, "loading diag_timing.bin into FPGA SRAM")
        self.fpga.load_image(self.images / "diag_timing.bin", sha, hold=self.spi.reset_assert)
        checkpoint("timing.image")
        did = self.spi.diag_id()
        res["diag_id"] = did
        if did is None:
            self.ev.result("diag_id", "incomplete", "the diag image does not answer 0xA2: it did not "
                           "configure, or the Pi SPI link to the FPGA is broken", step="timing")
            return self.finish(res, ok=False)
        bmsg = f"diag image v{did['version']}, build {did['commit']}, features {', '.join(did['features'])}"
        if expect_build and not build_matches(did["commit"], expect_build):
            self.ev.result("diag_id", "incomplete", bmsg + f": expected build {expect_build}", step="timing")
            return self.finish(res, ok=False)
        from . import manifest
        checks = manifest.diag_id_matches(self.info, did)
        for status, msg in checks:
            self.ev.result("diag_id_check", status, msg, step="timing")
        if any(s == "incomplete" for s, _ in checks):
            return self.finish(res, ok=False)
        self.ev.result("diag_id", "info", bmsg, step="timing")
        self.has_tprobe = "tprobe" in did["features"]
        self.has_hist = "histograms" in did["features"]
        self.has_cycles = "cycle_counters" in did["features"]
        self.ev.info("diag image: " + ", ".join(n for n, on in (("histograms", self.has_hist),
                     ("cycle counters", self.has_cycles), ("tprobe", self.has_tprobe)) if on))
        self.ev.progress(10, "clock counters")
        try:
            ck = clocks.measure(self.spi, sleep=self.clock.sleep, now=self.clock.now)
            res["clocks"] = ck
            for c in ck["clocks"]:
                self.ev.result(f"clock_{c['name']}", c["status"], c["msg"], step="timing")
        except Exception as e:
            self.ev.result("clocks", "incomplete", f"clock counters: {e}", step="timing")
        self.ev.progress(20, "starting the exerciser")
        self.run_plan(presets, mask)
        return self.finish(res, ok=True)

    def finish(self, res, ok):
        res["machine"] = self.machine
        res["exerciser"] = self.exerciser
        res["results"] = self.results
        res["hangs"] = self.hangs
        res["start_captures"] = self.start_captures
        res["histograms"] = {str(p): regs for p, regs in self.hist.items()}
        res["histogram_measurements"] = [n for n, _ in P.HIST_MEAS]
        res["tprobe_histograms"] = {str(p): {r: {str(m): v for m, v in ms.items()} for r, ms in regs.items()}
                                    for p, regs in self.tp_hist.items()}
        res["fabric_mhz"] = {str(p): self.fabric_mhz(p) for p in self.fab}
        res["cycles_unit"] = "fabric clks"
        for w in self.waves:                 # VCD times at the preset's measured fabric clock
            mhz = self.fabric_mhz(w["preset"])
            if mhz and w.get("data"):
                vcd.write(self.out / w["file"], w["data"], trigger_index=w.get("trigger_index"),
                          fabric_mhz=mhz, events=w.get("events"),
                          title=f"preset {w['preset']}, trigger {w['trigger']}, region {w['region_name']}")
                w["fabric_mhz"] = mhz
        res["waves"] = [{k: v for k, v in w.items() if k not in ("data", "events")} for w in self.waves]
        res["address_log"] = dict(self.log_stats, used=self.log_stats["captures"] > 0)
        res["hold_mode"] = bool(self.hold_ok)
        res["hold_requests"] = self.hold_log
        try:
            res["hold_echo"] = self.read_echo()         # 0x080-0x087 at the end
        except Exception:
            res["hold_echo"] = None
        res["clock_switches"] = clock_switches(self.results, self.switch_log)
        from . import zorro
        res["expansion_boards"] = zorro.decode(self.results)
        res["switch_announcements"] = self.switch_log
        res["cycles"] = {str(p): regs for p, regs in self.cycles.items()}
        res["cycle_counters"] = {str(p): v for p, v in self.cc.items()}
        res["has_tprobe"] = self.has_tprobe
        fails = [r for r in self.results if r["failure"]]
        for r in fails:
            what = (f"exception (vector offset {r['expected']}) at PC {r['got']}" if "exception" in r["flags"]
                    else f"{r['size']}-byte access at {r['addr']} expected {r['expected']} got {r['got']}"
                    if "summary" not in r["flags"] else
                    f"{int(r['got'], 16)} of {int(r['expected'], 16)} checks read back wrong")
            self.ev.result(f"exerciser_p{r['preset']}_{r['group'].replace(' ', '_')}", "finding",
                           f"preset {r['preset']}, {r['group']}: {what}", step="timing")
        if ok and not fails and self.machine is not None:
            self.ev.result("exerciser", "info", "no wrong data read back" + (" (some presets hung, see above)"
                           if self.hangs else " in the exerciser tests that ran"), step="timing")
        res["ok"] = ok
        (self.out / "timing.json").write_text(json.dumps(res, indent=1, default=str))
        (self.out / "histograms.json").write_text(json.dumps(
            {"measurements": res["histogram_measurements"], "fabric_mhz": res["fabric_mhz"],
             "histograms": res["histograms"]}, indent=1))
        write_hist_csv(self.out / "histograms.csv", self.hist, res["fabric_mhz"])
        if self.tp_hist:
            (self.out / "tprobe_histograms.json").write_text(json.dumps(res["tprobe_histograms"], indent=1))
            write_csv(self.out / "tprobe_histograms.csv", self.tp_hist)
        return res


def write_hist_csv(path, hist, fabric_mhz):
    """One row per preset / region / measurement: the 32 bins (fabric clks of 1 << shift)."""
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["preset", "region", "m", "measurement", "shift", "fabric_mhz", "as20_cycles", "lost",
                    "windows", "seconds"] + [f"bin{b}" for b in range(P.HIST_BINS)])
        for p, regs in sorted(hist.items()):
            for r, v in regs.items():
                for m in range(P.HIST_MEASUREMENTS):
                    w.writerow([p, r, m, P.HIST_MEAS[m][0], v["shift"], fabric_mhz.get(str(p)), v["as"],
                                v["lost"], v["windows"], v["seconds"]] + v["bins"][m])


def write_csv(path, hist):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["preset", "region", "meas", "name", "starts", "samples", "saturated"] +
                   [f"bin{b}" for b in range(P.TPROBE_BINS)])
        for p, regs in sorted(hist.items()):
            for r, ms in regs.items():
                for m, v in sorted(ms.items()):
                    w.writerow([p, r, m, P.MEAS[m][0], v["starts"], v["samples"], v["saturated"]] + v["bins"])


def clock_switches(results, announcements=None):
    """Test 0x01 info entries (one per CLKSEL switch): addr = preset (+ 0x100 when the Pi
    acked the switch), expected = polls of $E90044 until it read the new preset 64 times in a
    row, got = bus errors meanwhile; a failure entry with the timeout flag when it never
    settled. `announcements`: the Pi's own record of 0x088 (acked or not)."""
    out = []
    for r in results:
        if r.get("test") != P.CLOCK_SWITCH_TEST:
            continue
        if "info" in r.get("flags", []):
            a = int(r["addr"], 16)
            out.append({"preset": a & 0xFF, "acked": bool(a & 0x100), "polls": int(r["expected"], 16),
                        "bus_errors": int(r["got"], 16), "settled": True})
        elif "timeout" in r.get("flags", []):
            out.append({"preset": int(r["expected"], 16), "polls": None, "bus_errors": None,
                        "settled": False, "readback": int(r["got"], 16)})
    return out


def build_matches(commit_hex, expect):
    """The diag ID holds 32 bits of the commit: a 7-digit short hash right-aligned
    (0x0abc1234) or the first 8 hex digits of the hash."""
    e = expect.lower().strip()
    c = commit_hex.lower()
    try:
        if int(c, 16) == int(e, 16):
            return True
    except ValueError:
        return False
    return c.startswith(e[:8]) or (len(e) > 8 and e.startswith(c))


def load_info(images):
    from . import manifest
    return manifest.load(images)
