"""SPI to the RAM FPGA (spidev0.0): shared SRAM, diag commands, tprobe.

a314d must be stopped: it owns spidev0.0 when it runs.
Framing details live in protocol.py.
"""

import struct
import time

from . import protocol as P


class SpiError(RuntimeError):
    pass


def open_device(cfg=None):
    """A spidev.SpiDev (or the test fake named by cfg.fake_spi)."""
    hz = min(int(getattr(cfg, "spi_hz", P.SPI_HZ) or P.SPI_HZ), P.SPI_MAX_HZ)
    if cfg is not None and getattr(cfg, "fake_spi", ""):
        from .tests import fakes  # noqa: only reached in tests
        return fakes.FakeSpiDev(cfg.fake_spi)
    try:
        import spidev
    except ImportError as e:
        raise SpiError("python3-spidev is not installed") from e
    dev = spidev.SpiDev()
    try:
        dev.open(P.SPI_BUS, P.SPI_DEV)
    except OSError as e:
        raise SpiError(f"cannot open spidev{P.SPI_BUS}.{P.SPI_DEV}: {e}") from e
    dev.mode = P.SPI_MODE
    dev.max_speed_hz = hz
    return dev


def find_magic(resp, magic, search=P.MAGIC_SEARCH):
    i = resp.find(bytes([magic]), 1)
    if i < 0 or i > search:
        return None
    return i


class Spi:
    def __init__(self, dev):
        self.dev = dev

    def close(self):
        try:
            self.dev.close()
        except Exception:
            pass

    slow = False                 # set by the timing run around clock switches: SPI at SPI_SLOW_HZ

    def xfer(self, data, speed_hz=None):
        if self.slow:
            speed_hz = min(speed_hz or P.SPI_SLOW_HZ, P.SPI_SLOW_HZ)
        if speed_hz:
            return bytes(self.dev.xfer2(list(data), int(speed_hz)))
        return bytes(self.dev.xfer2(list(data)))

    # ---- shared SRAM ---------------------------------------------------------
    def sram_read(self, addr, n):
        out = bytearray()
        while n > 0:
            k = min(n, P.SPI_CHUNK)
            a = addr & 0x3FFF
            hdr = [P.SRAM_READ | ((a >> 16) & 0x1F), (a >> 8) & 0xFF, a & 0xFF]
            r = self.xfer(hdr + [0] * (P.SRAM_READ_DATA_AT - 3 + k))
            out += r[P.SRAM_READ_DATA_AT:P.SRAM_READ_DATA_AT + k]
            addr += k
            n -= k
        return bytes(out)

    def sram_write(self, addr, data):
        data = bytes(data)
        off = 0
        while off < len(data):
            k = min(len(data) - off, P.SPI_CHUNK)
            a = (addr + off) & 0x3FFF
            self.xfer([P.SRAM_WRITE | ((a >> 16) & 0x1F), (a >> 8) & 0xFF, a & 0xFF] + list(data[off:off + k]))
            off += k

    def u32(self, addr):
        return struct.unpack(">I", self.sram_read(addr, 4))[0]

    def u16(self, addr):
        return struct.unpack(">H", self.sram_read(addr, 2))[0]

    def proto_version(self):
        r = self.xfer([P.PROTO_VER, 0, 0, 0])
        return r[3]

    # ---- diag commands (DIAG builds only) ---------------------------------------
    def reset_assert(self):
        self.xfer([P.CMD_RESET_ASSERT])

    def reset_release(self):
        self.xfer([P.CMD_RESET_RELEASE])

    def diag_id(self):
        """{'version','features':[...],'feature_bits','commit'} or None (not a DIAG build)."""
        r = self.xfer([P.CMD_DIAG_ID] + [0] * (P.MAGIC_SEARCH + P.DIAG_ID_LEN))
        i = find_magic(r, P.MAGIC_DIAG_ID)
        if i is None:
            return None
        d = r[i:]
        bits = d[2]
        return {"version": d[1], "feature_bits": bits,
                "features": [n for b, n in P.FEATURES.items() if bits >> b & 1],
                "commit": d[3:7].hex()}

    def clocks_latch(self):
        """Latch and read the clock counters: {field: value} per protocol.CLOCK_FRAME."""
        n = sum(w for _, w in P.CLOCK_FRAME)
        r = self.xfer([P.CMD_CLOCKS] + [0] * (P.MAGIC_SEARCH + n))
        i = find_magic(r, P.MAGIC_CLOCKS)
        if i is None:
            raise SpiError("no clock counter magic (0xA4): not a DIAG build?")
        out, o = {}, i + 1
        for name, w in P.CLOCK_FRAME:
            out[name] = int.from_bytes(r[o:o + w], "big")
            o += w
        return out

    def cycle_counters(self):
        """0xA6: {field: value} per protocol.CYCLE_FRAME."""
        n = sum(w for _, w in P.CYCLE_FRAME)
        r = self.xfer([P.CMD_CYCLES] + [0] * (P.MAGIC_SEARCH + n))
        i = find_magic(r, P.MAGIC_CYCLES)
        if i is None:
            raise SpiError("no cycle counter magic (0xA6)")
        out, o = {}, i + 1
        for name, w in P.CYCLE_FRAME:
            out[name] = int.from_bytes(r[o:o + w], "big")
            o += w
        return out

    def cycle_clear(self, threshold=None):
        self.xfer([P.CMD_CYCLES_CLEAR] + ([threshold & 0xFF] if threshold is not None else []))

    # ---- timing histograms (0xA8 / 0xA9, bins in the SRAM) -------------------------
    def hist_start(self, shift, region):
        """Clear all 256 counters and bin region `region` with bin shift `shift`."""
        self.xfer([P.CMD_HIST_START, shift & 3, region & 7])

    def hist_status(self):
        n = sum(w for _, w in P.HIST_FRAME)
        r = self.xfer([P.CMD_HIST_STATUS] + [0] * (P.MAGIC_SEARCH + n))
        i = find_magic(r, P.MAGIC_HIST)
        if i is None:
            raise SpiError("no histogram magic (0xA9)")
        out, o = {}, i + 1
        for name, w in P.HIST_FRAME:
            out[name] = int.from_bytes(r[o:o + w], "big")
            o += w
        out["clearing"] = bool(out["status"] & P.HIST_STATUS_CLEARING)
        return out

    def hist_bins(self, reads=3):
        """[[32 counts] x 8]. The FPGA updates the counters by read-modify-write while
        the Pi reads, so a read can catch a half-updated counter: read `reads` times and
        take the median of each counter."""
        n = P.HIST_MEASUREMENTS * P.HIST_BINS
        snaps = []
        for _ in range(max(1, reads)):
            lo = self.sram_read(P.HIST_LO, n)
            hi = self.sram_read(P.HIST_HI, n)
            snaps.append([lo[i] | (hi[i] << 8) for i in range(n)])
        vals = [sorted(s[i] for s in snaps)[len(snaps) // 2] for i in range(n)]
        return [vals[m * P.HIST_BINS:(m + 1) * P.HIST_BINS] for m in range(P.HIST_MEASUREMENTS)]

    def wave_arm(self, kind, region=0, log=False):
        """Arm the waveform; with log=True (0xB4) the address log too (body running only)."""
        self.xfer([P.CMD_WAVE_LOG_ARM if log else P.CMD_WAVE_ARM, kind & 0xFF, region & 0xFF])

    def log_status(self):
        n = sum(w for _, w in P.LOG_FRAME)
        r = self.xfer([P.CMD_LOG_STATUS] + [0] * (P.MAGIC_SEARCH + n))
        i = find_magic(r, P.MAGIC_LOG)
        if i is None:
            raise SpiError("no address log magic (0xB5)")
        out, o = {}, i + 1
        for name, w in P.LOG_FRAME:
            out[name] = int.from_bytes(r[o:o + w], "big")
            o += w
        st = out["status"]
        out.update(armed=bool(st & P.LOG_STATUS_ARMED), frozen=bool(st & P.LOG_STATUS_FROZEN),
                   wrapped=bool(st & P.LOG_STATUS_WRAPPED), trigger_seen=bool(out["trigger"] & 0x400),
                   trigger_entry=out["trigger"] & 0x3FF)
        return out

    def log_entries(self, status):
        """Address log entries in time order (read only once the log is frozen)."""
        raw = self.sram_read(P.LOG_BASE, P.LOG_ENTRIES * P.LOG_ENTRY_BYTES)
        out = []
        for j in range(min(status["count"], P.LOG_ENTRIES)):
            e = (status["first"] + j) % P.LOG_ENTRIES
            ent = P.log_entry(raw[e * P.LOG_ENTRY_BYTES:(e + 1) * P.LOG_ENTRY_BYTES])
            ent["index"] = e
            out.append(ent)
        return out

    def wave_stop(self):
        self.xfer([P.CMD_WAVE_STOP])

    def wave_status(self):
        r = self.xfer([P.CMD_WAVE_READ] + [0] * (P.MAGIC_SEARCH + 12))
        i = find_magic(r, P.MAGIC_WAVE)
        if i is None:
            raise SpiError("no waveform magic (0xB1)")
        st = r[i + 1]
        count, first, trig, wp = struct.unpack(">HHHH", r[i + 2:i + 10])
        return {"armed": bool(st & P.WAVE_STATUS_ARMED), "triggered": bool(st & P.WAVE_STATUS_TRIGGERED),
                "full": bool(st & P.WAVE_STATUS_FULL), "status": st, "count": count,
                "first": first, "trigger": trig, "wp": wp, "kind": r[i + 10], "region": r[i + 11]}

    def wave_entries(self, status):
        """Entries in time order: list of (signals, duration_clks)."""
        raw = self.sram_read(P.WAVE_BASE, P.WAVE_SIZE)
        n = min(status["count"], P.WAVE_ENTRIES)
        out = []
        for j in range(n):
            e = (status["first"] + j) % P.WAVE_ENTRIES
            v = 0
            for k in range(P.WAVE_ENTRY_BYTES):
                v = (v << 8) | raw[P.wave_addr(e, k) - P.WAVE_BASE]
            out.append((v >> P.WAVE_DUR_BITS, P.wave_duration(v & ((1 << P.WAVE_DUR_BITS) - 1))))
        return out

    # ---- tprobe -------------------------------------------------------------
    def tprobe_select(self, meas, region=0):
        self.xfer([P.TPROBE_REGION | (region & 7), 0, 0])
        self.xfer([P.TPROBE_SEL | (meas & 0x1F), 0, 0])

    def tprobe_clear(self):
        self.xfer([P.TPROBE_CLEAR, 0, 0])

    def tprobe_read(self):
        r = self.xfer([P.TPROBE_READ] + [0] * 80)
        i = find_magic(r, P.TPROBE_MAGIC)
        if i is None:
            raise SpiError("no tprobe magic (0x80)")
        d = r[i:]
        w = [int.from_bytes(d[3 + 2 * k:5 + 2 * k], "big") for k in range(2 + P.TPROBE_BINS)]
        return {"sel": d[1], "region": d[2], "starts": w[0], "samples": w[1], "bins": w[2:]}

    # ---- pin readback image -------------------------------------------------
    def pinread(self, order=None):
        """{fpga_pin: level} from the diag_pins image, or None if it isn't loaded."""
        order = order or P.PINREAD_ORDER
        r = self.xfer([0] * P.PINREAD_LEN, P.PINREAD_HZ)
        if r[0] != P.PINREAD_MAGIC:
            return None
        bits = int.from_bytes(r[1:], "big")
        nbits = 8 * (len(r) - 1)
        return {pin: (bits >> (nbits - 1 - k)) & 1 for k, pin in enumerate(order)}


def sleep(s):
    time.sleep(s)
